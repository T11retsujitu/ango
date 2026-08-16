"""OKX public market-data WebSocket collector primitives。

認証を必要としない以下の feed を対象にする。

* public: ``trades`` / ``bbo-tbt`` / ``books`` / ``instruments``
* business: ``trades-all``

受信内容は解釈前に raw writer へ保存する。``books`` だけは、壊れた板状態を後段へ
渡さないため ``seqId`` / ``prevSeqId`` の連続性を接続中にも検証する。
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import random
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Any, Protocol

from mce.stream_store import GzipJsonlStreamWriter, StreamWriteError


PUBLIC_WS_URL = "wss://ws.okx.com:8443/ws/v5/public"
BUSINESS_WS_URL = "wss://ws.okx.com:8443/ws/v5/business"

logger = logging.getLogger(__name__)


def public_subscriptions(inst_id: str) -> list[dict[str, str]]:
    """板との配信順とmetadata変更を保つため、public 4 channelを同じ接続で購読する。"""

    return [
        {"channel": "trades", "instId": inst_id},
        {"channel": "bbo-tbt", "instId": inst_id},
        {"channel": "books", "instId": inst_id},
        # REST snapshot後の日中tick/lot/contract変更を欠落させない。OKXの
        # instruments channelはinstId filterではなくinstTypeを購読する仕様。
        {"channel": "instruments", "instType": "SWAP"},
    ]


def business_subscriptions(inst_id: str) -> list[dict[str, str]]:
    return [{"channel": "trades-all", "instId": inst_id}]


class WebSocketLike(Protocol):
    async def send(self, message: str) -> Any: ...

    async def recv(self) -> str | bytes: ...

    async def close(self) -> Any: ...


Connector = Callable[[str], Awaitable[WebSocketLike]]
WriterFactory = Callable[[str], GzipJsonlStreamWriter]


class MissingWebSocketDependency(RuntimeError):
    pass


class SubscriptionError(RuntimeError):
    """OKX が明示的にsubscriptionをrejectした（設定不良、権限不足など）。"""


class SubscriptionTimeout(RuntimeError):
    """要求した全subscription ACKが期限内に揃わなかった（再接続対象）。"""


class SessionRotationRequested(RuntimeError):
    """raw fileをhourlyに確定するための計画的なsession再接続。"""


class WebSocketProtocolError(RuntimeError):
    pass


class ReconnectRequested(RuntimeError):
    pass


class _StopRequested(Exception):
    pass


class BookSequenceStatus(str, Enum):
    SNAPSHOT = "snapshot"
    UPDATE = "update"
    HEARTBEAT = "heartbeat"
    RESET = "reset"


class BookSequenceGap(RuntimeError):
    """``books`` update が直前の有効な state に接続しない。"""

    def __init__(
        self,
        message: str,
        *,
        expected_prev: int | None,
        prev_seq_id: int | None,
        seq_id: int | None,
    ) -> None:
        super().__init__(message)
        self.expected_prev = expected_prev
        self.prev_seq_id = prev_seq_id
        self.seq_id = seq_id


class BooksSequenceValidator:
    """OKX ``books`` snapshot/update の連続性を検証する状態機械。"""

    def __init__(self) -> None:
        self.last_seq_id: int | None = None
        self.valid = False

    def reset(self) -> None:
        self.last_seq_id = None
        self.valid = False

    @staticmethod
    def _int_field(data: Mapping[str, Any], name: str) -> int:
        try:
            return int(data[name])
        except (KeyError, TypeError, ValueError) as exc:
            raise WebSocketProtocolError(f"books data has invalid {name}: {data.get(name)!r}") from exc

    def _gap(self, message: str, prev: int | None, seq: int | None) -> None:
        expected = self.last_seq_id
        self.valid = False
        raise BookSequenceGap(
            message,
            expected_prev=expected,
            prev_seq_id=prev,
            seq_id=seq,
        )

    def observe(self, action: str, data: Mapping[str, Any]) -> BookSequenceStatus:
        prev = self._int_field(data, "prevSeqId")
        seq = self._int_field(data, "seqId")
        if seq < 0:
            raise WebSocketProtocolError(f"books seqId must be non-negative: {seq}")

        # OKX heartbeatは asks/bids の「両fieldが存在し、両方とも空」のupdate。
        # field欠落を ``dict.get`` で空と同一視すると、schema破損を正常heartbeatとして
        # 飲み込んでしまうためfail-closedにする。
        if "asks" not in data or "bids" not in data:
            self._gap("books data is missing asks or bids", prev, seq)
        asks = data["asks"]
        bids = data["bids"]
        if not isinstance(asks, list) or not isinstance(bids, list):
            self._gap("books asks and bids must be arrays", prev, seq)

        if action == "snapshot":
            if prev != -1:
                self._gap("books snapshot prevSeqId must be -1", prev, seq)
            self.last_seq_id = seq
            self.valid = True
            return BookSequenceStatus.SNAPSHOT

        if action != "update":
            raise WebSocketProtocolError(f"unsupported books action: {action!r}")
        if not self.valid or self.last_seq_id is None:
            self._gap("books update received before a valid snapshot", prev, seq)
        if prev != self.last_seq_id:
            self._gap("books prevSeqId does not match the previous seqId", prev, seq)

        has_depth = bool(asks) or bool(bids)
        if seq == prev:
            if has_depth:
                self._gap("books repeated seqId carried non-empty depth", prev, seq)
            return BookSequenceStatus.HEARTBEAT

        # OKX は maintenance 時に seqId を小さい値へ reset する。その reset update
        # 自体も prev が直前 seq と一致する限り連続した正当な update である。
        status = BookSequenceStatus.RESET if seq < prev else BookSequenceStatus.UPDATE
        self.last_seq_id = seq
        self.valid = True
        return status

    def observe_message(self, message: Mapping[str, Any]) -> list[BookSequenceStatus]:
        # subscribe ACK/errorにも ``arg.channel=books`` が入るが、板データではない。
        if "event" in message:
            return []
        arg = message.get("arg")
        if not isinstance(arg, Mapping) or arg.get("channel") != "books":
            return []
        action = message.get("action")
        data = message.get("data")
        if not isinstance(action, str) or not isinstance(data, list):
            raise WebSocketProtocolError("books message must contain action and data array")
        statuses: list[BookSequenceStatus] = []
        for item in data:
            if not isinstance(item, Mapping):
                raise WebSocketProtocolError("books data item must be an object")
            statuses.append(self.observe(action, item))
        return statuses


# 呼び手側では channel 名より単数形の方が自然なため、互換用の別名も公開する。
BookSequenceValidator = BooksSequenceValidator


@dataclass
class ConnectionStats:
    sessions: int = 0
    sessions_ready: int = 0
    reconnects: int = 0
    rotations: int = 0
    frames_in: int = 0
    subscription_acks: int = 0
    subscription_timeouts: int = 0
    book_snapshots: int = 0
    book_updates: int = 0
    book_heartbeats: int = 0
    book_resets: int = 0
    book_gaps: int = 0
    errors: int = 0
    last_error: str | None = None


async def default_connector(url: str) -> WebSocketLike:
    """websockets を遅延 import し、ライブラリ標準pingは無効化する。"""

    try:
        try:
            from websockets.asyncio.client import connect
        except ImportError:
            from websockets import connect
    except ImportError as exc:
        raise MissingWebSocketDependency(
            "microstructure collector requires the 'websockets' package"
        ) from exc

    return await connect(
        url,
        ping_interval=None,
        open_timeout=20,
        close_timeout=5,
        max_queue=4096,
        max_size=16 * 1024 * 1024,
    )


_TIMEOUT = object()


def _consume_task_result(task: asyncio.Task[Any]) -> None:
    """bounded cancel後に遅れて終わったtaskの例外warningを回収する。"""

    with contextlib.suppress(asyncio.CancelledError, Exception):
        task.result()


async def _cancel_task(task: asyncio.Task[Any], timeout: float = 1.0) -> bool:
    """taskをcancelし、非協調的I/Oでもshutdownを無期限blockしない。"""

    if task.done():
        _consume_task_result(task)
        return True
    task.cancel()
    done, _ = await asyncio.wait({task}, timeout=timeout)
    if task in done:
        _consume_task_result(task)
        return True
    task.add_done_callback(_consume_task_result)
    return False


async def _recv_or_stop(
    websocket: WebSocketLike,
    stop_event: asyncio.Event,
    timeout: float,
) -> str | bytes | object:
    """recv、停止要求、timeout のどれかを待つ。停止時も20秒待たせない。"""

    if stop_event.is_set():
        raise _StopRequested
    recv_task = asyncio.create_task(websocket.recv())
    stop_task = asyncio.create_task(stop_event.wait())
    try:
        done, _ = await asyncio.wait(
            {recv_task, stop_task}, timeout=timeout, return_when=asyncio.FIRST_COMPLETED
        )
        if recv_task in done:
            await _cancel_task(stop_task)
            return recv_task.result()
        if stop_task in done:
            await _cancel_task(recv_task)
            raise _StopRequested
        await _cancel_task(recv_task)
        await _cancel_task(stop_task)
        return _TIMEOUT
    finally:
        # 外側taskがsession rotationやcollector shutdownでcancelされた場合も、
        # websocket.recv / Event.wait の子taskを孤児にしない。
        await _cancel_task(recv_task)
        await _cancel_task(stop_task)


class OkxWsConnection:
    """1本のOKX WebSocket接続を購読・監視し、切断時に再接続する。"""

    def __init__(
        self,
        *,
        name: str,
        url: str,
        subscriptions: Sequence[Mapping[str, str]],
        writer_factory: WriterFactory,
        connector: Connector = default_connector,
        heartbeat_seconds: float = 20.0,
        pong_timeout_seconds: float = 10.0,
        subscribe_timeout_seconds: float = 10.0,
        reconnect_min_seconds: float = 1.0,
        reconnect_max_seconds: float = 60.0,
        session_rotation_seconds: float | None = 3600.0,
        connect_timeout_seconds: float = 20.0,
        cancellation_timeout_seconds: float = 1.0,
    ) -> None:
        if not subscriptions:
            raise ValueError("at least one subscription is required")
        if min(heartbeat_seconds, pong_timeout_seconds, subscribe_timeout_seconds) <= 0:
            raise ValueError("heartbeat, pong and subscribe timeouts must be positive")
        if reconnect_min_seconds < 0 or reconnect_max_seconds < reconnect_min_seconds:
            raise ValueError("invalid reconnect delay range")
        if session_rotation_seconds is not None and session_rotation_seconds <= 0:
            raise ValueError("session_rotation_seconds must be positive or None")
        if connect_timeout_seconds <= 0 or cancellation_timeout_seconds < 0:
            raise ValueError("connect timeout must be positive and cancellation timeout non-negative")

        self.name = name
        self.url = url
        self.subscriptions = [dict(item) for item in subscriptions]
        self.writer_factory = writer_factory
        self.connector = connector
        self.heartbeat_seconds = heartbeat_seconds
        self.pong_timeout_seconds = pong_timeout_seconds
        self.subscribe_timeout_seconds = subscribe_timeout_seconds
        self.reconnect_min_seconds = reconnect_min_seconds
        self.reconnect_max_seconds = reconnect_max_seconds
        self.session_rotation_seconds = session_rotation_seconds
        self.connect_timeout_seconds = connect_timeout_seconds
        self.cancellation_timeout_seconds = cancellation_timeout_seconds
        self.stats = ConnectionStats()
        self._session_ready = False

    @staticmethod
    def _subscription_key(arg: Mapping[str, Any]) -> tuple[str, str, str, str]:
        return (
            str(arg.get("channel", "")),
            str(arg.get("instId", "")),
            str(arg.get("instType", "")),
            str(arg.get("instFamily", "")),
        )

    def _record_inbound(
        self, writer: GzipJsonlStreamWriter, raw: str | bytes
    ) -> str:
        if isinstance(raw, bytes):
            encoded = base64.b64encode(raw).decode("ascii")
            writer.append(encoded, direction="in", kind="binary_frame")
            raise WebSocketProtocolError("OKX sent an unexpected binary WebSocket frame")
        if not isinstance(raw, str):
            raise WebSocketProtocolError(f"unsupported WebSocket frame type: {type(raw)!r}")
        writer.append(raw, direction="in", kind="pong" if raw == "pong" else "frame")
        self.stats.frames_in += 1
        return raw

    def _decode_and_validate(
        self,
        raw: str,
        writer: GzipJsonlStreamWriter,
        validator: BooksSequenceValidator,
    ) -> Mapping[str, Any] | None:
        if raw == "pong":
            return None
        try:
            message = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise WebSocketProtocolError("OKX sent malformed JSON") from exc
        if not isinstance(message, Mapping):
            raise WebSocketProtocolError("OKX JSON frame must be an object")

        event = message.get("event")
        if event == "error":
            raise SubscriptionError(
                f"OKX websocket error {message.get('code')}: {message.get('msg')}"
            )
        if event == "notice" and str(message.get("code")) == "64008":
            raise ReconnectRequested("OKX websocket service upgrade notice (64008)")

        try:
            statuses = validator.observe_message(message)
        except BookSequenceGap as exc:
            self.stats.book_gaps += 1
            # 元frameは直前の inbound record に無改変で残っているが、監査時に
            # gap理由と該当payloadを一箇所で確認できるよう診断eventにも複製する。
            writer.append_event(
                "book_sequence_gap",
                message=str(exc),
                expected_prev=exc.expected_prev,
                prev_seq_id=exc.prev_seq_id,
                seq_id=exc.seq_id,
                raw_payload=raw,
            )
            raise
        for status in statuses:
            if status is BookSequenceStatus.SNAPSHOT:
                self.stats.book_snapshots += 1
            elif status is BookSequenceStatus.UPDATE:
                self.stats.book_updates += 1
            elif status is BookSequenceStatus.HEARTBEAT:
                self.stats.book_heartbeats += 1
            elif status is BookSequenceStatus.RESET:
                self.stats.book_resets += 1
        return message

    async def _receive_message(
        self,
        websocket: WebSocketLike,
        writer: GzipJsonlStreamWriter,
        validator: BooksSequenceValidator,
        stop_event: asyncio.Event,
        timeout: float,
    ) -> Mapping[str, Any] | None | object:
        raw = await _recv_or_stop(websocket, stop_event, timeout)
        if raw is _TIMEOUT:
            return _TIMEOUT
        raw_text = self._record_inbound(writer, raw)
        return self._decode_and_validate(raw_text, writer, validator)

    async def _subscribe(
        self,
        websocket: WebSocketLike,
        writer: GzipJsonlStreamWriter,
        validator: BooksSequenceValidator,
        stop_event: asyncio.Event,
    ) -> None:
        request_id = "".join(char for char in writer.session_id if char.isalnum())[:24]
        if not request_id:
            raise WebSocketProtocolError("session_id produced an empty OKX request id")
        request = {
            # OKX error 60033: request id は英数字のみ（最大32文字）。session UUIDの
            # hex部分だけを使えば、接続内で一意かつ仕様内に収まる。
            "id": request_id,
            "op": "subscribe",
            "args": self.subscriptions,
        }
        raw_request = json.dumps(request, separators=(",", ":"))
        await websocket.send(raw_request)
        writer.append(raw_request, direction="out", kind="subscribe")

        remaining = {self._subscription_key(item) for item in self.subscriptions}
        deadline = asyncio.get_running_loop().time() + self.subscribe_timeout_seconds
        while remaining:
            timeout = deadline - asyncio.get_running_loop().time()
            if timeout <= 0:
                self.stats.subscription_timeouts += 1
                writer.append_event(
                    "subscription_ack_timeout",
                    request_id=request_id,
                    remaining=sorted(remaining),
                )
                raise SubscriptionTimeout(
                    f"subscription ACK timeout: {sorted(remaining)}"
                )
            message = await self._receive_message(
                websocket, writer, validator, stop_event, timeout
            )
            if message is _TIMEOUT:
                self.stats.subscription_timeouts += 1
                writer.append_event(
                    "subscription_ack_timeout",
                    request_id=request_id,
                    remaining=sorted(remaining),
                )
                raise SubscriptionTimeout(
                    f"subscription ACK timeout: {sorted(remaining)}"
                )
            if message is None:
                continue
            if message.get("event") == "subscribe":
                ack_id = message.get("id")
                if ack_id != request_id:
                    writer.append_event(
                        "subscription_ack_ignored",
                        reason="request_id_mismatch",
                        expected_id=request_id,
                        received_id=ack_id,
                    )
                    continue
                arg = message.get("arg")
                if not isinstance(arg, Mapping):
                    raise WebSocketProtocolError("subscription ACK has no arg object")
                key = self._subscription_key(arg)
                if key in remaining:
                    remaining.remove(key)
                    self.stats.subscription_acks += 1
                else:
                    writer.append_event(
                        "subscription_ack_ignored",
                        reason="unexpected_or_duplicate_subscription",
                        request_id=request_id,
                        subscription=key,
                    )

        self._session_ready = True
        self.stats.sessions_ready += 1
        writer.append_event("subscriptions_ready", subscriptions=self.subscriptions)

    async def _await_pong(
        self,
        websocket: WebSocketLike,
        writer: GzipJsonlStreamWriter,
        validator: BooksSequenceValidator,
        stop_event: asyncio.Event,
    ) -> None:
        deadline = asyncio.get_running_loop().time() + self.pong_timeout_seconds
        while True:
            timeout = deadline - asyncio.get_running_loop().time()
            if timeout <= 0:
                raise TimeoutError("OKX websocket pong timeout")
            raw = await _recv_or_stop(websocket, stop_event, timeout)
            if raw is _TIMEOUT:
                raise TimeoutError("OKX websocket pong timeout")
            raw_text = self._record_inbound(writer, raw)
            if raw_text == "pong":
                return
            # ping と pong の間に market-data が来ても捨てずに検証・保存する。
            self._decode_and_validate(raw_text, writer, validator)

    async def _run_session(
        self,
        websocket: WebSocketLike,
        writer: GzipJsonlStreamWriter,
        stop_event: asyncio.Event,
    ) -> None:
        validator = BooksSequenceValidator()
        await self._subscribe(websocket, writer, validator, stop_event)
        rotation_deadline = (
            asyncio.get_running_loop().time() + self.session_rotation_seconds
            if self.session_rotation_seconds is not None
            else None
        )
        while True:
            timeout = self.heartbeat_seconds
            if rotation_deadline is not None:
                rotation_remaining = rotation_deadline - asyncio.get_running_loop().time()
                if rotation_remaining <= 0:
                    raise SessionRotationRequested(
                        f"planned raw session rotation after {self.session_rotation_seconds}s"
                    )
                timeout = min(timeout, rotation_remaining)
            message = await self._receive_message(
                websocket,
                writer,
                validator,
                stop_event,
                timeout,
            )
            if message is not _TIMEOUT:
                continue
            if (
                rotation_deadline is not None
                and asyncio.get_running_loop().time() >= rotation_deadline
            ):
                raise SessionRotationRequested(
                    f"planned raw session rotation after {self.session_rotation_seconds}s"
                )
            await websocket.send("ping")
            writer.append("ping", direction="out", kind="ping")
            await self._await_pong(websocket, writer, validator, stop_event)

    async def run_session(
        self,
        websocket: WebSocketLike,
        writer: GzipJsonlStreamWriter,
        stop_event: asyncio.Event,
    ) -> None:
        """接続済みWebSocketを1 session処理する。fixtureテスト用にも公開する。"""

        try:
            await self._run_session(websocket, writer, stop_event)
        except _StopRequested:
            return

    async def _wait_before_reconnect(self, stop_event: asyncio.Event, delay: float) -> None:
        if delay <= 0:
            return
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(stop_event.wait(), timeout=delay)

    async def _connect_or_stop(self, stop_event: asyncio.Event) -> WebSocketLike:
        """duration/signalが発火したら、接続handshake中でも直ちに中断する。"""

        connect_task = asyncio.create_task(self.connector(self.url))
        stop_task = asyncio.create_task(stop_event.wait())
        done, _ = await asyncio.wait(
            {connect_task, stop_task},
            timeout=self.connect_timeout_seconds,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if connect_task in done:
            await _cancel_task(stop_task)
            return connect_task.result()
        if stop_task in done:
            cancelled = await _cancel_task(
                connect_task, timeout=self.cancellation_timeout_seconds
            )
            if not cancelled:
                logger.warning("%s connector ignored bounded cancellation", self.name)
            raise _StopRequested
        await _cancel_task(stop_task)
        cancelled = await _cancel_task(
            connect_task, timeout=self.cancellation_timeout_seconds
        )
        if not cancelled:
            logger.warning("%s connector timed out and ignored bounded cancellation", self.name)
        raise TimeoutError(
            f"{self.name} websocket connect timeout after {self.connect_timeout_seconds}s"
        )

    async def run(self, stop_event: asyncio.Event) -> None:
        """停止要求まで実行し、回復可能な切断・gapでは新しいsessionへ接続する。"""

        attempt = 0
        while not stop_event.is_set():
            writer = self.writer_factory(self.name)
            websocket: WebSocketLike | None = None
            delay = 0.0
            self._session_ready = False
            self.stats.sessions += 1
            try:
                writer.append_event(
                    "session_opening", url=self.url, subscriptions=self.subscriptions
                )
                websocket = await self._connect_or_stop(stop_event)
                writer.append_event("connected", url=self.url)
                await self.run_session(websocket, writer, stop_event)
                if stop_event.is_set():
                    writer.append_event("stopped")
                    return
                raise ConnectionError("websocket session ended unexpectedly")
            except asyncio.CancelledError:
                raise
            except _StopRequested:
                writer.append_event("stopped")
                return
            except SessionRotationRequested as exc:
                self.stats.rotations += 1
                attempt = 0
                writer.append_event(
                    "session_rotation",
                    message=str(exc),
                    next_session_frame_no=0,
                )
            except (MissingWebSocketDependency, SubscriptionError, StreamWriteError) as exc:
                with contextlib.suppress(StreamWriteError):
                    writer.append_event(
                        "fatal_error",
                        error_type=type(exc).__name__,
                        message=str(exc),
                    )
                raise
            except Exception as exc:
                self.stats.errors += 1
                self.stats.last_error = f"{type(exc).__name__}: {exc}"
                with contextlib.suppress(StreamWriteError):
                    writer.append_event(
                        "session_error",
                        error_type=type(exc).__name__,
                        message=str(exc),
                    )
                if stop_event.is_set():
                    return
                self.stats.reconnects += 1
                # 一度readyまで到達した接続はcollector設定・endpoint自体が正常だった。
                # その後の切断を過去のhandshake失敗回数に累積させない。
                if self._session_ready:
                    attempt = 0
                cap = min(
                    self.reconnect_max_seconds,
                    self.reconnect_min_seconds * (2**attempt),
                )
                delay = random.uniform(0.0, cap) if cap > 0 else 0.0
                attempt += 1
                logger.warning("%s reconnecting after %s", self.name, self.stats.last_error)
            finally:
                if websocket is not None:
                    with contextlib.suppress(Exception):
                        await websocket.close()
                writer.close()

            await self._wait_before_reconnect(stop_event, delay)

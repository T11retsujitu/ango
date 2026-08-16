import asyncio
import gzip
import json
import time
from collections import deque

import pytest

from mce.okx_ws import (
    BookSequenceGap,
    BookSequenceStatus,
    BooksSequenceValidator,
    OkxWsConnection,
    SubscriptionError,
    business_subscriptions,
    public_subscriptions,
)
from mce.collect_microstructure import collect_microstructure
from mce.stream_store import GzipJsonlStreamWriter


INST_ID = "BTC-USDT-SWAP"


def _book(action, prev, seq, *, asks=None, bids=None):
    return {
        "arg": {"channel": "books", "instId": INST_ID},
        "action": action,
        "data": [
            {
                "asks": [] if asks is None else asks,
                "bids": [] if bids is None else bids,
                "ts": "1786800000000",
                "prevSeqId": prev,
                "seqId": seq,
                "checksum": 0,
            }
        ],
    }


def _ack(channel, request_id=None):
    arg = (
        {"channel": channel, "instType": "SWAP"}
        if channel == "instruments"
        else {"channel": channel, "instId": INST_ID}
    )
    message = {
        "event": "subscribe",
        "arg": arg,
        "connId": "fixture",
    }
    if request_id is not None:
        message["id"] = request_id
    return json.dumps(message)


class ScriptedWebSocket:
    def __init__(self, incoming=()):
        self.incoming = deque(incoming)
        self.sent = []
        self.closed = False
        self.waiter = asyncio.Event()

    async def send(self, message):
        self.sent.append(message)
        # fixture ACKにrequest idを省略した場合、直前のsubscribe requestと同じidを
        # serverのようにechoする。明示idはmismatch testcase用に保持する。
        try:
            request = json.loads(message)
        except json.JSONDecodeError:
            return
        if request.get("op") != "subscribe":
            return
        rewritten = deque()
        for item in self.incoming:
            if isinstance(item, str):
                try:
                    candidate = json.loads(item)
                except json.JSONDecodeError:
                    candidate = None
                if (
                    isinstance(candidate, dict)
                    and candidate.get("event") == "subscribe"
                    and "id" not in candidate
                ):
                    candidate["id"] = request["id"]
                    item = json.dumps(candidate)
            rewritten.append(item)
        self.incoming = rewritten

    async def recv(self):
        if self.incoming:
            item = self.incoming.popleft()
            if isinstance(item, BaseException):
                raise item
            return item
        await self.waiter.wait()
        # Tests only set waiter while also appending a scripted item.
        self.waiter.clear()
        return await self.recv()

    def push(self, item):
        self.incoming.append(item)
        self.waiter.set()

    async def close(self):
        self.closed = True
        self.waiter.set()


def _writer_factory(tmp_path, paths):
    def factory(name):
        writer = GzipJsonlStreamWriter(tmp_path, name)
        paths.append(writer.path)
        return writer

    return factory


def _read_rows(path):
    with gzip.open(path, "rt", encoding="utf-8") as f:
        return [json.loads(line) for line in f]


def test_book_sequence_accepts_snapshot_update_empty_heartbeat_and_reset():
    validator = BooksSequenceValidator()

    assert validator.observe_message(_book("snapshot", -1, 10, bids=[["1", "2", "0", "1"]])) == [
        BookSequenceStatus.SNAPSHOT
    ]
    assert validator.observe_message(_book("update", 10, 15, asks=[["2", "3", "0", "1"]])) == [
        BookSequenceStatus.UPDATE
    ]
    # 約60秒無更新時に送られる正当な空update。
    assert validator.observe_message(_book("update", 15, 15)) == [
        BookSequenceStatus.HEARTBEAT
    ]
    # maintenance resetはprevが直前seqに接続していれば正当。
    assert validator.observe_message(_book("update", 15, 3, bids=[["1", "4", "0", "2"]])) == [
        BookSequenceStatus.RESET
    ]
    assert validator.observe_message(_book("update", 3, 5)) == [BookSequenceStatus.UPDATE]
    assert validator.last_seq_id == 5
    assert validator.valid


def test_book_sequence_gap_invalidates_until_new_snapshot():
    validator = BooksSequenceValidator()
    validator.observe_message(_book("snapshot", -1, 10))

    with pytest.raises(BookSequenceGap) as raised:
        validator.observe_message(_book("update", 9, 11))
    assert raised.value.expected_prev == 10
    assert raised.value.prev_seq_id == 9
    assert not validator.valid

    with pytest.raises(BookSequenceGap, match="before a valid snapshot"):
        validator.observe_message(_book("update", 10, 12))
    assert validator.observe_message(_book("snapshot", -1, 20)) == [
        BookSequenceStatus.SNAPSHOT
    ]


def test_repeated_sequence_with_depth_is_not_a_heartbeat():
    validator = BooksSequenceValidator()
    validator.observe_message(_book("snapshot", -1, 10))
    with pytest.raises(BookSequenceGap, match="non-empty depth"):
        validator.observe_message(
            _book("update", 10, 10, asks=[["2", "3", "0", "1"]])
        )


@pytest.mark.parametrize("missing_field", ["asks", "bids"])
def test_missing_depth_field_is_not_misclassified_as_heartbeat(missing_field):
    validator = BooksSequenceValidator()
    validator.observe_message(_book("snapshot", -1, 10))
    update = _book("update", 10, 10)
    del update["data"][0][missing_field]

    with pytest.raises(BookSequenceGap, match="missing asks or bids"):
        validator.observe_message(update)
    assert not validator.valid


def test_public_session_requires_all_acks_and_sends_application_ping(tmp_path):
    async def scenario():
        paths = []
        websocket = ScriptedWebSocket(
            [_ack("trades"), _ack("bbo-tbt"), _ack("books"), _ack("instruments")]
        )
        connection = OkxWsConnection(
            name="public",
            url="wss://example.invalid/public",
            subscriptions=public_subscriptions(INST_ID),
            writer_factory=_writer_factory(tmp_path, paths),
            heartbeat_seconds=0.01,
            pong_timeout_seconds=0.2,
            subscribe_timeout_seconds=0.2,
            reconnect_min_seconds=0,
            reconnect_max_seconds=0,
        )
        writer = _writer_factory(tmp_path, paths)("public")
        stop = asyncio.Event()
        task = asyncio.create_task(connection.run_session(websocket, writer, stop))

        async def wait_for_ping():
            for _ in range(100):
                if "ping" in websocket.sent:
                    return
                await asyncio.sleep(0.002)
            raise AssertionError("collector did not send ping")

        await wait_for_ping()
        websocket.push("pong")
        await asyncio.sleep(0)
        stop.set()
        await asyncio.wait_for(task, timeout=0.2)
        writer.close()

        subscribe = json.loads(websocket.sent[0])
        assert subscribe["op"] == "subscribe"
        assert subscribe["id"].isalnum()
        assert len(subscribe["id"]) <= 32
        assert subscribe["args"] == public_subscriptions(INST_ID)
        assert websocket.sent[-1] == "ping"
        assert connection.stats.subscription_acks == 4
        rows = _read_rows(writer.path)
        inbound = [row["payload"] for row in rows if row["direction"] == "in"]
        assert [json.loads(raw)["arg"]["channel"] for raw in inbound[:-1]] == [
            "trades",
            "bbo-tbt",
            "books",
            "instruments",
        ]
        assert all(json.loads(raw)["id"] == subscribe["id"] for raw in inbound[:-1])
        assert inbound[-1] == "pong"

    asyncio.run(scenario())


def test_ack_error_is_fatal_for_session(tmp_path):
    async def scenario():
        paths = []
        error = json.dumps({"event": "error", "code": "60012", "msg": "bad arg"})
        websocket = ScriptedWebSocket([error])
        connection = OkxWsConnection(
            name="business",
            url="wss://example.invalid/business",
            subscriptions=business_subscriptions(INST_ID),
            writer_factory=_writer_factory(tmp_path, paths),
            subscribe_timeout_seconds=0.2,
        )
        writer = _writer_factory(tmp_path, paths)("business")
        with pytest.raises(SubscriptionError, match="60012"):
            await connection.run_session(websocket, writer, asyncio.Event())
        writer.close()

    asyncio.run(scenario())


def test_subscription_ack_must_echo_request_id(tmp_path):
    async def scenario():
        paths = []
        websocket = ScriptedWebSocket(
            [_ack("trades-all", "foreign-request"), _ack("trades-all")]
        )
        connection = OkxWsConnection(
            name="business",
            url="wss://example.invalid/business",
            subscriptions=business_subscriptions(INST_ID),
            writer_factory=_writer_factory(tmp_path, paths),
            subscribe_timeout_seconds=0.2,
        )
        writer = _writer_factory(tmp_path, paths)("business")
        stop = asyncio.Event()
        task = asyncio.create_task(connection.run_session(websocket, writer, stop))
        for _ in range(100):
            if connection.stats.sessions_ready == 1:
                break
            await asyncio.sleep(0.002)
        else:
            raise AssertionError("matching ACK was not accepted")
        stop.set()
        await asyncio.wait_for(task, timeout=0.2)
        writer.close()

        assert connection.stats.subscription_acks == 1
        ignored = [
            json.loads(row["payload"])
            for row in _read_rows(writer.path)
            if row["kind"] == "subscription_ack_ignored"
        ]
        assert ignored == [
            {
                "expected_id": json.loads(websocket.sent[0])["id"],
                "reason": "request_id_mismatch",
                "received_id": "foreign-request",
            }
        ]

    asyncio.run(scenario())


def test_books_gap_is_counted_and_forces_session_failure(tmp_path):
    async def scenario():
        paths = []
        gap_raw = json.dumps(_book("update", 8, 11))
        incoming = [
            _ack("trades"),
            _ack("bbo-tbt"),
            _ack("books"),
            _ack("instruments"),
            json.dumps(_book("snapshot", -1, 10)),
            gap_raw,
        ]
        websocket = ScriptedWebSocket(incoming)
        connection = OkxWsConnection(
            name="public",
            url="wss://example.invalid/public",
            subscriptions=public_subscriptions(INST_ID),
            writer_factory=_writer_factory(tmp_path, paths),
            subscribe_timeout_seconds=0.2,
        )
        writer = _writer_factory(tmp_path, paths)("public")
        with pytest.raises(BookSequenceGap):
            await connection.run_session(websocket, writer, asyncio.Event())
        writer.close()
        assert connection.stats.book_snapshots == 1
        assert connection.stats.book_gaps == 1
        gap_events = [
            json.loads(row["payload"])
            for row in _read_rows(writer.path)
            if row["kind"] == "book_sequence_gap"
        ]
        assert gap_events == [
            {
                "expected_prev": 10,
                "message": "books prevSeqId does not match the previous seqId",
                "prev_seq_id": 8,
                "raw_payload": gap_raw,
                "seq_id": 11,
            }
        ]

    asyncio.run(scenario())


def test_subscription_timeout_reconnects_instead_of_failing_fatal(tmp_path):
    async def scenario():
        paths = []
        first = ScriptedWebSocket()
        second = ScriptedWebSocket([_ack("trades-all")])
        sessions = deque([first, second])

        async def connector(url):
            return sessions.popleft()

        connection = OkxWsConnection(
            name="business",
            url="wss://example.invalid/business",
            subscriptions=business_subscriptions(INST_ID),
            writer_factory=_writer_factory(tmp_path, paths),
            connector=connector,
            subscribe_timeout_seconds=0.01,
            reconnect_min_seconds=0,
            reconnect_max_seconds=0,
        )
        stop = asyncio.Event()
        task = asyncio.create_task(connection.run(stop))
        for _ in range(200):
            if connection.stats.sessions_ready == 1:
                break
            await asyncio.sleep(0.002)
        else:
            raise AssertionError("collector did not reconnect after ACK timeout")
        stop.set()
        await asyncio.wait_for(task, timeout=0.2)

        assert connection.stats.subscription_timeouts == 1
        assert connection.stats.reconnects == 1
        assert connection.stats.errors == 1
        assert connection.stats.sessions == 2
        assert first.closed and second.closed

    asyncio.run(scenario())


def test_ready_session_resets_exponential_backoff(tmp_path, monkeypatch):
    async def scenario():
        paths = []
        sessions = deque(
            [
                ScriptedWebSocket([ConnectionError("pre-ready-1")]),
                ScriptedWebSocket([ConnectionError("pre-ready-2")]),
                ScriptedWebSocket(
                    [_ack("trades-all"), ConnectionError("post-ready")]
                ),
                ScriptedWebSocket([_ack("trades-all")]),
            ]
        )

        async def connector(url):
            return sessions.popleft()

        delays = []

        async def record_delay(stop_event, delay):
            delays.append(delay)

        monkeypatch.setattr("mce.okx_ws.random.uniform", lambda low, high: high)
        connection = OkxWsConnection(
            name="business",
            url="wss://example.invalid/business",
            subscriptions=business_subscriptions(INST_ID),
            writer_factory=_writer_factory(tmp_path, paths),
            connector=connector,
            subscribe_timeout_seconds=0.2,
            reconnect_min_seconds=1,
            reconnect_max_seconds=8,
        )
        connection._wait_before_reconnect = record_delay
        stop = asyncio.Event()
        task = asyncio.create_task(connection.run(stop))
        for _ in range(200):
            if connection.stats.sessions_ready >= 2:
                break
            await asyncio.sleep(0.002)
        else:
            raise AssertionError("fourth session did not become ready")
        stop.set()
        await asyncio.wait_for(task, timeout=0.2)

        assert delays == [1, 2, 1]
        assert connection.stats.reconnects == 3
        assert connection.stats.sessions == 4

    asyncio.run(scenario())


def test_planned_rotation_creates_new_complete_session_file(tmp_path):
    async def scenario():
        paths = []
        sockets = []

        async def connector(url):
            websocket = ScriptedWebSocket([_ack("trades-all")])
            sockets.append(websocket)
            return websocket

        connection = OkxWsConnection(
            name="business",
            url="wss://example.invalid/business",
            subscriptions=business_subscriptions(INST_ID),
            writer_factory=_writer_factory(tmp_path, paths),
            connector=connector,
            heartbeat_seconds=1,
            subscribe_timeout_seconds=0.2,
            reconnect_min_seconds=1,
            reconnect_max_seconds=8,
            session_rotation_seconds=0.01,
        )
        stop = asyncio.Event()
        task = asyncio.create_task(connection.run(stop))
        for _ in range(200):
            if connection.stats.rotations >= 1 and connection.stats.sessions_ready >= 2:
                break
            await asyncio.sleep(0.002)
        else:
            raise AssertionError("collector did not rotate raw session")
        stop.set()
        await asyncio.wait_for(task, timeout=0.2)

        assert connection.stats.rotations == 1
        assert connection.stats.reconnects == 0
        assert connection.stats.errors == 0
        assert len(paths) == 2
        assert all(path.exists() for path in paths)
        assert list(tmp_path.glob("okx/ws/business/**/*.partial")) == []
        rows_by_session = [_read_rows(path) for path in paths]
        assert [row["frame_no"] for row in rows_by_session[0]] == list(
            range(len(rows_by_session[0]))
        )
        assert [row["frame_no"] for row in rows_by_session[1]] == list(
            range(len(rows_by_session[1]))
        )
        assert rows_by_session[0][0]["session_id"] != rows_by_session[1][0]["session_id"]

    asyncio.run(scenario())


def test_shutdown_does_not_wait_forever_for_noncooperative_connector(tmp_path):
    async def scenario():
        paths = []
        release = asyncio.Event()
        cancellation_seen = asyncio.Event()

        async def connector(url):
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancellation_seen.set()
                await release.wait()
                raise

        connection = OkxWsConnection(
            name="business",
            url="wss://example.invalid/business",
            subscriptions=business_subscriptions(INST_ID),
            writer_factory=_writer_factory(tmp_path, paths),
            connector=connector,
            cancellation_timeout_seconds=0.01,
        )
        stop = asyncio.Event()
        task = asyncio.create_task(connection.run(stop))
        await asyncio.sleep(0)
        started = time.monotonic()
        stop.set()
        await asyncio.wait_for(task, timeout=0.1)
        elapsed = time.monotonic() - started

        assert cancellation_seen.is_set()
        assert elapsed < 0.08
        assert paths[0].exists()
        release.set()
        await asyncio.sleep(0.01)

    asyncio.run(scenario())


def test_connection_reconnects_after_transport_failure(tmp_path):
    async def scenario():
        paths = []
        first = ScriptedWebSocket(
            [_ack("trades-all"), ConnectionError("fixture disconnect")]
        )
        second = ScriptedWebSocket([_ack("trades-all")])
        sessions = deque([first, second])

        async def connector(url):
            return sessions.popleft()

        connection = OkxWsConnection(
            name="business",
            url="wss://example.invalid/business",
            subscriptions=business_subscriptions(INST_ID),
            writer_factory=_writer_factory(tmp_path, paths),
            connector=connector,
            heartbeat_seconds=1,
            pong_timeout_seconds=1,
            subscribe_timeout_seconds=0.2,
            reconnect_min_seconds=0,
            reconnect_max_seconds=0,
        )
        stop = asyncio.Event()
        task = asyncio.create_task(connection.run(stop))
        for _ in range(100):
            if connection.stats.sessions >= 2 and connection.stats.subscription_acks >= 2:
                break
            await asyncio.sleep(0.002)
        else:
            raise AssertionError("collector did not reconnect")
        stop.set()
        await asyncio.wait_for(task, timeout=0.2)

        assert connection.stats.sessions == 2
        assert connection.stats.reconnects == 1
        assert connection.stats.errors == 1
        assert first.closed and second.closed
        assert len(paths) == 2

    asyncio.run(scenario())


def test_duration_stops_both_public_and_business_collectors(tmp_path):
    async def scenario():
        public = ScriptedWebSocket(
            [_ack("trades"), _ack("bbo-tbt"), _ack("books"), _ack("instruments")]
        )
        business = ScriptedWebSocket([_ack("trades-all")])

        async def connector(url):
            return business if url.endswith("/business") else public

        stats = await collect_microstructure(
            raw_dir=tmp_path,
            inst_id=INST_ID,
            duration=0.1,
            public_url="wss://fixture/public",
            business_url="wss://fixture/business",
            connector=connector,
            heartbeat_seconds=1,
            pong_timeout_seconds=1,
            subscribe_timeout_seconds=0.2,
            reconnect_min_seconds=0,
            reconnect_max_seconds=0,
            collect_instrument_metadata=False,
        )
        assert stats["public"].sessions == 1
        assert stats["business"].sessions == 1
        assert stats["public"].subscription_acks == 4
        assert stats["business"].subscription_acks == 1
        assert public.closed and business.closed
        assert len(list(tmp_path.glob("okx/ws/public/**/*.jsonl.gz"))) == 1
        assert len(list(tmp_path.glob("okx/ws/business/**/*.jsonl.gz"))) == 1

    asyncio.run(scenario())


def test_duration_interrupts_a_hanging_connection_handshake(tmp_path):
    async def scenario():
        cancelled = 0

        async def connector(url):
            nonlocal cancelled
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled += 1
                raise

        stats = await asyncio.wait_for(
            collect_microstructure(
                raw_dir=tmp_path,
                inst_id=INST_ID,
                duration=0.01,
                public_url="wss://fixture/public",
                business_url="wss://fixture/business",
                connector=connector,
                subscribe_timeout_seconds=0.2,
                collect_instrument_metadata=False,
            ),
            timeout=0.2,
        )
        assert cancelled == 2
        assert stats["public"].sessions == 1
        assert stats["business"].sessions == 1

    asyncio.run(scenario())

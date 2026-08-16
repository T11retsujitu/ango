"""Closed OKX WebSocket raw filesの整合性を検査する。

collector が確定した ``*.jsonl.gz`` だけを対象にし、予測ラベルや特徴量は生成しない。
raw wrapper、接続 lifecycle、購読 ACK、板 sequence を到着順に再検証し、soak
運転を研究データとして使えるか判断するための記述統計を JSON で返す。
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import heapq
import json
import math
import os
import random
import tempfile
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Mapping, Sequence

from mce.okx_ws import (
    BookSequenceGap,
    BookSequenceStatus,
    BooksSequenceValidator,
    WebSocketProtocolError,
)
from mce.stream_store import SCHEMA_VERSION


TARGET_CHANNELS = {"trades", "bbo-tbt", "books", "trades-all", "instruments"}
REQUIRED_CHANNELS = {
    "public": {"trades", "bbo-tbt", "books"},
    "business": {"trades-all"},
}
ALLOWED_CHANNELS = {
    "public": {"trades", "bbo-tbt", "books", "instruments"},
    "business": {"trades-all"},
}
REQUIRED_WRAPPER_FIELDS = {
    "schema_version",
    "stream",
    "session_id",
    "frame_no",
    "direction",
    "kind",
    "received_at_ns",
    "monotonic_ns",
    "payload",
}
_MAX_REASON_DETAILS = 100
_LAG_SAMPLE_LIMIT = 200_000


def _reason(code: str, message: str, **context: Any) -> dict[str, Any]:
    result = {"code": code, "message": message}
    if context:
        result["context"] = context
    return result


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _nonnegative_integer(value: Any) -> int:
    if _is_int(value):
        result = value
    elif isinstance(value, str) and value.isascii() and value.isdigit():
        result = int(value)
    else:
        raise ValueError
    if result < 0:
        raise ValueError
    return result


def _quantile(values: Sequence[float], probability: float) -> float | None:
    if not values:
        return None
    if len(values) == 1:
        return values[0]
    position = probability * (len(values) - 1)
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return values[lower]
    weight = position - lower
    return values[lower] * (1.0 - weight) + values[upper] * weight


class _LagAccumulator:
    """negative 件数は全件、quantile はmerge可能なpriority sampleで保持する。"""

    def __init__(self, *, sample_limit: int = _LAG_SAMPLE_LIMIT, seed: int = 0) -> None:
        self.sample_limit = sample_limit
        self._random = random.Random(seed)
        self._sample_heap: list[tuple[float, float]] = []
        self.count = 0
        self.negative_count = 0
        self.minimum: float | None = None
        self.maximum: float | None = None

    def add(self, value: float) -> None:
        self.count += 1
        if value < 0:
            self.negative_count += 1
        self.minimum = value if self.minimum is None else min(self.minimum, value)
        self.maximum = value if self.maximum is None else max(self.maximum, value)
        priority = self._random.random()
        entry = (priority, value)
        if len(self._sample_heap) < self.sample_limit:
            heapq.heappush(self._sample_heap, entry)
        elif priority > self._sample_heap[0][0]:
            heapq.heapreplace(self._sample_heap, entry)

    @property
    def samples(self) -> list[float]:
        return [value for _, value in self._sample_heap]

    def merge(self, other: _LagAccumulator) -> None:
        self.count += other.count
        self.negative_count += other.negative_count
        if other.minimum is not None:
            self.minimum = (
                other.minimum if self.minimum is None else min(self.minimum, other.minimum)
            )
        if other.maximum is not None:
            self.maximum = (
                other.maximum if self.maximum is None else max(self.maximum, other.maximum)
            )
        for entry in other._sample_heap:
            if len(self._sample_heap) < self.sample_limit:
                heapq.heappush(self._sample_heap, entry)
            elif entry[0] > self._sample_heap[0][0]:
                heapq.heapreplace(self._sample_heap, entry)

    def report(self) -> dict[str, Any]:
        sample = sorted(self.samples)
        quantiles = {
            name: _quantile(sample, probability)
            for name, probability in (
                ("p50", 0.50),
                ("p90", 0.90),
                ("p95", 0.95),
                ("p99", 0.99),
            )
        }
        return {
            "unit": "ms",
            "count": self.count,
            "negative_count": self.negative_count,
            "negative_fraction": self.negative_count / self.count if self.count else None,
            "min": self.minimum,
            **quantiles,
            "max": self.maximum,
            "sample_size": len(sample),
            "quantiles_approximate": self.count > len(sample),
        }


@dataclass
class _TradeAccumulator:
    count: int = 0
    quantity: Decimal = Decimal(0)
    first_ts_ms: int | None = None
    last_ts_ms: int | None = None
    minute_counts: dict[int, int] = field(default_factory=lambda: defaultdict(int))
    minute_quantity: dict[int, Decimal] = field(
        default_factory=lambda: defaultdict(Decimal)
    )

    def add(self, ts_ms: int, quantity: Decimal) -> None:
        self.count += 1
        self.quantity += quantity
        self.first_ts_ms = ts_ms if self.first_ts_ms is None else min(self.first_ts_ms, ts_ms)
        self.last_ts_ms = ts_ms if self.last_ts_ms is None else max(self.last_ts_ms, ts_ms)
        minute = ts_ms // 60_000
        self.minute_counts[minute] += 1
        self.minute_quantity[minute] += quantity

    def merge(self, other: _TradeAccumulator) -> None:
        self.count += other.count
        self.quantity += other.quantity
        if other.first_ts_ms is not None:
            self.first_ts_ms = (
                other.first_ts_ms
                if self.first_ts_ms is None
                else min(self.first_ts_ms, other.first_ts_ms)
            )
        if other.last_ts_ms is not None:
            self.last_ts_ms = (
                other.last_ts_ms
                if self.last_ts_ms is None
                else max(self.last_ts_ms, other.last_ts_ms)
            )
        for minute, count in other.minute_counts.items():
            self.minute_counts[minute] += count
        for minute, quantity in other.minute_quantity.items():
            self.minute_quantity[minute] += quantity

    def report(self) -> dict[str, Any]:
        return {
            "items": self.count,
            "contract_quantity": _decimal_text(self.quantity),
            "first_exchange_ts_ms": self.first_ts_ms,
            "last_exchange_ts_ms": self.last_ts_ms,
            "active_minute_buckets": len(self.minute_counts),
        }


@dataclass
class _InternalFileResult:
    report: dict[str, Any]
    lag: _LagAccumulator
    trades: dict[str, _TradeAccumulator]
    instrument_ids: set[str]


def _decimal_text(value: Decimal) -> str:
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _channel_template() -> dict[str, Any]:
    return {
        "messages": 0,
        "items": 0,
        "payload_bytes": 0,
        "first_exchange_ts_ms": None,
        "last_exchange_ts_ms": None,
    }


def _record_issue(
    reasons: list[dict[str, Any]], code: str, message: str, **context: Any
) -> None:
    if len(reasons) < _MAX_REASON_DETAILS:
        reasons.append(_reason(code, message, **context))


def _parse_payload_object(
    payload: str,
    *,
    reasons: list[dict[str, Any]],
    line_no: int,
    context: str,
) -> Mapping[str, Any] | None:
    try:
        value = json.loads(payload)
    except json.JSONDecodeError as exc:
        _record_issue(
            reasons,
            "payload_json_invalid",
            f"{context} payload is not JSON: {exc.msg}",
            line_no=line_no,
        )
        return None
    if not isinstance(value, Mapping):
        _record_issue(
            reasons,
            "payload_schema_invalid",
            f"{context} payload must be a JSON object",
            line_no=line_no,
        )
        return None
    return value


def _subscription_key(value: Mapping[str, Any]) -> tuple[str, str, str, str]:
    return (
        str(value.get("channel", "")),
        str(value.get("instId", "")),
        str(value.get("instType", "")),
        str(value.get("instFamily", "")),
    )


def _analyze_file(path: Path) -> _InternalFileResult:
    reasons: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    records = 0
    uncompressed_bytes = 0
    payload_bytes = 0
    first_received_ns: int | None = None
    last_received_ns: int | None = None
    first_monotonic_ns: int | None = None
    last_monotonic_ns: int | None = None
    expected_frame_no = 0
    session_id: str | None = None
    stream: str | None = None
    lifecycle: list[dict[str, Any]] = []
    subscribe_index: int | None = None
    subscribe_count = 0
    ready_index: int | None = None
    ack_indexes: list[int] = []
    requested: set[tuple[str, str, str, str]] = set()
    opening_subscriptions: set[tuple[str, str, str, str]] = set()
    acknowledged: set[tuple[str, str, str, str]] = set()
    ready_subscriptions: set[tuple[str, str, str, str]] = set()
    duplicate_acks = 0
    channels: dict[str, dict[str, Any]] = defaultdict(_channel_template)
    lag_seed = int.from_bytes(
        hashlib.sha256(str(path).encode("utf-8")).digest()[:8], "big"
    )
    lag = _LagAccumulator(seed=lag_seed)
    trades: dict[str, _TradeAccumulator] = defaultdict(_TradeAccumulator)
    book_validator = BooksSequenceValidator()
    book_counts = {
        "messages": 0,
        "items": 0,
        "snapshots": 0,
        "updates": 0,
        "heartbeats": 0,
        "resets": 0,
        "sequence_gaps": 0,
        "protocol_errors": 0,
    }
    instrument_ids: set[str] = set()
    btc_instrument_ids: set[str] = set()
    btc_contract_metadata: dict[str, dict[str, Any]] = {}
    gzip_ok = True

    try:
        compressed_bytes = path.stat().st_size
    except OSError as exc:
        return _InternalFileResult(
            report={
                "path": str(path),
                "valid": False,
                "reasons": [_reason("file_unreadable", str(exc))],
            },
            lag=_LagAccumulator(),
            trades={},
            instrument_ids=set(),
        )

    try:
        with gzip.open(path, "rt", encoding="utf-8", newline="") as handle:
            for line_no, line in enumerate(handle, start=1):
                line_bytes = len(line.encode("utf-8"))
                uncompressed_bytes += line_bytes
                if not line.strip():
                    _record_issue(
                        reasons,
                        "blank_jsonl_line",
                        "blank JSONL line",
                        line_no=line_no,
                    )
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError as exc:
                    _record_issue(
                        reasons,
                        "wrapper_json_invalid",
                        f"raw wrapper is not JSON: {exc.msg}",
                        line_no=line_no,
                    )
                    continue
                if not isinstance(row, Mapping):
                    _record_issue(
                        reasons,
                        "wrapper_schema_invalid",
                        "raw wrapper must be a JSON object",
                        line_no=line_no,
                    )
                    continue

                records += 1
                missing = sorted(REQUIRED_WRAPPER_FIELDS - row.keys())
                if missing:
                    _record_issue(
                        reasons,
                        "wrapper_fields_missing",
                        "raw wrapper is missing required fields",
                        line_no=line_no,
                        fields=missing,
                    )
                    continue

                if not _is_int(row["schema_version"]) or row["schema_version"] != SCHEMA_VERSION:
                    _record_issue(
                        reasons,
                        "schema_version_invalid",
                        "unsupported raw wrapper schema_version",
                        line_no=line_no,
                        value=row["schema_version"],
                    )
                frame_no = row["frame_no"]
                if not _is_int(frame_no):
                    _record_issue(
                        reasons,
                        "frame_no_invalid",
                        "frame_no must be an integer",
                        line_no=line_no,
                    )
                else:
                    if frame_no != expected_frame_no:
                        _record_issue(
                            reasons,
                            "frame_sequence_gap",
                            "frame_no is not contiguous",
                            line_no=line_no,
                            expected=expected_frame_no,
                            actual=frame_no,
                        )
                    expected_frame_no = frame_no + 1

                row_stream = row["stream"]
                row_session = row["session_id"]
                if not isinstance(row_stream, str) or not row_stream:
                    _record_issue(
                        reasons,
                        "stream_invalid",
                        "stream must be a non-empty string",
                        line_no=line_no,
                    )
                elif stream is None:
                    stream = row_stream
                elif row_stream != stream:
                    _record_issue(
                        reasons,
                        "stream_changed",
                        "stream changed inside one file",
                        line_no=line_no,
                        first=stream,
                        actual=row_stream,
                    )
                if not isinstance(row_session, str) or not row_session:
                    _record_issue(
                        reasons,
                        "session_id_invalid",
                        "session_id must be a non-empty string",
                        line_no=line_no,
                    )
                elif session_id is None:
                    session_id = row_session
                elif row_session != session_id:
                    _record_issue(
                        reasons,
                        "session_id_changed",
                        "session_id changed inside one file",
                        line_no=line_no,
                        first=session_id,
                        actual=row_session,
                    )

                direction = row["direction"]
                kind = row["kind"]
                payload = row["payload"]
                received_ns = row["received_at_ns"]
                monotonic_ns = row["monotonic_ns"]
                if direction not in {"in", "out", "local"}:
                    _record_issue(
                        reasons,
                        "direction_invalid",
                        "direction must be in/out/local",
                        line_no=line_no,
                        value=direction,
                    )
                if not isinstance(kind, str) or not kind:
                    _record_issue(
                        reasons,
                        "kind_invalid",
                        "kind must be a non-empty string",
                        line_no=line_no,
                    )
                if not isinstance(payload, str):
                    _record_issue(
                        reasons,
                        "payload_invalid",
                        "payload must be a string",
                        line_no=line_no,
                    )
                    continue
                payload_size = len(payload.encode("utf-8"))
                payload_bytes += payload_size
                if not _is_int(received_ns) or received_ns < 0:
                    _record_issue(
                        reasons,
                        "received_at_ns_invalid",
                        "received_at_ns must be a non-negative integer",
                        line_no=line_no,
                    )
                    received_ns = None
                else:
                    first_received_ns = (
                        received_ns
                        if first_received_ns is None
                        else min(first_received_ns, received_ns)
                    )
                    last_received_ns = (
                        received_ns
                        if last_received_ns is None
                        else max(last_received_ns, received_ns)
                    )
                if not _is_int(monotonic_ns) or monotonic_ns < 0:
                    _record_issue(
                        reasons,
                        "monotonic_ns_invalid",
                        "monotonic_ns must be a non-negative integer",
                        line_no=line_no,
                    )
                else:
                    if last_monotonic_ns is not None and monotonic_ns < last_monotonic_ns:
                        _record_issue(
                            reasons,
                            "monotonic_clock_reversed",
                            "monotonic_ns decreased",
                            line_no=line_no,
                            previous=last_monotonic_ns,
                            actual=monotonic_ns,
                        )
                    first_monotonic_ns = (
                        monotonic_ns if first_monotonic_ns is None else first_monotonic_ns
                    )
                    last_monotonic_ns = monotonic_ns

                if direction == "local":
                    details = _parse_payload_object(
                        payload,
                        reasons=reasons,
                        line_no=line_no,
                        context="local event",
                    )
                    lifecycle.append({"kind": kind, "frame_no": frame_no})
                    if kind == "session_opening" and details is not None:
                        values = details.get("subscriptions")
                        if isinstance(values, list):
                            for value in values:
                                if isinstance(value, Mapping):
                                    opening_subscriptions.add(_subscription_key(value))
                                else:
                                    _record_issue(
                                        reasons,
                                        "opening_schema_invalid",
                                        "session_opening contains a non-object subscription",
                                        line_no=line_no,
                                    )
                        else:
                            _record_issue(
                                reasons,
                                "opening_schema_invalid",
                                "session_opening has no subscriptions array",
                                line_no=line_no,
                            )
                    if kind == "subscriptions_ready":
                        ready_index = frame_no if _is_int(frame_no) else None
                        if details is not None:
                            values = details.get("subscriptions")
                            if isinstance(values, list):
                                for value in values:
                                    if isinstance(value, Mapping):
                                        ready_subscriptions.add(_subscription_key(value))
                                    else:
                                        _record_issue(
                                            reasons,
                                            "ready_schema_invalid",
                                            "subscriptions_ready contains a non-object",
                                            line_no=line_no,
                                        )
                            else:
                                _record_issue(
                                    reasons,
                                    "ready_schema_invalid",
                                    "subscriptions_ready has no subscriptions array",
                                    line_no=line_no,
                                )
                    continue

                if direction == "out":
                    if kind == "subscribe":
                        subscribe_count += 1
                        message = _parse_payload_object(
                            payload,
                            reasons=reasons,
                            line_no=line_no,
                            context="subscribe",
                        )
                        subscribe_index = frame_no if _is_int(frame_no) else None
                        if message is not None:
                            args = message.get("args")
                            if message.get("op") != "subscribe" or not isinstance(args, list):
                                _record_issue(
                                    reasons,
                                    "subscribe_schema_invalid",
                                    "outbound subscribe must have op=subscribe and args array",
                                    line_no=line_no,
                                )
                            else:
                                for value in args:
                                    if isinstance(value, Mapping):
                                        requested.add(_subscription_key(value))
                                    else:
                                        _record_issue(
                                            reasons,
                                            "subscribe_schema_invalid",
                                            "subscribe args contains a non-object",
                                            line_no=line_no,
                                        )
                    elif kind == "ping" and payload != "ping":
                        _record_issue(
                            reasons,
                            "ping_payload_invalid",
                            "outbound ping payload must be literal ping",
                            line_no=line_no,
                        )
                    continue

                # Inbound pong is deliberately not JSON.
                if payload == "pong" and kind == "pong":
                    continue
                message = _parse_payload_object(
                    payload,
                    reasons=reasons,
                    line_no=line_no,
                    context="inbound frame",
                )
                if message is None:
                    continue
                event = message.get("event")
                if event == "subscribe":
                    arg = message.get("arg")
                    if not isinstance(arg, Mapping):
                        _record_issue(
                            reasons,
                            "ack_schema_invalid",
                            "subscription ACK has no arg object",
                            line_no=line_no,
                        )
                    else:
                        key = _subscription_key(arg)
                        if key in acknowledged:
                            duplicate_acks += 1
                        acknowledged.add(key)
                        if _is_int(frame_no):
                            ack_indexes.append(frame_no)
                    continue
                if event == "error":
                    _record_issue(
                        reasons,
                        "exchange_error_event",
                        "OKX returned an error event",
                        line_no=line_no,
                        exchange_code=message.get("code"),
                        exchange_message=message.get("msg"),
                    )
                    continue
                if event is not None:
                    continue

                arg = message.get("arg")
                data = message.get("data")
                if not isinstance(arg, Mapping) or not isinstance(arg.get("channel"), str):
                    _record_issue(
                        reasons,
                        "market_arg_invalid",
                        "market-data frame has no arg.channel",
                        line_no=line_no,
                    )
                    continue
                channel = str(arg["channel"])
                if channel not in TARGET_CHANNELS:
                    warnings.append(
                        _reason(
                            "unexpected_channel",
                            "market-data channel is outside this collector protocol",
                            line_no=line_no,
                            channel=channel,
                        )
                    )
                if not isinstance(data, list):
                    _record_issue(
                        reasons,
                        "market_data_invalid",
                        "market-data frame has no data array",
                        line_no=line_no,
                        channel=channel,
                    )
                    continue
                channel_report = channels[channel]
                channel_report["messages"] += 1
                channel_report["items"] += len(data)
                channel_report["payload_bytes"] += payload_size
                if channel == "books":
                    book_counts["messages"] += 1
                    book_counts["items"] += len(data)

                for item_index, item in enumerate(data):
                    if not isinstance(item, Mapping):
                        _record_issue(
                            reasons,
                            "market_item_invalid",
                            "market-data item must be an object",
                            line_no=line_no,
                            item_index=item_index,
                            channel=channel,
                        )
                        continue
                    raw_ts = item.get("ts")
                    ts_ms: int | None = None
                    if raw_ts is not None or channel != "instruments":
                        try:
                            ts_ms = _nonnegative_integer(raw_ts)
                        except (TypeError, ValueError):
                            _record_issue(
                                reasons,
                                "exchange_ts_invalid",
                                "market-data item ts must be non-negative integer milliseconds",
                                line_no=line_no,
                                item_index=item_index,
                                channel=channel,
                                value=raw_ts,
                            )
                            ts_ms = None
                    if ts_ms is not None:
                        first_ts = channel_report["first_exchange_ts_ms"]
                        last_ts = channel_report["last_exchange_ts_ms"]
                        channel_report["first_exchange_ts_ms"] = (
                            ts_ms if first_ts is None else min(first_ts, ts_ms)
                        )
                        channel_report["last_exchange_ts_ms"] = (
                            ts_ms if last_ts is None else max(last_ts, ts_ms)
                        )
                        if received_ns is not None:
                            lag.add((received_ns - ts_ms * 1_000_000) / 1_000_000.0)

                    if channel in {"trades", "trades-all"}:
                        raw_quantity = item.get("sz")
                        try:
                            quantity = Decimal(str(raw_quantity))
                            if not quantity.is_finite() or quantity < 0:
                                raise InvalidOperation
                        except (InvalidOperation, ValueError):
                            _record_issue(
                                reasons,
                                "trade_size_invalid",
                                "trade sz must be a non-negative finite decimal",
                                line_no=line_no,
                                item_index=item_index,
                                channel=channel,
                                value=raw_quantity,
                            )
                        else:
                            if ts_ms is not None:
                                source = f"{stream or 'unknown'}/{channel}"
                                trades[source].add(ts_ms, quantity)
                    elif channel == "instruments":
                        inst_id = item.get("instId")
                        if not isinstance(inst_id, str) or not inst_id:
                            _record_issue(
                                reasons,
                                "instrument_id_invalid",
                                "instrument metadata item has no instId",
                                line_no=line_no,
                                item_index=item_index,
                            )
                        else:
                            instrument_ids.add(inst_id)
                            if "BTC" in inst_id.upper():
                                btc_instrument_ids.add(inst_id)
                                selected_fields = (
                                    "instId",
                                    "instType",
                                    "instFamily",
                                    "uly",
                                    "ctVal",
                                    "ctMult",
                                    "ctValCcy",
                                    "ctType",
                                    "settleCcy",
                                    "tickSz",
                                    "lotSz",
                                    "minSz",
                                    "state",
                                    "ruleType",
                                )
                                btc_contract_metadata[inst_id] = {
                                    key: item.get(key)
                                    for key in selected_fields
                                    if key in item
                                }

                if channel == "books":
                    try:
                        statuses = book_validator.observe_message(message)
                    except BookSequenceGap as exc:
                        book_counts["sequence_gaps"] += 1
                        _record_issue(
                            reasons,
                            "books_sequence_gap",
                            str(exc),
                            line_no=line_no,
                            expected_prev=exc.expected_prev,
                            prev_seq_id=exc.prev_seq_id,
                            seq_id=exc.seq_id,
                        )
                    except WebSocketProtocolError as exc:
                        book_counts["protocol_errors"] += 1
                        _record_issue(
                            reasons,
                            "books_protocol_invalid",
                            str(exc),
                            line_no=line_no,
                        )
                    else:
                        for status in statuses:
                            key = {
                                BookSequenceStatus.SNAPSHOT: "snapshots",
                                BookSequenceStatus.UPDATE: "updates",
                                BookSequenceStatus.HEARTBEAT: "heartbeats",
                                BookSequenceStatus.RESET: "resets",
                            }[status]
                            book_counts[key] += 1
    except (gzip.BadGzipFile, EOFError, UnicodeDecodeError, OSError) as exc:
        gzip_ok = False
        _record_issue(
            reasons,
            "gzip_read_failed",
            f"gzip/UTF-8 stream could not be read: {type(exc).__name__}: {exc}",
        )

    if records == 0:
        _record_issue(reasons, "empty_file", "raw file contains no valid wrapper records")
    if stream not in REQUIRED_CHANNELS:
        _record_issue(
            reasons,
            "stream_unsupported",
            "stream must be public or business",
            stream=stream,
        )

    lifecycle_kinds = [event["kind"] for event in lifecycle]
    for required in ("session_opening", "connected", "subscriptions_ready"):
        if lifecycle_kinds.count(required) != 1:
            _record_issue(
                reasons,
                "lifecycle_event_count_invalid",
                f"expected exactly one {required} event",
                kind=required,
                count=lifecycle_kinds.count(required),
            )
    terminal = [
        item for item in lifecycle if item["kind"] in {"stopped", "session_error", "fatal_error"}
    ]
    if len(terminal) != 1:
        _record_issue(
            reasons,
            "lifecycle_terminal_invalid",
            "expected exactly one terminal lifecycle event",
            count=len(terminal),
        )
    elif terminal[0]["kind"] == "fatal_error":
        _record_issue(
            reasons,
            "fatal_session",
            "collector session ended with fatal_error",
        )
    elif terminal[0]["kind"] == "session_error":
        warnings.append(
            _reason(
                "transport_session_error",
                "session ended with a recorded recoverable error; inspect adjacent reconnect session",
            )
        )

    lifecycle_positions = {item["kind"]: item["frame_no"] for item in lifecycle}
    ordering = [
        lifecycle_positions.get("session_opening"),
        lifecycle_positions.get("connected"),
        subscribe_index,
        max(ack_indexes) if ack_indexes else None,
        ready_index,
        terminal[0]["frame_no"] if len(terminal) == 1 else None,
    ]
    if all(_is_int(value) for value in ordering) and ordering != sorted(ordering):
        _record_issue(
            reasons,
            "lifecycle_order_invalid",
            "opening, connected, subscribe, ACK, ready, terminal are out of order",
            frame_numbers=ordering,
        )

    if not requested:
        _record_issue(reasons, "subscriptions_missing", "no outbound subscriptions found")
    if subscribe_count != 1:
        _record_issue(
            reasons,
            "subscribe_count_invalid",
            "expected exactly one outbound subscribe request",
            count=subscribe_count,
        )
    if opening_subscriptions != requested:
        _record_issue(
            reasons,
            "session_opening_subscriptions_mismatch",
            "session_opening subscriptions do not match the outbound request",
            opening=sorted(opening_subscriptions),
            requested=sorted(requested),
        )
    missing_acks = sorted(requested - acknowledged)
    unexpected_acks = sorted(acknowledged - requested)
    if missing_acks:
        _record_issue(
            reasons,
            "subscription_ack_missing",
            "not all requested subscriptions were acknowledged",
            subscriptions=missing_acks,
        )
    if unexpected_acks:
        _record_issue(
            reasons,
            "subscription_ack_unexpected",
            "ACK was not present in the outbound request",
            subscriptions=unexpected_acks,
        )
    if ready_subscriptions != requested:
        _record_issue(
            reasons,
            "subscriptions_ready_mismatch",
            "subscriptions_ready does not match the outbound request",
            requested=sorted(requested),
            ready=sorted(ready_subscriptions),
        )
    if stream in REQUIRED_CHANNELS:
        requested_channels = {item[0] for item in requested}
        missing_required = REQUIRED_CHANNELS[stream] - requested_channels
        disallowed = requested_channels - ALLOWED_CHANNELS[stream]
        if missing_required:
            _record_issue(
                reasons,
                "collector_channel_set_invalid",
                "requested channels omit required collector feeds",
                stream=stream,
                missing=sorted(missing_required),
            )
        if disallowed:
            _record_issue(
                reasons,
                "collector_channel_set_invalid",
                "requested channels contain feeds outside this collector protocol",
                stream=stream,
                disallowed=sorted(disallowed),
            )
        for channel in sorted(requested_channels - channels.keys()):
            warnings.append(
                _reason(
                    "channel_has_no_data",
                    "subscription completed but no market-data frame was observed",
                    channel=channel,
                )
            )

    if stream == "public" and "books" in {item[0] for item in requested}:
        if book_counts["snapshots"] == 0:
            _record_issue(
                reasons,
                "books_snapshot_missing",
                "books subscription did not yield a valid snapshot",
            )
        if not book_validator.valid:
            _record_issue(
                reasons,
                "books_state_invalid_at_close",
                "books state was not valid at file close",
            )
    if lag.negative_count:
        warnings.append(
            _reason(
                "negative_exchange_lag",
                "exchange ts was later than local receive wall clock; inspect clock synchronization",
                count=lag.negative_count,
            )
        )
    if duplicate_acks:
        warnings.append(
            _reason(
                "duplicate_subscription_ack",
                "duplicate subscription ACKs were observed",
                count=duplicate_acks,
            )
        )

    duration_ns = None
    if first_monotonic_ns is not None and last_monotonic_ns is not None:
        duration_ns = last_monotonic_ns - first_monotonic_ns
    report = {
        "path": str(path),
        "valid": not reasons,
        "reasons": reasons,
        "warnings": warnings,
        "file": {
            "gzip_ok": gzip_ok,
            "compressed_bytes": compressed_bytes,
            "uncompressed_bytes": uncompressed_bytes,
            "payload_bytes": payload_bytes,
            "records": records,
        },
        "session": {
            "stream": stream,
            "session_id": session_id,
            "first_received_at_ns": first_received_ns,
            "last_received_at_ns": last_received_ns,
            "first_monotonic_ns": first_monotonic_ns,
            "last_monotonic_ns": last_monotonic_ns,
            "duration_seconds": duration_ns / 1_000_000_000 if duration_ns is not None else None,
            "lifecycle": lifecycle,
            "terminal": terminal[0]["kind"] if len(terminal) == 1 else None,
        },
        "subscriptions": {
            "session_opening": [list(item) for item in sorted(opening_subscriptions)],
            "requested": [list(item) for item in sorted(requested)],
            "acknowledged": [list(item) for item in sorted(acknowledged)],
            "ready": [list(item) for item in sorted(ready_subscriptions)],
            "missing_acks": [list(item) for item in missing_acks],
            "unexpected_acks": [list(item) for item in unexpected_acks],
            "duplicate_ack_count": duplicate_acks,
        },
        "channels": dict(sorted(channels.items())),
        "exchange_to_receive_lag": lag.report(),
        "books": {
            **book_counts,
            "state_valid_at_close": book_validator.valid,
            "last_seq_id": book_validator.last_seq_id,
        },
        "instruments": {
            "messages": channels.get("instruments", {}).get("messages", 0),
            "items": channels.get("instruments", {}).get("items", 0),
            "unique_instrument_count": len(instrument_ids),
            "btc_instrument_count": len(btc_instrument_ids),
            "btc_instrument_ids": sorted(btc_instrument_ids),
            "btc_usdt_swap_present": "BTC-USDT-SWAP" in instrument_ids,
            "btc_contract_metadata": [
                btc_contract_metadata[key] for key in sorted(btc_contract_metadata)
            ],
        },
        "trades": {key: value.report() for key, value in sorted(trades.items())},
    }
    return _InternalFileResult(
        report=report,
        lag=lag,
        trades=dict(trades),
        instrument_ids=instrument_ids,
    )


def analyze_file(path: Path | str) -> dict[str, Any]:
    """1 closed raw fileを検査する。"""

    return _analyze_file(Path(path)).report


def _expand_inputs(inputs: Sequence[Path | str]) -> tuple[list[Path], list[dict[str, Any]]]:
    files: set[Path] = set()
    errors: list[dict[str, Any]] = []
    for raw_input in inputs:
        path = Path(raw_input)
        if not path.exists():
            errors.append(_reason("input_missing", "input path does not exist", path=str(path)))
        elif path.is_dir():
            files.update(item for item in path.rglob("*.jsonl.gz") if item.is_file())
        elif path.is_file() and path.name.endswith(".jsonl.gz"):
            files.add(path)
        else:
            errors.append(
                _reason(
                    "input_unsupported",
                    "input must be a directory or a closed *.jsonl.gz file",
                    path=str(path),
                )
            )
    return sorted(files, key=lambda value: str(value)), errors


def _merge_channel(target: dict[str, Any], source: Mapping[str, Any]) -> None:
    target["messages"] += int(source["messages"])
    target["items"] += int(source["items"])
    target["payload_bytes"] += int(source["payload_bytes"])
    first = source.get("first_exchange_ts_ms")
    last = source.get("last_exchange_ts_ms")
    if first is not None:
        target["first_exchange_ts_ms"] = (
            first
            if target["first_exchange_ts_ms"] is None
            else min(target["first_exchange_ts_ms"], first)
        )
    if last is not None:
        target["last_exchange_ts_ms"] = (
            last
            if target["last_exchange_ts_ms"] is None
            else max(target["last_exchange_ts_ms"], last)
        )


def _trade_reconciliation(trades: Mapping[str, _TradeAccumulator]) -> dict[str, Any]:
    public = trades.get("public/trades", _TradeAccumulator())
    business = trades.get("business/trades-all", _TradeAccumulator())
    result: dict[str, Any] = {
        "comparison_is_descriptive_only": True,
        "note": (
            "trades is aggregated while trades-all contains individual fills and the two feeds use "
            "independent connections; item counts are not an equality gate"
        ),
        "public_trades": public.report(),
        "business_trades_all": business.report(),
        "available": bool(public.count and business.count),
    }
    if not result["available"]:
        result["overlap"] = None
        return result

    assert public.first_ts_ms is not None and public.last_ts_ms is not None
    assert business.first_ts_ms is not None and business.last_ts_ms is not None
    start = max(public.first_ts_ms, business.first_ts_ms)
    end = min(public.last_ts_ms, business.last_ts_ms)
    if end < start:
        result["overlap"] = {
            "available": False,
            "start_exchange_ts_ms": start,
            "end_exchange_ts_ms": end,
        }
        return result

    first_minute = start // 60_000
    last_minute = end // 60_000
    active_minutes = {
        minute
        for minute in set(public.minute_counts) | set(business.minute_counts)
        if first_minute <= minute <= last_minute
    }
    public_count = sum(public.minute_counts.get(minute, 0) for minute in active_minutes)
    business_count = sum(business.minute_counts.get(minute, 0) for minute in active_minutes)
    public_quantity = sum(
        (public.minute_quantity.get(minute, Decimal(0)) for minute in active_minutes),
        Decimal(0),
    )
    business_quantity = sum(
        (business.minute_quantity.get(minute, Decimal(0)) for minute in active_minutes),
        Decimal(0),
    )
    difference = public_quantity - business_quantity
    denominator = max(abs(public_quantity), abs(business_quantity))
    result["overlap"] = {
        "available": True,
        "method": "inclusive minute buckets intersecting the common exchange timestamp window",
        "start_exchange_ts_ms": start,
        "end_exchange_ts_ms": end,
        "minute_bucket_count": last_minute - first_minute + 1,
        "active_minute_bucket_count": len(active_minutes),
        "public_items": public_count,
        "business_items": business_count,
        "public_contract_quantity": _decimal_text(public_quantity),
        "business_contract_quantity": _decimal_text(business_quantity),
        "public_minus_business_contract_quantity": _decimal_text(difference),
        "absolute_relative_quantity_difference": (
            float(abs(difference) / denominator) if denominator else 0.0
        ),
        "business_items_per_public_item": (
            business_count / public_count if public_count else None
        ),
    }
    return result


def analyze_paths(inputs: Sequence[Path | str]) -> dict[str, Any]:
    """複数file/directoryを展開してsession別・全体のquality reportを返す。"""

    files, input_errors = _expand_inputs(inputs)
    if not files:
        input_errors.append(_reason("no_closed_raw_files", "no closed *.jsonl.gz files found"))

    reports: list[dict[str, Any]] = []
    aggregate_channels: dict[str, dict[str, Any]] = defaultdict(_channel_template)
    aggregate_lag = _LagAccumulator(seed=1)
    aggregate_trades: dict[str, _TradeAccumulator] = defaultdict(_TradeAccumulator)
    stream_summary: dict[str, dict[str, int]] = defaultdict(
        lambda: {"files": 0, "valid_files": 0, "records": 0}
    )
    compressed_bytes = 0
    uncompressed_bytes = 0
    record_count = 0
    session_paths: dict[str, list[str]] = defaultdict(list)

    aggregate_books = {
        "messages": 0,
        "items": 0,
        "snapshots": 0,
        "updates": 0,
        "heartbeats": 0,
        "resets": 0,
        "sequence_gaps": 0,
        "protocol_errors": 0,
    }
    aggregate_instruments = {
        "messages": 0,
        "items": 0,
        "unique_instrument_ids": set(),
        "btc_instrument_ids": set(),
        "btc_usdt_swap_present": False,
    }

    for path in files:
        item = _analyze_file(path)
        report = item.report
        reports.append(report)
        file_info = report.get("file", {})
        session = report.get("session", {})
        compressed_bytes += int(file_info.get("compressed_bytes", 0))
        uncompressed_bytes += int(file_info.get("uncompressed_bytes", 0))
        record_count += int(file_info.get("records", 0))
        stream = session.get("stream")
        if isinstance(stream, str):
            stream_summary[stream]["files"] += 1
            stream_summary[stream]["records"] += int(file_info.get("records", 0))
            if report.get("valid"):
                stream_summary[stream]["valid_files"] += 1
        session_id = session.get("session_id")
        if isinstance(session_id, str):
            session_paths[session_id].append(str(report["path"]))
        for channel, channel_report in report.get("channels", {}).items():
            _merge_channel(aggregate_channels[channel], channel_report)
        aggregate_lag.merge(item.lag)
        for source, values in item.trades.items():
            aggregate_trades[source].merge(values)
        for key in aggregate_books:
            aggregate_books[key] += int(report.get("books", {}).get(key, 0))
        instrument_report = report.get("instruments", {})
        aggregate_instruments["messages"] += int(instrument_report.get("messages", 0))
        aggregate_instruments["items"] += int(instrument_report.get("items", 0))
        aggregate_instruments["unique_instrument_ids"].update(item.instrument_ids)
        # All instrument IDs can be numerous; only BTC IDs need to be retained in the report.
        aggregate_instruments["btc_instrument_ids"].update(
            instrument_report.get("btc_instrument_ids", [])
        )
        aggregate_instruments["btc_usdt_swap_present"] = bool(
            aggregate_instruments["btc_usdt_swap_present"]
            or instrument_report.get("btc_usdt_swap_present")
        )

    duplicate_sessions = {
        session_id: paths
        for session_id, paths in sorted(session_paths.items())
        if len(paths) > 1
    }
    top_reasons = list(input_errors)
    if duplicate_sessions:
        top_reasons.append(
            _reason(
                "duplicate_session_files",
                "one session_id appears in multiple closed files",
                sessions=duplicate_sessions,
            )
        )

    valid = not top_reasons and all(report.get("valid", False) for report in reports)
    return {
        "report_schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "valid": valid,
        "reasons": top_reasons,
        "files": reports,
        "summary": {
            "file_count": len(reports),
            "valid_file_count": sum(bool(report.get("valid")) for report in reports),
            "session_count": len(session_paths),
            "compressed_bytes": compressed_bytes,
            "uncompressed_bytes": uncompressed_bytes,
            "records": record_count,
            "streams": dict(sorted(stream_summary.items())),
            "channels": dict(sorted(aggregate_channels.items())),
            "exchange_to_receive_lag": aggregate_lag.report(),
            "books": aggregate_books,
            "instruments": {
                "messages": aggregate_instruments["messages"],
                "items": aggregate_instruments["items"],
                "unique_instrument_count": len(
                    aggregate_instruments["unique_instrument_ids"]
                ),
                "btc_instrument_ids": sorted(aggregate_instruments["btc_instrument_ids"]),
                "btc_usdt_swap_present": aggregate_instruments["btc_usdt_swap_present"],
            },
            "trade_reconciliation": _trade_reconciliation(aggregate_trades),
        },
    }


def _atomic_write_json(path: Path, text: str) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_name = handle.name
            handle.write(text)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
        temporary_name = None
        directory_fd: int | None = None
        try:
            directory_fd = os.open(path.parent, os.O_RDONLY)
            os.fsync(directory_fd)
        except OSError:
            pass
        finally:
            if directory_fd is not None:
                os.close(directory_fd)
    finally:
        if temporary_name is not None:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="closed OKX WebSocket raw gzip群のsoak/quality reportを生成する"
    )
    parser.add_argument(
        "inputs",
        nargs="*",
        type=Path,
        default=[Path("data/raw/okx/ws")],
        help="*.jsonl.gz または再帰検索するdirectory（既定: data/raw/okx/ws）",
    )
    parser.add_argument("--output", type=Path, help="同じJSONをatomic writeする任意path")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    report = analyze_paths(args.inputs)
    text = json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2)
    print(text)
    if args.output is not None:
        _atomic_write_json(args.output, text)
    return 0 if report["valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

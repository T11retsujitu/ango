"""OKX WebSocket rawとREST instrument snapshotをimmutable Parquetへ正規化する。

入力は :class:`mce.stream_store.GzipJsonlStreamWriter` が保存したWS gzip JSONL、
またはcollectorが保存したREST instruments gzip JSONL。exchange timestamp と別に、
すべての行へ raw の到着座標
``(stream, session_id, frame_no, data_idx)`` を残す。板 level にはさらに
``(side, level_idx)`` を付けるため、raw payload へ常に戻れる。

Parquet はschema versionとlocal ``received_at_ns`` のUTC日時/hourでpartitionする。
1回の正規化で作ったshardは追記・置換せず、raw内容のSHA-256とchunk番号から決まる
名前でcreate-onlyにする。同じrawの再実行は同じ内容を検証して再利用する。
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import math
import os
import tempfile
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import polars as pl

from mce.okx_ws import BookSequenceGap, BooksSequenceValidator, WebSocketProtocolError


RAW_SCHEMA_VERSION = 1
REST_INSTRUMENT_RAW_SCHEMA_VERSION = 1
NORMALIZED_SCHEMA_VERSION = 3
DEFAULT_NORMALIZED_OUTPUT_ROOT = Path("data/normalized/okx/microstructure/v3")
_ROWS_PER_SHARD = 250_000
_TABLES = (
    "trades",
    "bbo",
    "book_messages",
    "book_levels",
    "instrument_metadata",
    "session_controls",
)

_MARKET_CHANNEL_STREAM = {
    "trades": "public",
    "trades-all": "business",
    "bbo-tbt": "public",
    "books": "public",
    "instruments": "public",
}
_KNOWN_WS_EVENTS = {"subscribe", "unsubscribe", "error", "notice"}


class MicrostructureNormalizationError(RuntimeError):
    """raw が壊れているか、immutable output と矛盾した。"""


class RawStreamFormatError(MicrostructureNormalizationError):
    """raw wrapper または認識対象の OKX payload が期待した形式ではない。"""


class ImmutableOutputError(MicrostructureNormalizationError):
    """同じ deterministic path に異なる Parquet が既に存在する。"""


class ContractConversionError(ValueError):
    """contract metadataから数量を一意かつ安全に換算できない。"""


@dataclass(frozen=True)
class NormalizationResult:
    raw_path: Path
    raw_archive_sha256: str
    raw_logical_sha256: str
    row_counts: dict[str, int]
    output_paths: tuple[Path, ...]

    @property
    def source_sha256(self) -> str:
        """v1呼び手向けalias。意味は展開後JSONLのlogical hash。"""

        return self.raw_logical_sha256


@dataclass(frozen=True)
class ContractQuantities:
    """SWAPの契約枚数、base数量、quote notionalをDecimalで保持する。"""

    contracts: Decimal
    base_qty: Decimal
    quote_notional: Decimal


def _conversion_decimal(value: Decimal | str | int, field: str) -> Decimal:
    # floatは呼び出し前に丸め誤差が入り得るため、換算境界では受け付けない。
    if isinstance(value, bool) or isinstance(value, float):
        raise ContractConversionError(
            f"{field} must be Decimal, string, or integer; got {type(value).__name__}"
        )
    try:
        result = value if isinstance(value, Decimal) else Decimal(value)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ContractConversionError(f"invalid {field}: {value!r}") from exc
    if not result.is_finite():
        raise ContractConversionError(f"non-finite {field}: {value!r}")
    return result


def convert_contract_quantity(
    *,
    contracts: Decimal | str | int,
    price: Decimal | str | int,
    ct_val: Decimal | str | int,
    ct_mult: Decimal | str | int,
    ct_val_ccy: str,
    ct_type: str,
    base_ccy: str,
    quote_ccy: str,
) -> ContractQuantities:
    """linear SWAPのcontract数量をbase量とquote notionalへ厳密換算する。

    v1で固定した二仕様だけを受け付ける。``ctValCcy == base_ccy``なら
    contract valueはbase建て、``ctValCcy == quote_ccy``ならquote建てとして
    換算する。inverse、未知currency、非正のmetadataは推測せず拒否する。
    """

    if ct_type != "linear":
        raise ContractConversionError(f"unsupported ct_type: {ct_type!r}")
    if (
        not isinstance(base_ccy, str)
        or not base_ccy
        or not isinstance(quote_ccy, str)
        or not quote_ccy
        or base_ccy == quote_ccy
    ):
        raise ContractConversionError("base_ccy and quote_ccy must be distinct strings")
    if not isinstance(ct_val_ccy, str) or not ct_val_ccy:
        raise ContractConversionError("ct_val_ccy must be a non-empty string")

    contracts_value = _conversion_decimal(contracts, "contracts")
    price_value = _conversion_decimal(price, "price")
    ct_val_value = _conversion_decimal(ct_val, "ct_val")
    ct_mult_value = _conversion_decimal(ct_mult, "ct_mult")
    if contracts_value < 0:
        raise ContractConversionError("contracts must be non-negative")
    if price_value <= 0:
        raise ContractConversionError("price must be positive")
    if ct_val_value <= 0:
        raise ContractConversionError("ct_val must be positive")
    if ct_mult_value <= 0:
        raise ContractConversionError("ct_mult must be positive")

    contract_value = contracts_value * ct_val_value * ct_mult_value
    if ct_val_ccy == base_ccy:
        base_qty = contract_value
        quote_notional = base_qty * price_value
    elif ct_val_ccy == quote_ccy:
        quote_notional = contract_value
        base_qty = quote_notional / price_value
    else:
        raise ContractConversionError(
            f"ct_val_ccy {ct_val_ccy!r} matches neither base {base_ccy!r} "
            f"nor quote {quote_ccy!r}"
        )
    return ContractQuantities(
        contracts=contracts_value,
        base_qty=base_qty,
        quote_notional=quote_notional,
    )


_COMMON_SCHEMA: dict[str, pl.DataType] = {
    "normalized_schema_version": pl.Int16,
    "raw_schema_version": pl.Int16,
    "raw_archive_sha256": pl.String,
    "raw_logical_sha256": pl.String,
    "stream": pl.String,
    "session_id": pl.String,
    "frame_no": pl.Int64,
    "data_idx": pl.Int32,
    "received_at_ns": pl.Int64,
    "monotonic_ns": pl.Int64,
    "channel": pl.String,
    "inst_id": pl.String,
    "is_post_ready": pl.Boolean,
}

_SCHEMAS: dict[str, dict[str, pl.DataType]] = {
    "trades": {
        **_COMMON_SCHEMA,
        "event_ts_ms": pl.Int64,
        "trade_id": pl.String,
        "px": pl.Float64,
        "sz": pl.Float64,
        "px_raw": pl.String,
        "sz_raw": pl.String,
        "side": pl.String,
        "count": pl.Int64,
        "seq_id": pl.Int64,
        "trade_source": pl.Int64,
    },
    "bbo": {
        **_COMMON_SCHEMA,
        "action": pl.String,
        "event_ts_ms": pl.Int64,
        "seq_id": pl.Int64,
        "bid_px": pl.Float64,
        "bid_sz": pl.Float64,
        "bid_px_raw": pl.String,
        "bid_sz_raw": pl.String,
        "bid_order_count": pl.Int64,
        "ask_px": pl.Float64,
        "ask_sz": pl.Float64,
        "ask_px_raw": pl.String,
        "ask_sz_raw": pl.String,
        "ask_order_count": pl.Int64,
    },
    "book_messages": {
        **_COMMON_SCHEMA,
        "action": pl.String,
        "event_ts_ms": pl.Int64,
        "prev_seq_id": pl.Int64,
        "seq_id": pl.Int64,
        "checksum": pl.Int64,
        "ask_level_count": pl.Int32,
        "bid_level_count": pl.Int32,
        "is_heartbeat": pl.Boolean,
        "is_sequence_reset": pl.Boolean,
    },
    "book_levels": {
        **_COMMON_SCHEMA,
        "action": pl.String,
        "event_ts_ms": pl.Int64,
        "prev_seq_id": pl.Int64,
        "seq_id": pl.Int64,
        "side": pl.String,
        "level_idx": pl.Int32,
        "px": pl.Float64,
        "sz": pl.Float64,
        "px_raw": pl.String,
        "sz_raw": pl.String,
        "liquidated_order_count": pl.Int64,
        "order_count": pl.Int64,
    },
    "instrument_metadata": {
        **_COMMON_SCHEMA,
        "origin": pl.String,
        "effective_received_at_ns": pl.Int64,
        "event_ts_ms": pl.Int64,
        "inst_type": pl.String,
        "inst_family": pl.String,
        "uly": pl.String,
        "ct_type": pl.String,
        "ct_val_raw": pl.String,
        "ct_mult_raw": pl.String,
        "ct_val_ccy": pl.String,
        "tick_sz_raw": pl.String,
        "lot_sz_raw": pl.String,
        "min_sz_raw": pl.String,
        "max_mkt_sz_raw": pl.String,
        "max_lmt_sz_raw": pl.String,
        "state": pl.String,
        "raw_item_json": pl.String,
    },
    "session_controls": {
        **_COMMON_SCHEMA,
        "direction": pl.String,
        "kind": pl.String,
        "control_type": pl.String,
        "event": pl.String,
        "code": pl.String,
        "message": pl.String,
        "payload": pl.String,
        "is_unrecognized": pl.Boolean,
        "unrecognized_reason": pl.String,
    },
}


def table_schema(table: str) -> dict[str, pl.DataType]:
    """公開用schema accessor。呼び手によるdict変更を内部へ波及させない。"""

    try:
        return dict(_SCHEMAS[table])
    except KeyError as exc:
        raise ValueError(f"unknown normalized table: {table!r}") from exc


def _format_error(raw_path: Path, line_no: int, message: str) -> RawStreamFormatError:
    return RawStreamFormatError(f"{raw_path}:{line_no}: {message}")


def _required_int(value: Any, field: str, raw_path: Path, line_no: int) -> int:
    if isinstance(value, bool):
        raise _format_error(raw_path, line_no, f"{field} must be an integer")
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise _format_error(raw_path, line_no, f"invalid {field}: {value!r}") from exc


def _optional_int(value: Any, field: str, raw_path: Path, line_no: int) -> int | None:
    if value is None or value == "":
        return None
    return _required_int(value, field, raw_path, line_no)


def _required_float(value: Any, field: str, raw_path: Path, line_no: int) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise _format_error(raw_path, line_no, f"invalid {field}: {value!r}") from exc
    if not math.isfinite(number):
        raise _format_error(raw_path, line_no, f"non-finite {field}: {value!r}")
    return number


def _required_text(value: Any, field: str, raw_path: Path, line_no: int) -> str:
    if not isinstance(value, str) or not value:
        raise _format_error(raw_path, line_no, f"{field} must be a non-empty string")
    return value


def _optional_raw_text(
    value: Any, field: str, raw_path: Path, line_no: int
) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise _format_error(raw_path, line_no, f"{field} must be a string or null")
    return value


def _archive_sha256(raw_path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with raw_path.open("rb") as source:
            while chunk := source.read(1024 * 1024):
                digest.update(chunk)
    except OSError as exc:
        raise RawStreamFormatError(f"failed to read raw archive {raw_path}") from exc
    return digest.hexdigest()


def _logical_sha256(raw_path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with gzip.open(raw_path, "rb") as source:
            while chunk := source.read(1024 * 1024):
                digest.update(chunk)
    except (OSError, EOFError) as exc:
        raise RawStreamFormatError(f"failed to read gzip raw stream {raw_path}") from exc
    return digest.hexdigest()


def _partition(received_at_ns: int) -> tuple[str, str]:
    seconds = received_at_ns // 1_000_000_000
    try:
        arrived = datetime.fromtimestamp(seconds, tz=timezone.utc)
    except (OverflowError, OSError, ValueError) as exc:
        raise RawStreamFormatError(f"received_at_ns is out of range: {received_at_ns}") from exc
    return arrived.strftime("%Y-%m-%d"), arrived.strftime("%H")


def _common(
    record: Mapping[str, Any],
    *,
    raw_archive_sha256: str,
    raw_logical_sha256: str,
    data_idx: int,
    channel: str | None,
    inst_id: str | None,
    is_post_ready: bool,
) -> dict[str, Any]:
    return {
        "normalized_schema_version": NORMALIZED_SCHEMA_VERSION,
        "raw_schema_version": RAW_SCHEMA_VERSION,
        "raw_archive_sha256": raw_archive_sha256,
        "raw_logical_sha256": raw_logical_sha256,
        "stream": record["stream"],
        "session_id": record["session_id"],
        "frame_no": record["frame_no"],
        "data_idx": data_idx,
        "received_at_ns": record["received_at_ns"],
        "monotonic_ns": record["monotonic_ns"],
        "channel": channel,
        "inst_id": inst_id,
        "is_post_ready": is_post_ready,
    }


def _depth_level(
    value: Any,
    *,
    field: str,
    raw_path: Path,
    line_no: int,
) -> tuple[str, str, float, float, int | None, int | None]:
    if not isinstance(value, list) or len(value) < 2:
        raise _format_error(raw_path, line_no, f"{field} level must have at least px and sz")
    px_raw = _required_text(value[0], f"{field}.px", raw_path, line_no)
    sz_raw = _required_text(value[1], f"{field}.sz", raw_path, line_no)
    liquidated = _optional_int(
        value[2] if len(value) > 2 else None,
        f"{field}.liquidated_order_count",
        raw_path,
        line_no,
    )
    orders = _optional_int(
        value[3] if len(value) > 3 else None,
        f"{field}.order_count",
        raw_path,
        line_no,
    )
    return (
        px_raw,
        sz_raw,
        _required_float(px_raw, f"{field}.px", raw_path, line_no),
        _required_float(sz_raw, f"{field}.sz", raw_path, line_no),
        liquidated,
        orders,
    )


def _normalize_trade(
    record: Mapping[str, Any],
    item: Mapping[str, Any],
    *,
    raw_archive_sha256: str,
    raw_logical_sha256: str,
    data_idx: int,
    channel: str,
    inst_id: str,
    raw_path: Path,
    line_no: int,
    is_post_ready: bool,
) -> dict[str, Any]:
    px_raw = _required_text(item.get("px"), "data.px", raw_path, line_no)
    sz_raw = _required_text(item.get("sz"), "data.sz", raw_path, line_no)
    side = _required_text(item.get("side"), "data.side", raw_path, line_no)
    if side not in {"buy", "sell"}:
        raise _format_error(raw_path, line_no, f"unsupported trade side: {side!r}")
    item_inst_id = item.get("instId")
    if item_inst_id is not None and item_inst_id != inst_id:
        raise _format_error(
            raw_path,
            line_no,
            f"arg.instId {inst_id!r} != data.instId {item_inst_id!r}",
        )
    return {
        **_common(
            record,
            raw_archive_sha256=raw_archive_sha256,
            raw_logical_sha256=raw_logical_sha256,
            data_idx=data_idx,
            channel=channel,
            inst_id=inst_id,
            is_post_ready=is_post_ready,
        ),
        "event_ts_ms": _required_int(item.get("ts"), "data.ts", raw_path, line_no),
        "trade_id": None if item.get("tradeId") is None else str(item["tradeId"]),
        "px": _required_float(px_raw, "data.px", raw_path, line_no),
        "sz": _required_float(sz_raw, "data.sz", raw_path, line_no),
        "px_raw": px_raw,
        "sz_raw": sz_raw,
        "side": side,
        "count": _optional_int(item.get("count"), "data.count", raw_path, line_no),
        "seq_id": _optional_int(item.get("seqId"), "data.seqId", raw_path, line_no),
        "trade_source": _optional_int(item.get("source"), "data.source", raw_path, line_no),
    }


def _normalize_bbo(
    record: Mapping[str, Any],
    item: Mapping[str, Any],
    *,
    raw_archive_sha256: str,
    raw_logical_sha256: str,
    data_idx: int,
    channel: str,
    inst_id: str,
    action: str,
    raw_path: Path,
    line_no: int,
    is_post_ready: bool,
) -> dict[str, Any]:
    asks = item.get("asks")
    bids = item.get("bids")
    if not isinstance(asks, list) or not isinstance(bids, list):
        raise _format_error(raw_path, line_no, "bbo data asks/bids must be arrays")
    ask = (
        _depth_level(asks[0], field="asks[0]", raw_path=raw_path, line_no=line_no)
        if asks
        else None
    )
    bid = (
        _depth_level(bids[0], field="bids[0]", raw_path=raw_path, line_no=line_no)
        if bids
        else None
    )
    if ask is None and bid is None:
        raise _format_error(raw_path, line_no, "bbo data cannot have both sides empty")
    return {
        **_common(
            record,
            raw_archive_sha256=raw_archive_sha256,
            raw_logical_sha256=raw_logical_sha256,
            data_idx=data_idx,
            channel=channel,
            inst_id=inst_id,
            is_post_ready=is_post_ready,
        ),
        "action": action,
        "event_ts_ms": _required_int(item.get("ts"), "data.ts", raw_path, line_no),
        "seq_id": _optional_int(item.get("seqId"), "data.seqId", raw_path, line_no),
        "bid_px": None if bid is None else bid[2],
        "bid_sz": None if bid is None else bid[3],
        "bid_px_raw": None if bid is None else bid[0],
        "bid_sz_raw": None if bid is None else bid[1],
        "bid_order_count": None if bid is None else bid[5],
        "ask_px": None if ask is None else ask[2],
        "ask_sz": None if ask is None else ask[3],
        "ask_px_raw": None if ask is None else ask[0],
        "ask_sz_raw": None if ask is None else ask[1],
        "ask_order_count": None if ask is None else ask[5],
    }


def _normalize_book(
    record: Mapping[str, Any],
    item: Mapping[str, Any],
    *,
    raw_archive_sha256: str,
    raw_logical_sha256: str,
    data_idx: int,
    channel: str,
    inst_id: str,
    action: str,
    raw_path: Path,
    line_no: int,
    is_post_ready: bool,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    asks = item.get("asks")
    bids = item.get("bids")
    if not isinstance(asks, list) or not isinstance(bids, list):
        raise _format_error(raw_path, line_no, "books data asks/bids must be arrays")
    event_ts_ms = _required_int(item.get("ts"), "data.ts", raw_path, line_no)
    prev_seq_id = _required_int(item.get("prevSeqId"), "data.prevSeqId", raw_path, line_no)
    seq_id = _required_int(item.get("seqId"), "data.seqId", raw_path, line_no)
    common = _common(
        record,
        raw_archive_sha256=raw_archive_sha256,
        raw_logical_sha256=raw_logical_sha256,
        data_idx=data_idx,
        channel=channel,
        inst_id=inst_id,
        is_post_ready=is_post_ready,
    )
    message = {
        **common,
        "action": action,
        "event_ts_ms": event_ts_ms,
        "prev_seq_id": prev_seq_id,
        "seq_id": seq_id,
        "checksum": _optional_int(item.get("checksum"), "data.checksum", raw_path, line_no),
        "ask_level_count": len(asks),
        "bid_level_count": len(bids),
        "is_heartbeat": action == "update" and seq_id == prev_seq_id and not asks and not bids,
        "is_sequence_reset": action == "update" and seq_id < prev_seq_id,
    }
    levels: list[dict[str, Any]] = []
    for side, values in (("ask", asks), ("bid", bids)):
        for level_idx, value in enumerate(values):
            px_raw, sz_raw, px, sz, liquidated, orders = _depth_level(
                value,
                field=f"{side}s[{level_idx}]",
                raw_path=raw_path,
                line_no=line_no,
            )
            levels.append(
                {
                    **common,
                    "action": action,
                    "event_ts_ms": event_ts_ms,
                    "prev_seq_id": prev_seq_id,
                    "seq_id": seq_id,
                    "side": side,
                    "level_idx": level_idx,
                    "px": px,
                    "sz": sz,
                    "px_raw": px_raw,
                    "sz_raw": sz_raw,
                    "liquidated_order_count": liquidated,
                    "order_count": orders,
                }
            )
    return message, levels


def _channel_coordinates(message: Mapping[str, Any]) -> tuple[str | None, str | None]:
    arg = message.get("arg")
    if not isinstance(arg, Mapping):
        return None, None
    channel = arg.get("channel")
    inst_id = arg.get("instId")
    return (
        channel if isinstance(channel, str) else None,
        inst_id if isinstance(inst_id, str) else None,
    )


def _control_row(
    record: Mapping[str, Any],
    *,
    raw_archive_sha256: str,
    raw_logical_sha256: str,
    is_post_ready: bool,
    control_type: str,
    message: Mapping[str, Any] | None = None,
    is_unrecognized: bool = False,
    unrecognized_reason: str | None = None,
) -> dict[str, Any]:
    channel, inst_id = _channel_coordinates(message or {})
    event = None if message is None else message.get("event")
    code = None if message is None else message.get("code")
    msg = None if message is None else message.get("msg")
    return {
        **_common(
            record,
            raw_archive_sha256=raw_archive_sha256,
            raw_logical_sha256=raw_logical_sha256,
            data_idx=-1,
            channel=channel,
            inst_id=inst_id,
            is_post_ready=is_post_ready,
        ),
        "direction": record["direction"],
        "kind": record["kind"],
        "control_type": control_type,
        "event": None if event is None else str(event),
        "code": None if code is None else str(code),
        "message": None if msg is None else str(msg),
        "payload": record["payload"],
        "is_unrecognized": is_unrecognized,
        "unrecognized_reason": unrecognized_reason,
    }


def _normalize_instrument(
    record: Mapping[str, Any],
    item: Mapping[str, Any],
    *,
    raw_archive_sha256: str,
    raw_logical_sha256: str,
    data_idx: int,
    arg_inst_type: str,
    raw_path: Path,
    line_no: int,
    is_post_ready: bool,
    origin: str,
) -> dict[str, Any]:
    inst_id = _required_text(item.get("instId"), "data.instId", raw_path, line_no)
    inst_type = _required_text(item.get("instType"), "data.instType", raw_path, line_no)
    if inst_type != arg_inst_type:
        raise _format_error(
            raw_path,
            line_no,
            f"arg.instType {arg_inst_type!r} != data.instType {inst_type!r}",
        )
    event_ts = item.get("uTime", item.get("ts"))
    return {
        **_common(
            record,
            raw_archive_sha256=raw_archive_sha256,
            raw_logical_sha256=raw_logical_sha256,
            data_idx=data_idx,
            channel="instruments",
            inst_id=inst_id,
            is_post_ready=is_post_ready,
        ),
        "origin": origin,
        # REST初期snapshotもWS更新も、ローカルでraw受信が完了した時点からのみ
        # causalに有効。exchange側uTimeをeffective timeへ昇格させない。
        "effective_received_at_ns": record["received_at_ns"],
        "event_ts_ms": _optional_int(event_ts, "data.uTime", raw_path, line_no),
        "inst_type": inst_type,
        "inst_family": _optional_raw_text(
            item.get("instFamily"), "data.instFamily", raw_path, line_no
        ),
        "uly": _optional_raw_text(item.get("uly"), "data.uly", raw_path, line_no),
        "ct_type": _required_text(item.get("ctType"), "data.ctType", raw_path, line_no),
        "ct_val_raw": _required_text(item.get("ctVal"), "data.ctVal", raw_path, line_no),
        "ct_mult_raw": _required_text(
            item.get("ctMult"), "data.ctMult", raw_path, line_no
        ),
        "ct_val_ccy": _required_text(
            item.get("ctValCcy"), "data.ctValCcy", raw_path, line_no
        ),
        "tick_sz_raw": _required_text(
            item.get("tickSz"), "data.tickSz", raw_path, line_no
        ),
        "lot_sz_raw": _required_text(item.get("lotSz"), "data.lotSz", raw_path, line_no),
        "min_sz_raw": _required_text(item.get("minSz"), "data.minSz", raw_path, line_no),
        "max_mkt_sz_raw": _required_text(
            item.get("maxMktSz"), "data.maxMktSz", raw_path, line_no
        ),
        "max_lmt_sz_raw": _optional_raw_text(
            item.get("maxLmtSz"), "data.maxLmtSz", raw_path, line_no
        ),
        "state": _required_text(item.get("state"), "data.state", raw_path, line_no),
        "raw_item_json": json.dumps(
            item, ensure_ascii=False, separators=(",", ":"), sort_keys=True
        ),
    }


def _validate_stream_channel(
    record: Mapping[str, Any], channel: str, raw_path: Path, line_no: int
) -> None:
    expected_stream = _MARKET_CHANNEL_STREAM[channel]
    if record["stream"] != expected_stream:
        raise _format_error(
            raw_path,
            line_no,
            f"channel {channel!r} requires stream {expected_stream!r}, "
            f"got {record['stream']!r}",
        )


def _payload_rows(
    record: Mapping[str, Any],
    *,
    raw_archive_sha256: str,
    raw_logical_sha256: str,
    raw_path: Path,
    line_no: int,
    is_post_ready: bool,
    book_validators: dict[str, BooksSequenceValidator],
) -> Iterable[tuple[str, dict[str, Any]]]:
    payload = record["payload"]
    try:
        message = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise _format_error(raw_path, line_no, "inbound frame payload is not JSON") from exc
    if not isinstance(message, Mapping):
        raise _format_error(raw_path, line_no, "inbound JSON payload must be an object")

    if "event" in message:
        event = message.get("event")
        channel, _ = _channel_coordinates(message)
        if channel in _MARKET_CHANNEL_STREAM:
            _validate_stream_channel(record, channel, raw_path, line_no)
        event_arg = message.get("arg")
        event_channel_value = (
            event_arg.get("channel") if isinstance(event_arg, Mapping) else None
        )
        unknown_event = not isinstance(event, str) or event not in _KNOWN_WS_EVENTS
        unknown_channel = event_channel_value is not None and (
            not isinstance(event_channel_value, str)
            or event_channel_value not in _MARKET_CHANNEL_STREAM
        )
        unrecognized = unknown_event or unknown_channel
        reasons: list[str] = []
        if unknown_event:
            reasons.append(f"unknown websocket event: {event!r}")
        if unknown_channel:
            reasons.append(f"unknown websocket event channel: {event_channel_value!r}")
        yield (
            "session_controls",
            _control_row(
                record,
                raw_archive_sha256=raw_archive_sha256,
                raw_logical_sha256=raw_logical_sha256,
                is_post_ready=is_post_ready,
                control_type="ws_event" if not unrecognized else "unrecognized",
                message=message,
                is_unrecognized=unrecognized,
                unrecognized_reason=None if not reasons else "; ".join(reasons),
            ),
        )
        return

    arg = message.get("arg")
    if not isinstance(arg, Mapping):
        yield (
            "session_controls",
            _control_row(
                record,
                raw_archive_sha256=raw_archive_sha256,
                raw_logical_sha256=raw_logical_sha256,
                is_post_ready=is_post_ready,
                control_type="unrecognized",
                message=message,
                is_unrecognized=True,
                unrecognized_reason="inbound JSON frame has no arg object",
            ),
        )
        return

    channel_value = arg.get("channel")
    if not isinstance(channel_value, str) or channel_value not in _MARKET_CHANNEL_STREAM:
        yield (
            "session_controls",
            _control_row(
                record,
                raw_archive_sha256=raw_archive_sha256,
                raw_logical_sha256=raw_logical_sha256,
                is_post_ready=is_post_ready,
                control_type="unrecognized",
                message=message,
                is_unrecognized=True,
                unrecognized_reason=f"unknown inbound channel: {channel_value!r}",
            ),
        )
        return
    channel = channel_value
    _validate_stream_channel(record, channel, raw_path, line_no)

    data = message.get("data")
    if not isinstance(data, list):
        raise _format_error(raw_path, line_no, f"{channel} data must be an array")
    if not data:
        yield (
            "session_controls",
            _control_row(
                record,
                raw_archive_sha256=raw_archive_sha256,
                raw_logical_sha256=raw_logical_sha256,
                is_post_ready=is_post_ready,
                control_type="empty_market_data",
                message=message,
            ),
        )
        return

    if channel == "instruments":
        arg_inst_type = _required_text(
            arg.get("instType"), "arg.instType", raw_path, line_no
        )
        if arg_inst_type != "SWAP":
            raise _format_error(
                raw_path, line_no, f"unsupported instruments instType: {arg_inst_type!r}"
            )
        for data_idx, item in enumerate(data):
            if not isinstance(item, Mapping):
                raise _format_error(raw_path, line_no, f"data[{data_idx}] must be an object")
            yield (
                "instrument_metadata",
                _normalize_instrument(
                    record,
                    item,
                    raw_archive_sha256=raw_archive_sha256,
                    raw_logical_sha256=raw_logical_sha256,
                    data_idx=data_idx,
                    arg_inst_type=arg_inst_type,
                    raw_path=raw_path,
                    line_no=line_no,
                    is_post_ready=is_post_ready,
                    origin="ws",
                ),
            )
        return

    inst_id = _required_text(arg.get("instId"), "arg.instId", raw_path, line_no)
    action_value = message.get("action")
    if channel == "bbo-tbt" and action_value is None:
        # OKX の bbo-tbt は各pushがfull snapshotで、実payloadには action がない。
        action = "snapshot"
    elif channel in {"bbo-tbt", "books"}:
        action = _required_text(action_value, "action", raw_path, line_no)
        if action not in {"snapshot", "update"}:
            raise _format_error(raw_path, line_no, f"unsupported {channel} action: {action!r}")
    else:
        action = ""

    for data_idx, item in enumerate(data):
        if not isinstance(item, Mapping):
            raise _format_error(raw_path, line_no, f"data[{data_idx}] must be an object")
        if channel in {"trades", "trades-all"}:
            yield (
                "trades",
                _normalize_trade(
                    record,
                    item,
                    raw_archive_sha256=raw_archive_sha256,
                    raw_logical_sha256=raw_logical_sha256,
                    data_idx=data_idx,
                    channel=channel,
                    inst_id=inst_id,
                    raw_path=raw_path,
                    line_no=line_no,
                    is_post_ready=is_post_ready,
                ),
            )
        elif channel == "bbo-tbt":
            yield (
                "bbo",
                _normalize_bbo(
                    record,
                    item,
                    raw_archive_sha256=raw_archive_sha256,
                    raw_logical_sha256=raw_logical_sha256,
                    data_idx=data_idx,
                    channel=channel,
                    inst_id=inst_id,
                    action=action,
                    raw_path=raw_path,
                    line_no=line_no,
                    is_post_ready=is_post_ready,
                ),
            )
        else:
            try:
                book_validators.setdefault(inst_id, BooksSequenceValidator()).observe(
                    action, item
                )
            except (BookSequenceGap, WebSocketProtocolError) as exc:
                raise _format_error(
                    raw_path, line_no, f"invalid books sequence: {exc}"
                ) from exc
            book_message, levels = _normalize_book(
                record,
                item,
                raw_archive_sha256=raw_archive_sha256,
                raw_logical_sha256=raw_logical_sha256,
                data_idx=data_idx,
                channel=channel,
                inst_id=inst_id,
                action=action,
                raw_path=raw_path,
                line_no=line_no,
                is_post_ready=is_post_ready,
            )
            yield "book_messages", book_message
            for level in levels:
                yield "book_levels", level


class _StagedShardWriter:
    def __init__(
        self, output_root: Path, staging_root: Path, raw_archive_sha256: str
    ) -> None:
        self.output_root = output_root
        self.staging_root = staging_root
        self.raw_archive_sha256 = raw_archive_sha256
        self.buffers: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
        self.chunk_numbers: dict[tuple[str, str, str], int] = defaultdict(int)
        self.staged: list[tuple[Path, Path]] = []
        self.row_counts = {table: 0 for table in _TABLES}

    def add(self, table: str, row: dict[str, Any]) -> None:
        date, hour = _partition(row["received_at_ns"])
        key = (table, date, hour)
        self.buffers[key].append(row)
        self.row_counts[table] += 1
        if len(self.buffers[key]) >= _ROWS_PER_SHARD:
            self._flush(key)

    def _flush(self, key: tuple[str, str, str]) -> None:
        rows = self.buffers[key]
        if not rows:
            return
        table, date, hour = key
        chunk_number = self.chunk_numbers[key]
        self.chunk_numbers[key] += 1
        filename = (
            f"part-v{NORMALIZED_SCHEMA_VERSION}-{self.raw_archive_sha256}-"
            f"{chunk_number:06d}.parquet"
        )
        staged_path = self.staging_root / f"{table}-{date}-{hour}-{filename}"
        final_path = (
            self.output_root
            / table
            / f"schema_version={NORMALIZED_SCHEMA_VERSION}"
            / f"arrival_date={date}"
            / f"arrival_hour={hour}"
            / filename
        )
        frame = pl.DataFrame(rows, schema=_SCHEMAS[table], strict=True)
        frame.write_parquet(staged_path, compression="zstd", statistics=True)
        self.staged.append((staged_path, final_path))
        rows.clear()

    def finish_staging(self) -> None:
        for key in sorted(self.buffers):
            self._flush(key)

    @staticmethod
    def _same_parquet(staged_path: Path, final_path: Path) -> bool:
        staged = pl.read_parquet(staged_path)
        existing = pl.read_parquet(final_path)
        return existing.columns == staged.columns and existing.equals(staged, null_equal=True)

    def commit(self) -> tuple[Path, ...]:
        # 先に全 collision を検証し、矛盾時に一部だけ追加するのを避ける。
        for staged_path, final_path in self.staged:
            if final_path.exists() and not self._same_parquet(staged_path, final_path):
                raise ImmutableOutputError(
                    f"immutable normalized shard differs from existing file: {final_path}"
                )

        outputs: list[Path] = []
        for staged_path, final_path in self.staged:
            final_path.parent.mkdir(parents=True, exist_ok=True)
            if not final_path.exists():
                try:
                    os.link(staged_path, final_path)
                except FileExistsError:
                    if not self._same_parquet(staged_path, final_path):
                        raise ImmutableOutputError(
                            f"concurrent immutable shard collision: {final_path}"
                        )
            outputs.append(final_path)
        return tuple(sorted(outputs))


def _validated_record(
    value: Any,
    *,
    raw_path: Path,
    line_no: int,
    expected_frame_no: int,
    expected_stream: str | None,
    expected_session_id: str | None,
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise _format_error(raw_path, line_no, "raw JSONL row must be an object")
    schema_version = _required_int(
        value.get("schema_version"), "schema_version", raw_path, line_no
    )
    if schema_version != RAW_SCHEMA_VERSION:
        raise _format_error(
            raw_path,
            line_no,
            f"unsupported raw schema_version {schema_version}; expected {RAW_SCHEMA_VERSION}",
        )
    stream = _required_text(value.get("stream"), "stream", raw_path, line_no)
    if stream not in {"public", "business"}:
        raise _format_error(raw_path, line_no, f"unsupported stream: {stream!r}")
    session_id = _required_text(value.get("session_id"), "session_id", raw_path, line_no)
    frame_no = _required_int(value.get("frame_no"), "frame_no", raw_path, line_no)
    if frame_no != expected_frame_no:
        raise _format_error(
            raw_path,
            line_no,
            f"frame_no discontinuity: got {frame_no}, expected {expected_frame_no}",
        )
    if expected_stream is not None and stream != expected_stream:
        raise _format_error(raw_path, line_no, "stream changed inside one raw file")
    if expected_session_id is not None and session_id != expected_session_id:
        raise _format_error(raw_path, line_no, "session_id changed inside one raw file")
    direction = _required_text(value.get("direction"), "direction", raw_path, line_no)
    if direction not in {"in", "out", "local"}:
        raise _format_error(raw_path, line_no, f"unsupported direction: {direction!r}")
    kind = _required_text(value.get("kind"), "kind", raw_path, line_no)
    payload = value.get("payload")
    if not isinstance(payload, str):
        raise _format_error(raw_path, line_no, "payload must be a string")
    return {
        "schema_version": schema_version,
        "stream": stream,
        "session_id": session_id,
        "frame_no": frame_no,
        "direction": direction,
        "kind": kind,
        "received_at_ns": _required_int(
            value.get("received_at_ns"), "received_at_ns", raw_path, line_no
        ),
        "monotonic_ns": _required_int(
            value.get("monotonic_ns"), "monotonic_ns", raw_path, line_no
        ),
        "payload": payload,
    }


def _normalize_ws_raw_file(raw_path: Path, output_root: Path) -> NormalizationResult:
    """閉じたWebSocket raw session 1ファイルを正規化する。

    gzip archive bytesと展開後JSONLの両hashを別々に固定する。途中で
    ファイルが変化した場合はhash不一致で失敗し、stagingしたParquetは
    公開しない。collectorが書き込み中のファイルではなく、close済みのsessionを
    渡すこと。
    """

    raw_path = Path(raw_path)
    output_root = Path(output_root)
    raw_archive_sha256 = _archive_sha256(raw_path)
    raw_logical_sha256 = _logical_sha256(raw_path)
    output_root.mkdir(parents=True, exist_ok=True)
    staging_parent = output_root / ".staging"
    staging_parent.mkdir(parents=True, exist_ok=True)

    second_digest = hashlib.sha256()
    with tempfile.TemporaryDirectory(prefix="normalize-", dir=staging_parent) as stage_name:
        writer = _StagedShardWriter(
            output_root, Path(stage_name), raw_archive_sha256
        )
        expected_frame_no = 0
        expected_stream: str | None = None
        expected_session_id: str | None = None
        session_ready = False
        book_validators: dict[str, BooksSequenceValidator] = {}
        try:
            with gzip.open(raw_path, "rb") as source:
                for line_no, raw_line in enumerate(source, start=1):
                    second_digest.update(raw_line)
                    try:
                        line = raw_line.decode("utf-8")
                    except UnicodeDecodeError as exc:
                        raise _format_error(raw_path, line_no, "raw JSONL is not UTF-8") from exc
                    if not line.strip():
                        raise _format_error(raw_path, line_no, "blank JSONL row")
                    try:
                        value = json.loads(line)
                    except json.JSONDecodeError as exc:
                        raise _format_error(raw_path, line_no, "invalid raw JSONL") from exc
                    record = _validated_record(
                        value,
                        raw_path=raw_path,
                        line_no=line_no,
                        expected_frame_no=expected_frame_no,
                        expected_stream=expected_stream,
                        expected_session_id=expected_session_id,
                    )
                    expected_frame_no += 1
                    expected_stream = record["stream"]
                    expected_session_id = record["session_id"]

                    row_is_post_ready = session_ready
                    if (
                        record["direction"] == "local"
                        and record["kind"] == "subscriptions_ready"
                    ):
                        if session_ready:
                            raise _format_error(
                                raw_path,
                                line_no,
                                "duplicate local subscriptions_ready event",
                            )
                        session_ready = True
                        row_is_post_ready = True

                    if record["direction"] != "in" or record["kind"] != "frame":
                        parsed_control: Mapping[str, Any] | None = None
                        try:
                            parsed_value = json.loads(record["payload"])
                            if isinstance(parsed_value, Mapping):
                                parsed_control = parsed_value
                        except json.JSONDecodeError:
                            pass
                        unexpected_inbound = (
                            record["direction"] == "in" and record["kind"] != "pong"
                        )
                        writer.add(
                            "session_controls",
                            _control_row(
                                record,
                                raw_archive_sha256=raw_archive_sha256,
                                raw_logical_sha256=raw_logical_sha256,
                                is_post_ready=row_is_post_ready,
                                control_type=f"{record['direction']}:{record['kind']}",
                                message=parsed_control,
                                is_unrecognized=unexpected_inbound,
                                unrecognized_reason=(
                                    f"unexpected inbound kind: {record['kind']!r}"
                                    if unexpected_inbound
                                    else None
                                ),
                            ),
                        )
                        continue
                    for table, row in _payload_rows(
                        record,
                        raw_archive_sha256=raw_archive_sha256,
                        raw_logical_sha256=raw_logical_sha256,
                        raw_path=raw_path,
                        line_no=line_no,
                        is_post_ready=row_is_post_ready,
                        book_validators=book_validators,
                    ):
                        writer.add(table, row)
        except (OSError, EOFError) as exc:
            raise RawStreamFormatError(f"failed while parsing gzip raw stream {raw_path}") from exc

        if second_digest.hexdigest() != raw_logical_sha256:
            raise RawStreamFormatError(
                f"raw logical stream changed between hash and parse passes: {raw_path}"
            )
        if _archive_sha256(raw_path) != raw_archive_sha256:
            raise RawStreamFormatError(
                f"raw archive changed between hash and parse passes: {raw_path}"
            )
        writer.finish_staging()
        output_paths = writer.commit()
        row_counts = dict(writer.row_counts)

    return NormalizationResult(
        raw_path=raw_path,
        raw_archive_sha256=raw_archive_sha256,
        raw_logical_sha256=raw_logical_sha256,
        row_counts=row_counts,
        output_paths=output_paths,
    )


def _validated_rest_instrument_record(
    value: Any,
    *,
    raw_path: Path,
    line_no: int,
) -> tuple[dict[str, Any], Mapping[str, Any], str]:
    """collectorのREST instruments envelopeを検証して共通座標へ写す。"""

    if not isinstance(value, Mapping):
        raise _format_error(raw_path, line_no, "REST raw JSONL row must be an object")
    schema_version = _required_int(
        value.get("schema_version"), "schema_version", raw_path, line_no
    )
    if schema_version != REST_INSTRUMENT_RAW_SCHEMA_VERSION:
        raise _format_error(
            raw_path,
            line_no,
            f"unsupported REST raw schema_version {schema_version}; "
            f"expected {REST_INSTRUMENT_RAW_SCHEMA_VERSION}",
        )
    if value.get("source") != "okx":
        raise _format_error(raw_path, line_no, f"unsupported REST source: {value.get('source')!r}")
    if value.get("endpoint") != "/api/v5/public/instruments":
        raise _format_error(
            raw_path, line_no, f"unsupported REST endpoint: {value.get('endpoint')!r}"
        )
    request = value.get("request")
    if not isinstance(request, Mapping):
        raise _format_error(raw_path, line_no, "REST request must be an object")
    if request.get("instType") != "SWAP":
        raise _format_error(
            raw_path,
            line_no,
            f"unsupported REST request instType: {request.get('instType')!r}",
        )
    requested_inst_id = _required_text(
        request.get("instId"), "request.instId", raw_path, line_no
    )

    response = value.get("response")
    if not isinstance(response, Mapping):
        raise _format_error(raw_path, line_no, "REST response must be an object")
    if str(response.get("code")) != "0":
        raise _format_error(
            raw_path,
            line_no,
            f"REST instruments response failed: {response.get('code')!r} "
            f"{response.get('msg')!r}",
        )
    data = response.get("data")
    if not isinstance(data, list):
        raise _format_error(raw_path, line_no, "REST response.data must be an array")
    if len(data) != 1 or not isinstance(data[0], Mapping):
        raise _format_error(
            raw_path,
            line_no,
            "REST instruments response must contain exactly one instrument object",
        )
    item = data[0]
    if item.get("instId") != requested_inst_id:
        raise _format_error(
            raw_path,
            line_no,
            f"request.instId {requested_inst_id!r} != data.instId {item.get('instId')!r}",
        )
    received_at_ns = _required_int(
        value.get("received_at_ns"), "received_at_ns", raw_path, line_no
    )
    if received_at_ns <= 0:
        raise _format_error(raw_path, line_no, "received_at_ns must be positive")
    record = {
        "schema_version": schema_version,
        "stream": "rest",
        # raw archive hashは呼出し側で決まるため、ここではpath由来でなく後で上書きする。
        "session_id": "",
        "frame_no": 0,
        "direction": "in",
        "kind": "rest_response",
        "received_at_ns": received_at_ns,
        "monotonic_ns": None,
        "payload": "",
    }
    return record, item, requested_inst_id


def normalize_rest_instrument_file(
    raw_path: Path, output_root: Path
) -> NormalizationResult:
    """閉じたREST ``public/instruments`` raw 1ファイルをcanonical化する。

    REST snapshotは成功responseをローカル受信した時点から有効な初期stateとする。
    gzip archive bytesと展開後JSONLは別hashで固定し、1 row以外は拒否する。
    """

    raw_path = Path(raw_path)
    output_root = Path(output_root)
    raw_archive_sha256 = _archive_sha256(raw_path)
    raw_logical_sha256 = _logical_sha256(raw_path)
    output_root.mkdir(parents=True, exist_ok=True)
    staging_parent = output_root / ".staging"
    staging_parent.mkdir(parents=True, exist_ok=True)

    second_digest = hashlib.sha256()
    with tempfile.TemporaryDirectory(prefix="normalize-", dir=staging_parent) as stage_name:
        writer = _StagedShardWriter(output_root, Path(stage_name), raw_archive_sha256)
        row_count = 0
        try:
            with gzip.open(raw_path, "rb") as source:
                for line_no, raw_line in enumerate(source, start=1):
                    second_digest.update(raw_line)
                    if row_count:
                        raise _format_error(
                            raw_path,
                            line_no,
                            "REST instruments archive must contain exactly one JSONL row",
                        )
                    try:
                        line = raw_line.decode("utf-8")
                    except UnicodeDecodeError as exc:
                        raise _format_error(raw_path, line_no, "raw JSONL is not UTF-8") from exc
                    if not line.strip():
                        raise _format_error(raw_path, line_no, "blank JSONL row")
                    try:
                        value = json.loads(line)
                    except json.JSONDecodeError as exc:
                        raise _format_error(raw_path, line_no, "invalid raw JSONL") from exc
                    record, item, _ = _validated_rest_instrument_record(
                        value, raw_path=raw_path, line_no=line_no
                    )
                    record["session_id"] = f"rest-{raw_archive_sha256}"
                    writer.add(
                        "instrument_metadata",
                        _normalize_instrument(
                            record,
                            item,
                            raw_archive_sha256=raw_archive_sha256,
                            raw_logical_sha256=raw_logical_sha256,
                            data_idx=0,
                            arg_inst_type="SWAP",
                            raw_path=raw_path,
                            line_no=line_no,
                            is_post_ready=True,
                            origin="rest",
                        ),
                    )
                    row_count += 1
        except (OSError, EOFError) as exc:
            raise RawStreamFormatError(f"failed while parsing gzip raw stream {raw_path}") from exc

        if row_count != 1:
            raise RawStreamFormatError(
                f"{raw_path}: REST instruments archive must contain exactly one JSONL row"
            )
        if second_digest.hexdigest() != raw_logical_sha256:
            raise RawStreamFormatError(
                f"raw logical stream changed between hash and parse passes: {raw_path}"
            )
        if _archive_sha256(raw_path) != raw_archive_sha256:
            raise RawStreamFormatError(
                f"raw archive changed between hash and parse passes: {raw_path}"
            )
        writer.finish_staging()
        output_paths = writer.commit()
        row_counts = dict(writer.row_counts)

    return NormalizationResult(
        raw_path=raw_path,
        raw_archive_sha256=raw_archive_sha256,
        raw_logical_sha256=raw_logical_sha256,
        row_counts=row_counts,
        output_paths=output_paths,
    )


def _raw_archive_kind(raw_path: Path) -> str:
    """gzip JSONLの先頭envelopeからWS/RESTをfail-closed判定する。"""

    try:
        with gzip.open(raw_path, "rt", encoding="utf-8") as source:
            line = source.readline()
    except (OSError, EOFError, UnicodeDecodeError) as exc:
        raise RawStreamFormatError(f"failed to read gzip raw stream {raw_path}") from exc
    if not line:
        raise RawStreamFormatError(f"{raw_path}: empty raw JSONL archive")
    try:
        value = json.loads(line)
    except json.JSONDecodeError as exc:
        raise RawStreamFormatError(f"{raw_path}:1: invalid raw JSONL") from exc
    if not isinstance(value, Mapping):
        raise RawStreamFormatError(f"{raw_path}:1: raw JSONL row must be an object")
    is_ws = "stream" in value or "session_id" in value or "frame_no" in value
    is_rest = "endpoint" in value or "request" in value or "response" in value
    if is_ws == is_rest:
        raise RawStreamFormatError(
            f"{raw_path}:1: raw envelope is ambiguous or unsupported"
        )
    return "ws" if is_ws else "rest"


def normalize_raw_file(raw_path: Path, output_root: Path) -> NormalizationResult:
    """閉じたWS sessionまたはREST instruments snapshotを自動判定して正規化する。"""

    raw_path = Path(raw_path)
    if _raw_archive_kind(raw_path) == "rest":
        return normalize_rest_instrument_file(raw_path, output_root)
    return _normalize_ws_raw_file(raw_path, output_root)


def normalize_raw_files(
    raw_paths: Iterable[Path], output_root: Path
) -> tuple[NormalizationResult, ...]:
    """複数の閉じたsessionを入力順に正規化する。"""

    return tuple(normalize_raw_file(path, output_root) for path in raw_paths)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("raw_paths", nargs="+", type=Path, help="closed .jsonl.gz session files")
    parser.add_argument(
        "--output-root",
        type=Path,
        # v2 smoke shardとschemaの異なるv3を同一dataset rootへ混在させない。
        default=DEFAULT_NORMALIZED_OUTPUT_ROOT,
    )
    return parser


def main() -> None:
    args = _parser().parse_args()
    results = normalize_raw_files(args.raw_paths, args.output_root)
    for result in results:
        counts = " ".join(f"{table}={result.row_counts[table]}" for table in _TABLES)
        print(
            f"{result.raw_path} archive_sha256={result.raw_archive_sha256} "
            f"logical_sha256={result.raw_logical_sha256} {counts}"
        )


if __name__ == "__main__":
    main()

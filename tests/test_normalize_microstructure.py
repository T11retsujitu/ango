import gzip
import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import polars as pl
import pytest

from mce.normalize_microstructure import (
    ContractConversionError,
    DEFAULT_NORMALIZED_OUTPUT_ROOT,
    ImmutableOutputError,
    NORMALIZED_SCHEMA_VERSION,
    RawStreamFormatError,
    convert_contract_quantity,
    normalize_raw_file,
    normalize_rest_instrument_file,
    table_schema,
)


INST_ID = "BTC-USDT-SWAP"


def _arrival_ns(hour: int, minute: int = 0) -> int:
    value = datetime(2026, 8, 16, hour, minute, tzinfo=timezone.utc)
    return int(value.timestamp()) * 1_000_000_000


def _raw_record(
    frame_no: int,
    payload,
    *,
    stream: str = "public",
    direction: str = "in",
    kind: str = "frame",
    received_at_ns: int | None = None,
):
    if not isinstance(payload, str):
        payload = json.dumps(payload, separators=(",", ":"))
    return {
        "schema_version": 1,
        "stream": stream,
        "session_id": f"fixture-{stream}",
        "frame_no": frame_no,
        "direction": direction,
        "kind": kind,
        "received_at_ns": received_at_ns or _arrival_ns(3, frame_no),
        "monotonic_ns": 10_000 + frame_no,
        "payload": payload,
    }


def _write_raw(path: Path, rows) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8", newline="\n") as output:
        for row in rows:
            output.write(json.dumps(row, separators=(",", ":")))
            output.write("\n")
    return path


def _instrument_item(**overrides):
    item = {
        "instId": INST_ID,
        "instType": "SWAP",
        "instFamily": "BTC-USDT",
        "uly": "BTC-USDT",
        "ctType": "linear",
        "ctVal": "0.01",
        "ctMult": "1",
        "ctValCcy": "BTC",
        "tickSz": "0.1",
        "lotSz": "0.01",
        "minSz": "0.01",
        "maxMktSz": "35000",
        "maxLmtSz": "100000000",
        "state": "live",
    }
    item.update(overrides)
    return item


def _rest_instrument_record(*, received_at_ns=None, item=None, code="0"):
    return {
        "schema_version": 1,
        "source": "okx",
        "endpoint": "/api/v5/public/instruments",
        "request": {"instType": "SWAP", "instId": INST_ID},
        "received_at_ns": received_at_ns or _arrival_ns(5, 30),
        "response": {
            "code": code,
            "data": [_instrument_item() if item is None else item],
            "msg": "",
        },
    }


def _read_table(root: Path, table: str) -> pl.DataFrame:
    paths = sorted((root / table).glob("**/*.parquet"))
    assert paths, f"no output for {table}"
    return pl.concat([pl.read_parquet(path) for path in paths], how="vertical")


def test_normalizes_public_trades_bbo_and_book_with_raw_coordinates(tmp_path):
    rows = [
        _raw_record(0, {"op": "subscribe"}, direction="out", kind="subscribe"),
        _raw_record(
            1,
            {"event": "subscribe", "arg": {"channel": "trades", "instId": INST_ID}},
        ),
        _raw_record(
            2,
            {
                "arg": {"channel": "trades", "instId": INST_ID},
                "data": [
                    {
                        "instId": INST_ID,
                        "tradeId": "1001",
                        "px": "62000.10",
                        "sz": "2",
                        "side": "buy",
                        "ts": "1786850100001",
                        "count": "3",
                        "seqId": "90",
                        "source": "0",
                    },
                    {
                        "instId": INST_ID,
                        "tradeId": "1002",
                        "px": "62000.20",
                        "sz": "1.5",
                        "side": "sell",
                        "ts": "1786850100002",
                    },
                ],
            },
        ),
        _raw_record(
            3,
            {"subscriptions": [{"channel": "trades", "instId": INST_ID}]},
            direction="local",
            kind="subscriptions_ready",
        ),
        _raw_record(4, "pong", kind="pong"),
        _raw_record(
            5,
            {
                "arg": {"channel": "bbo-tbt", "instId": INST_ID},
                "data": [
                    {
                        "asks": [["62001.0", "3", "0", "2"]],
                        "bids": [["62000.0", "4", "0", "5"]],
                        "ts": "1786850100010",
                        "seqId": "91",
                    }
                ],
            },
        ),
        _raw_record(
            6,
            {
                "arg": {"channel": "books", "instId": INST_ID},
                "action": "snapshot",
                "data": [
                    {
                        "asks": [
                            ["62001.0", "3", "0", "2"],
                            ["62002.0", "0", "0", "0"],
                        ],
                        "bids": [["62000.0", "4", "0", "5"]],
                        "ts": "1786850100020",
                        "prevSeqId": "-1",
                        "seqId": "92",
                        "checksum": "0",
                    }
                ],
            },
        ),
        _raw_record(7, {"reason": "fixture"}, direction="local", kind="disconnected"),
    ]
    raw_path = _write_raw(tmp_path / "raw" / "public.jsonl.gz", rows)
    output_root = tmp_path / "normalized"

    result = normalize_raw_file(raw_path, output_root)

    assert result.row_counts == {
        "trades": 2,
        "bbo": 1,
        "book_messages": 1,
        "book_levels": 3,
        "instrument_metadata": 0,
        "session_controls": 5,
    }
    assert len(result.output_paths) == 5
    assert all(
        "arrival_date=2026-08-16/arrival_hour=03" in str(path)
        for path in result.output_paths
    )
    assert all(
        path.name.startswith(f"part-v3-{result.raw_archive_sha256}-")
        for path in result.output_paths
    )
    assert all("schema_version=3" in str(path) for path in result.output_paths)
    assert NORMALIZED_SCHEMA_VERSION == 3
    assert DEFAULT_NORMALIZED_OUTPUT_ROOT.name == "v3"

    trades = _read_table(output_root, "trades")
    assert trades.schema == table_schema("trades")
    assert trades["frame_no"].to_list() == [2, 2]
    assert trades["data_idx"].to_list() == [0, 1]
    assert trades["channel"].to_list() == ["trades", "trades"]
    assert trades["trade_id"].to_list() == ["1001", "1002"]
    assert trades["px_raw"].to_list() == ["62000.10", "62000.20"]
    assert trades["count"].to_list() == [3, None]
    assert trades["raw_archive_sha256"].unique().to_list() == [
        result.raw_archive_sha256
    ]
    assert trades["raw_logical_sha256"].unique().to_list() == [
        result.raw_logical_sha256
    ]
    assert result.raw_archive_sha256 != result.raw_logical_sha256
    assert trades["is_post_ready"].to_list() == [False, False]

    bbo = _read_table(output_root, "bbo").row(0, named=True)
    assert bbo["frame_no"] == 5
    # OKXの実bbo-tbt payloadにはactionがないが、各pushはfull snapshot。
    assert bbo["action"] == "snapshot"
    assert bbo["bid_px"] == 62000.0
    assert bbo["bid_px_raw"] == "62000.0"
    assert bbo["bid_sz_raw"] == "4"
    assert bbo["bid_order_count"] == 5
    assert bbo["ask_px"] == 62001.0
    assert bbo["ask_px_raw"] == "62001.0"
    assert bbo["ask_sz_raw"] == "3"
    assert bbo["is_post_ready"]

    book_message = _read_table(output_root, "book_messages").row(0, named=True)
    assert book_message["prev_seq_id"] == -1
    assert book_message["seq_id"] == 92
    assert book_message["ask_level_count"] == 2
    assert book_message["bid_level_count"] == 1
    assert not book_message["is_heartbeat"]

    levels = _read_table(output_root, "book_levels")
    assert levels.select("frame_no", "data_idx").unique().row(0) == (6, 0)
    assert levels["side"].to_list() == ["ask", "ask", "bid"]
    assert levels["level_idx"].to_list() == [0, 1, 0]
    # size=0 のdelete deltaも落とさない。
    assert levels["sz"].to_list() == [3.0, 0.0, 4.0]

    controls = _read_table(output_root, "session_controls").sort("frame_no")
    assert controls["frame_no"].to_list() == [0, 1, 3, 4, 7]
    assert controls["is_unrecognized"].to_list() == [False] * 5
    assert controls.filter(pl.col("kind") == "subscriptions_ready")[
        "is_post_ready"
    ].item()


def test_trades_all_and_empty_book_heartbeat_are_preserved(tmp_path):
    trade_raw = _write_raw(
        tmp_path / "raw" / "business.jsonl.gz",
        [
            _raw_record(
                0,
                {
                    "arg": {"channel": "trades-all", "instId": INST_ID},
                    "data": [
                        {
                            "instId": INST_ID,
                            "tradeId": "2001",
                            "px": "61000",
                            "sz": "7",
                            "side": "sell",
                            "ts": "1786850200000",
                        }
                    ],
                },
                stream="business",
            )
        ],
    )
    book_raw = _write_raw(
        tmp_path / "raw" / "heartbeat.jsonl.gz",
        [
            _raw_record(
                0,
                {
                    "arg": {"channel": "books", "instId": INST_ID},
                    "action": "snapshot",
                    "data": [
                        {
                            "asks": [["61001", "2", "0", "1"]],
                            "bids": [["61000", "3", "0", "1"]],
                            "ts": "1786850200000",
                            "prevSeqId": "-1",
                            "seqId": "15",
                            "checksum": "0",
                        }
                    ],
                },
            ),
            _raw_record(
                1,
                {
                    "arg": {"channel": "books", "instId": INST_ID},
                    "action": "update",
                    "data": [
                        {
                            "asks": [],
                            "bids": [],
                            "ts": "1786850200001",
                            "prevSeqId": "15",
                            "seqId": "15",
                            "checksum": "0",
                        }
                    ],
                },
            )
        ],
    )
    output_root = tmp_path / "normalized"

    normalize_raw_file(trade_raw, output_root)
    heartbeat_result = normalize_raw_file(book_raw, output_root)

    trades = _read_table(output_root, "trades")
    assert trades["channel"].to_list() == ["trades-all"]
    assert trades["stream"].to_list() == ["business"]
    message = _read_table(output_root, "book_messages").sort("frame_no").row(
        1, named=True
    )
    assert message["is_heartbeat"]
    assert heartbeat_result.row_counts["book_levels"] == 2


def test_book_update_without_snapshot_fails_without_publishing(tmp_path):
    raw_path = _write_raw(
        tmp_path / "raw" / "heartbeat-without-snapshot.jsonl.gz",
        [
            _raw_record(
                0,
                {
                    "arg": {"channel": "books", "instId": INST_ID},
                    "action": "update",
                    "data": [
                        {
                            "asks": [],
                            "bids": [],
                            "ts": "1786850200001",
                            "prevSeqId": "15",
                            "seqId": "15",
                            "checksum": "0",
                        }
                    ],
                },
            )
        ],
    )
    output_root = tmp_path / "normalized"

    with pytest.raises(RawStreamFormatError, match="before a valid snapshot"):
        normalize_raw_file(raw_path, output_root)

    assert not list(output_root.glob("**/*.parquet"))


def test_instruments_ws_metadata_preserves_raw_fields_and_ready_boundary(tmp_path):
    rows = [
        _raw_record(
            0,
            {
                "op": "subscribe",
                "args": [{"channel": "instruments", "instType": "SWAP"}],
            },
            direction="out",
            kind="subscribe",
        ),
        _raw_record(
            1,
            {
                "event": "subscribe",
                "arg": {"channel": "instruments", "instType": "SWAP"},
            },
        ),
        _raw_record(
            2,
            {"subscriptions": [{"channel": "instruments", "instType": "SWAP"}]},
            direction="local",
            kind="subscriptions_ready",
        ),
        _raw_record(
            3,
            {
                "arg": {"channel": "instruments", "instType": "SWAP"},
                "data": [
                    {
                        "instId": INST_ID,
                        "instType": "SWAP",
                        "instFamily": "BTC-USDT",
                        "uly": "BTC-USDT",
                        "ctType": "linear",
                        "ctVal": "0.01",
                        "ctMult": "1",
                        "ctValCcy": "BTC",
                        "tickSz": "0.1",
                        "lotSz": "0.01",
                        "minSz": "0.01",
                        "maxMktSz": "100000",
                        "maxLmtSz": "1000000",
                        "state": "live",
                        "uTime": "1786850300000",
                    }
                ],
            },
        ),
    ]
    raw_path = _write_raw(tmp_path / "raw" / "instruments.jsonl.gz", rows)
    output_root = tmp_path / "normalized"

    result = normalize_raw_file(raw_path, output_root)

    assert result.row_counts["instrument_metadata"] == 1
    metadata = _read_table(output_root, "instrument_metadata").row(0, named=True)
    assert metadata["inst_id"] == INST_ID
    assert metadata["origin"] == "ws"
    assert metadata["effective_received_at_ns"] == _arrival_ns(3, 3)
    assert metadata["ct_type"] == "linear"
    assert metadata["ct_val_raw"] == "0.01"
    assert metadata["ct_mult_raw"] == "1"
    assert metadata["ct_val_ccy"] == "BTC"
    assert metadata["tick_sz_raw"] == "0.1"
    assert metadata["lot_sz_raw"] == "0.01"
    assert metadata["min_sz_raw"] == "0.01"
    assert metadata["max_mkt_sz_raw"] == "100000"
    assert metadata["state"] == "live"
    assert metadata["event_ts_ms"] == 1786850300000
    assert metadata["is_post_ready"]
    assert metadata["raw_archive_sha256"] == result.raw_archive_sha256
    assert metadata["raw_logical_sha256"] == result.raw_logical_sha256
    assert json.loads(metadata["raw_item_json"])["instId"] == INST_ID


def test_rest_initial_snapshot_uses_same_immutable_metadata_schema(tmp_path):
    received_at_ns = _arrival_ns(5, 30)
    raw_path = _write_raw(
        tmp_path / "raw" / "rest" / "instruments.jsonl.gz",
        [_rest_instrument_record(received_at_ns=received_at_ns)],
    )
    output_root = tmp_path / "normalized"

    first = normalize_raw_file(raw_path, output_root)
    explicit = normalize_rest_instrument_file(raw_path, output_root)

    assert first == explicit
    assert first.row_counts == {
        "trades": 0,
        "bbo": 0,
        "book_messages": 0,
        "book_levels": 0,
        "instrument_metadata": 1,
        "session_controls": 0,
    }
    assert len(first.output_paths) == 1
    assert "instrument_metadata/schema_version=3/arrival_date=2026-08-16" in str(
        first.output_paths[0]
    )
    metadata = _read_table(output_root, "instrument_metadata")
    assert metadata.schema == table_schema("instrument_metadata")
    row = metadata.row(0, named=True)
    assert row["origin"] == "rest"
    assert row["effective_received_at_ns"] == received_at_ns
    assert row["received_at_ns"] == received_at_ns
    assert row["event_ts_ms"] is None
    assert row["stream"] == "rest"
    assert row["session_id"] == f"rest-{first.raw_archive_sha256}"
    assert row["frame_no"] == 0
    assert row["data_idx"] == 0
    assert row["monotonic_ns"] is None
    assert row["channel"] == "instruments"
    assert row["inst_id"] == INST_ID
    assert row["is_post_ready"]
    assert row["ct_type"] == "linear"
    assert row["ct_val_raw"] == "0.01"
    assert row["ct_mult_raw"] == "1"
    assert row["ct_val_ccy"] == "BTC"
    assert row["tick_sz_raw"] == "0.1"
    assert row["lot_sz_raw"] == "0.01"
    assert row["min_sz_raw"] == "0.01"
    assert row["max_mkt_sz_raw"] == "35000"
    assert row["state"] == "live"
    assert row["raw_archive_sha256"] == first.raw_archive_sha256
    assert row["raw_logical_sha256"] == first.raw_logical_sha256
    assert json.loads(row["raw_item_json"])["maxMktSz"] == "35000"


def test_rest_initial_state_and_ws_update_share_causal_effective_timeline(tmp_path):
    rest_received = _arrival_ns(5, 0)
    ws_received = _arrival_ns(5, 1)
    # exchange uTime is deliberately older: effective order must still use local receipt.
    rest_raw = _write_raw(
        tmp_path / "rest.jsonl.gz",
        [_rest_instrument_record(received_at_ns=rest_received)],
    )
    ws_raw = _write_raw(
        tmp_path / "ws.jsonl.gz",
        [
            _raw_record(
                0,
                {
                    "arg": {"channel": "instruments", "instType": "SWAP"},
                    "data": [_instrument_item(tickSz="0.01", uTime="1")],
                },
                received_at_ns=ws_received,
            )
        ],
    )
    output_root = tmp_path / "normalized"

    results = [
        normalize_raw_file(rest_raw, output_root),
        normalize_raw_file(ws_raw, output_root),
    ]

    assert all(result.row_counts["instrument_metadata"] == 1 for result in results)
    timeline = _read_table(output_root, "instrument_metadata").sort(
        "effective_received_at_ns"
    )
    assert timeline["origin"].to_list() == ["rest", "ws"]
    assert timeline["effective_received_at_ns"].to_list() == [
        rest_received,
        ws_received,
    ]
    assert timeline["tick_sz_raw"].to_list() == ["0.1", "0.01"]
    assert timeline["event_ts_ms"].to_list() == [None, 1]


@pytest.mark.parametrize(
    ("record_mutator", "match"),
    [
        (lambda record: record["response"].update(code="50001"), "response failed"),
        (
            lambda record: record["response"]["data"][0].update(instId="ETH-USDT-SWAP"),
            "request.instId",
        ),
        (
            lambda record: record["response"]["data"][0].pop("ctVal"),
            "data.ctVal",
        ),
        (
            lambda record: record["response"].update(
                data=[_instrument_item(), _instrument_item()]
            ),
            "exactly one instrument",
        ),
    ],
)
def test_rest_initial_snapshot_fails_closed_without_publishing(
    tmp_path, record_mutator, match
):
    record = _rest_instrument_record()
    record_mutator(record)
    raw_path = _write_raw(tmp_path / "bad-rest.jsonl.gz", [record])
    output_root = tmp_path / "normalized"

    with pytest.raises(RawStreamFormatError, match=match):
        normalize_raw_file(raw_path, output_root)

    assert not list(output_root.glob("**/*.parquet"))


def test_schema_v3_path_does_not_collide_with_existing_v2_smoke_shard(tmp_path):
    output_root = tmp_path / "normalized"
    old_path = (
        output_root
        / "instrument_metadata"
        / "arrival_date=2026-08-16"
        / "arrival_hour=05"
        / "part-v2-smoke-000000.parquet"
    )
    old_path.parent.mkdir(parents=True)
    old_path.write_bytes(b"existing-v2-smoke")
    raw_path = _write_raw(
        tmp_path / "rest.jsonl.gz", [_rest_instrument_record()]
    )

    result = normalize_raw_file(raw_path, output_root)

    assert old_path.read_bytes() == b"existing-v2-smoke"
    assert len(result.output_paths) == 1
    assert "schema_version=3" in str(result.output_paths[0])
    assert result.output_paths[0] != old_path


def test_contract_quantity_base_denominated_linear_swap_is_exact_decimal():
    converted = convert_contract_quantity(
        contracts="7.25",
        price="62000.1",
        ct_val="0.01",
        ct_mult="1",
        ct_val_ccy="BTC",
        ct_type="linear",
        base_ccy="BTC",
        quote_ccy="USDT",
    )

    assert converted.contracts == Decimal("7.25")
    assert converted.base_qty == Decimal("0.0725")
    assert converted.quote_notional == Decimal("4495.00725")


def test_contract_quantity_quote_denominated_linear_swap_is_exact_decimal():
    converted = convert_contract_quantity(
        contracts=3,
        price="60000",
        ct_val="100",
        ct_mult="1",
        ct_val_ccy="USDT",
        ct_type="linear",
        base_ccy="BTC",
        quote_ccy="USDT",
    )

    assert converted.contracts == Decimal("3")
    assert converted.quote_notional == Decimal("300")
    assert converted.base_qty == Decimal("0.005")


@pytest.mark.parametrize(
    ("overrides", "match"),
    [
        ({"ct_type": "inverse"}, "unsupported ct_type"),
        ({"ct_val_ccy": "EUR"}, "matches neither"),
        ({"price": "0"}, "price must be positive"),
        ({"ct_val": "NaN"}, "non-finite ct_val"),
        ({"contracts": "-1"}, "contracts must be non-negative"),
        ({"contracts": 1.5}, "must be Decimal"),
    ],
)
def test_contract_quantity_unknown_or_unsafe_spec_fails_closed(overrides, match):
    values = {
        "contracts": "1",
        "price": "60000",
        "ct_val": "0.01",
        "ct_mult": "1",
        "ct_val_ccy": "BTC",
        "ct_type": "linear",
        "base_ccy": "BTC",
        "quote_ccy": "USDT",
    }
    values.update(overrides)

    with pytest.raises(ContractConversionError, match=match):
        convert_contract_quantity(**values)


def test_unknown_inbound_event_and_channel_are_auditable_controls(tmp_path):
    raw_path = _write_raw(
        tmp_path / "raw" / "unknown.jsonl.gz",
        [
            _raw_record(
                0,
                {"subscriptions": []},
                direction="local",
                kind="subscriptions_ready",
            ),
            _raw_record(1, {"event": "future-event", "code": "x"}),
            _raw_record(
                2,
                {"arg": {"channel": "future-channel"}, "data": [{"x": 1}]},
            ),
            _raw_record(
                3,
                {
                    "event": "subscribe",
                    "arg": {"channel": "future-channel"},
                },
            ),
        ],
    )
    output_root = tmp_path / "normalized"

    result = normalize_raw_file(raw_path, output_root)

    assert result.row_counts["session_controls"] == 4
    controls = _read_table(output_root, "session_controls").sort("frame_no")
    assert controls["is_unrecognized"].to_list() == [False, True, True, True]
    assert controls["is_post_ready"].to_list() == [True, True, True, True]
    reasons = controls["unrecognized_reason"].to_list()
    assert reasons[0] is None
    assert "unknown websocket event" in reasons[1]
    assert "unknown inbound channel" in reasons[2]
    assert "unknown websocket event channel" in reasons[3]


def test_known_channel_on_wrong_stream_fails_closed(tmp_path):
    raw_path = _write_raw(
        tmp_path / "raw" / "wrong-stream.jsonl.gz",
        [
            _raw_record(
                0,
                {
                    "arg": {"channel": "trades", "instId": INST_ID},
                    "data": [
                        {
                            "tradeId": "1",
                            "px": "1",
                            "sz": "1",
                            "side": "buy",
                            "ts": "1",
                        }
                    ],
                },
                stream="business",
            )
        ],
    )
    output_root = tmp_path / "normalized"

    with pytest.raises(RawStreamFormatError, match="requires stream 'public'"):
        normalize_raw_file(raw_path, output_root)

    assert not list(output_root.glob("**/*.parquet"))


def test_partitions_on_arrival_hour_and_rerun_is_idempotent(tmp_path):
    raw_path = _write_raw(
        tmp_path / "raw.jsonl.gz",
        [
            _raw_record(
                0,
                {
                    "arg": {"channel": "trades", "instId": INST_ID},
                    "data": [
                        {
                            "tradeId": "1",
                            "px": "1",
                            "sz": "2",
                            "side": "buy",
                            "ts": "1",
                        }
                    ],
                },
                received_at_ns=_arrival_ns(3, 59),
            ),
            _raw_record(
                1,
                {
                    "arg": {"channel": "trades", "instId": INST_ID},
                    "data": [
                        {
                            "tradeId": "2",
                            "px": "2",
                            "sz": "3",
                            "side": "sell",
                            "ts": "2",
                        }
                    ],
                },
                received_at_ns=_arrival_ns(4, 0),
            ),
        ],
    )
    output_root = tmp_path / "normalized"

    first = normalize_raw_file(raw_path, output_root)
    second = normalize_raw_file(raw_path, output_root)

    assert first.output_paths == second.output_paths
    assert len(first.output_paths) == 2
    assert len(list(output_root.glob("**/*.parquet"))) == 2
    assert {path.parent.name for path in first.output_paths} == {
        "arrival_hour=03",
        "arrival_hour=04",
    }
    assert sorted(_read_table(output_root, "trades")["trade_id"].to_list()) == ["1", "2"]


def test_frame_discontinuity_fails_without_publishing_parquet(tmp_path):
    raw_path = _write_raw(
        tmp_path / "broken.jsonl.gz",
        [
            _raw_record(0, {"event": "subscribe"}),
            _raw_record(2, {"event": "subscribe"}),
        ],
    )
    output_root = tmp_path / "normalized"

    with pytest.raises(RawStreamFormatError, match="frame_no discontinuity"):
        normalize_raw_file(raw_path, output_root)

    assert not list(output_root.glob("**/*.parquet"))


def test_invalid_recognized_payload_fails_without_publishing_parquet(tmp_path):
    raw_path = _write_raw(
        tmp_path / "bad-payload.jsonl.gz",
        [
            _raw_record(
                0,
                {
                    "arg": {"channel": "trades", "instId": INST_ID},
                    "data": [
                        {
                            "tradeId": "1",
                            "px": "not-a-number",
                            "sz": "2",
                            "side": "buy",
                            "ts": "1",
                        }
                    ],
                },
            )
        ],
    )
    output_root = tmp_path / "normalized"

    with pytest.raises(RawStreamFormatError, match="invalid data.px"):
        normalize_raw_file(raw_path, output_root)

    assert not list(output_root.glob("**/*.parquet"))


def test_existing_different_shard_is_never_overwritten(tmp_path):
    raw_path = _write_raw(
        tmp_path / "raw.jsonl.gz",
        [
            _raw_record(
                0,
                {
                    "arg": {"channel": "trades", "instId": INST_ID},
                    "data": [
                        {
                            "tradeId": "1",
                            "px": "1",
                            "sz": "2",
                            "side": "buy",
                            "ts": "1",
                        }
                    ],
                },
            )
        ],
    )
    output_root = tmp_path / "normalized"
    first = normalize_raw_file(raw_path, output_root)
    shard = first.output_paths[0]
    tampered = pl.read_parquet(shard).with_columns(pl.lit(999.0).alias("px"))
    tampered.write_parquet(shard)

    with pytest.raises(ImmutableOutputError, match="differs from existing"):
        normalize_raw_file(raw_path, output_root)

    # create-only: 衝突を検知しても既存ファイルには触れない。
    assert pl.read_parquet(shard)["px"].to_list() == [999.0]

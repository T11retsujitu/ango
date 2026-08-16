import gzip
import json
from datetime import datetime, timezone

from mce.microstructure_quality import analyze_file, analyze_paths, main
from mce.stream_store import GzipJsonlStreamWriter


INST_ID = "BTC-USDT-SWAP"
TS_MS = 1_786_800_000_000


def _public_subscriptions():
    return [
        {"channel": "trades", "instId": INST_ID},
        {"channel": "bbo-tbt", "instId": INST_ID},
        {"channel": "books", "instId": INST_ID},
        {"channel": "instruments", "instType": "SWAP"},
    ]


def _business_subscriptions():
    return [{"channel": "trades-all", "instId": INST_ID}]


def _message(channel, data, **extra):
    result = {"arg": {"channel": channel, "instId": INST_ID}, "data": data}
    result.update(extra)
    return json.dumps(result, separators=(",", ":"))


def _write_session(tmp_path, stream, subscriptions, frames, *, session_id):
    started = datetime(2026, 8, 16, 1 if stream == "public" else 2, tzinfo=timezone.utc)
    writer = GzipJsonlStreamWriter(
        tmp_path,
        stream,
        session_id=session_id,
        started_at=started,
    )
    writer.append_event(
        "session_opening", url=f"wss://fixture/{stream}", subscriptions=subscriptions
    )
    writer.append_event("connected", url=f"wss://fixture/{stream}")
    request = {"id": session_id, "op": "subscribe", "args": subscriptions}
    writer.append(
        json.dumps(request, separators=(",", ":")),
        direction="out",
        kind="subscribe",
    )
    for subscription in subscriptions:
        writer.append(
            json.dumps(
                {"event": "subscribe", "arg": subscription, "connId": session_id},
                separators=(",", ":"),
            ),
            direction="in",
        )
    writer.append_event("subscriptions_ready", subscriptions=subscriptions)
    for index, frame in enumerate(frames):
        writer.append(
            frame,
            direction="in",
            received_at_ns=(TS_MS + 5 + index) * 1_000_000,
        )
    writer.append_event("stopped")
    path = writer.path
    writer.close()
    return path


def _valid_pair(tmp_path):
    public_frames = [
        _message(
            "trades",
            [
                {"ts": str(TS_MS), "sz": "1", "tradeId": "1", "side": "buy"},
                {"ts": str(TS_MS + 1), "sz": "2", "tradeId": "2", "side": "sell"},
            ],
        ),
        _message(
            "bbo-tbt",
            [
                {
                    "ts": str(TS_MS + 2),
                    "asks": [["60001", "2", "0", "1"]],
                    "bids": [["60000", "3", "0", "1"]],
                }
            ],
        ),
        _message(
            "books",
            [
                {
                    "ts": str(TS_MS + 3),
                    "asks": [["60001", "2", "0", "1"]],
                    "bids": [["60000", "3", "0", "1"]],
                    "prevSeqId": -1,
                    "seqId": 10,
                }
            ],
            action="snapshot",
        ),
        _message(
            "books",
            [
                {
                    "ts": str(TS_MS + 4),
                    "asks": [["60001", "1", "0", "1"]],
                    "bids": [],
                    "prevSeqId": 10,
                    "seqId": 11,
                }
            ],
            action="update",
        ),
        json.dumps(
            {
                "arg": {"channel": "instruments", "instType": "SWAP"},
                "data": [
                    {
                        "instId": INST_ID,
                        "instType": "SWAP",
                        "instFamily": "BTC-USDT",
                        "ctVal": "0.01",
                        "ctValCcy": "BTC",
                        "ctType": "linear",
                        "settleCcy": "USDT",
                        "tickSz": "0.1",
                        "lotSz": "0.01",
                        "minSz": "0.01",
                        "state": "live",
                    },
                    {"instId": "ETH-USDT-SWAP", "instType": "SWAP"},
                ],
            },
            separators=(",", ":"),
        ),
    ]
    business_frames = [
        _message(
            "trades-all",
            [
                {"ts": str(TS_MS), "sz": "0.5", "tradeId": "a", "side": "buy"},
                {"ts": str(TS_MS + 1), "sz": "1", "tradeId": "b", "side": "buy"},
                {"ts": str(TS_MS + 2), "sz": "1.5", "tradeId": "c", "side": "sell"},
            ],
        )
    ]
    public_path = _write_session(
        tmp_path,
        "public",
        _public_subscriptions(),
        public_frames,
        session_id="publicfixture",
    )
    business_path = _write_session(
        tmp_path,
        "business",
        _business_subscriptions(),
        business_frames,
        session_id="businessfixture",
    )
    return public_path, business_path


def _codes(report):
    return {reason["code"] for reason in report["reasons"]}


def test_valid_multi_session_report_counts_channels_lag_books_and_trades(tmp_path):
    public_path, business_path = _valid_pair(tmp_path)

    report = analyze_paths([tmp_path])

    assert report["valid"]
    assert report["summary"]["file_count"] == 2
    assert report["summary"]["valid_file_count"] == 2
    assert report["summary"]["records"] > 0
    assert report["summary"]["compressed_bytes"] == (
        public_path.stat().st_size + business_path.stat().st_size
    )
    assert report["summary"]["channels"]["trades"]["items"] == 2
    assert report["summary"]["channels"]["trades-all"]["items"] == 3
    assert report["summary"]["channels"]["books"]["messages"] == 2
    assert report["summary"]["books"]["snapshots"] == 1
    assert report["summary"]["books"]["updates"] == 1
    assert report["summary"]["books"]["sequence_gaps"] == 0
    assert report["summary"]["exchange_to_receive_lag"]["count"] == 8
    assert report["summary"]["exchange_to_receive_lag"]["negative_count"] == 0

    instruments = report["summary"]["instruments"]
    assert instruments["messages"] == 1
    assert instruments["items"] == 2
    assert instruments["unique_instrument_count"] == 2
    assert instruments["btc_usdt_swap_present"]

    reconciliation = report["summary"]["trade_reconciliation"]
    assert reconciliation["comparison_is_descriptive_only"]
    assert reconciliation["public_trades"]["items"] == 2
    assert reconciliation["business_trades_all"]["items"] == 3
    assert reconciliation["public_trades"]["contract_quantity"] == "3"
    assert reconciliation["business_trades_all"]["contract_quantity"] == "3"
    assert reconciliation["overlap"]["absolute_relative_quantity_difference"] == 0

    public = next(item for item in report["files"] if item["session"]["stream"] == "public")
    assert public["subscriptions"]["missing_acks"] == []
    assert len(public["subscriptions"]["acknowledged"]) == 4
    assert public["session"]["terminal"] == "stopped"
    assert public["file"]["gzip_ok"]
    assert public["instruments"]["btc_contract_metadata"][0]["ctVal"] == "0.01"


def test_books_gap_is_revalidated_and_marks_file_invalid(tmp_path):
    frames = [
        _message(
            "trades",
            [{"ts": str(TS_MS), "sz": "1", "tradeId": "1", "side": "buy"}],
        ),
        _message(
            "bbo-tbt",
            [{"ts": str(TS_MS), "asks": [], "bids": []}],
        ),
        _message(
            "books",
            [
                {
                    "ts": str(TS_MS),
                    "asks": [],
                    "bids": [],
                    "prevSeqId": -1,
                    "seqId": 10,
                }
            ],
            action="snapshot",
        ),
        _message(
            "books",
            [
                {
                    "ts": str(TS_MS + 1),
                    "asks": [],
                    "bids": [],
                    "prevSeqId": 8,
                    "seqId": 11,
                }
            ],
            action="update",
        ),
    ]
    path = _write_session(
        tmp_path,
        "public",
        _public_subscriptions()[:3],
        frames,
        session_id="bookgap",
    )

    report = analyze_file(path)

    assert not report["valid"]
    assert "books_sequence_gap" in _codes(report)
    assert "books_state_invalid_at_close" in _codes(report)
    assert report["books"]["sequence_gaps"] == 1


def test_ack_completeness_is_inferred_for_three_channel_public_session(tmp_path):
    frames = [
        _message(
            "trades",
            [{"ts": str(TS_MS), "sz": "1", "tradeId": "1", "side": "buy"}],
        ),
        _message(
            "bbo-tbt",
            [{"ts": str(TS_MS), "asks": [], "bids": []}],
        ),
        _message(
            "books",
            [
                {
                    "ts": str(TS_MS),
                    "asks": [],
                    "bids": [],
                    "prevSeqId": -1,
                    "seqId": 10,
                }
            ],
            action="snapshot",
        ),
    ]
    path = _write_session(
        tmp_path,
        "public",
        _public_subscriptions()[:3],
        frames,
        session_id="threechannels",
    )

    report = analyze_file(path)

    assert report["valid"]
    assert len(report["subscriptions"]["requested"]) == 3
    assert len(report["subscriptions"]["acknowledged"]) == 3
    assert report["subscriptions"]["missing_acks"] == []


def test_bad_json_and_noncontiguous_frame_are_not_silently_skipped(tmp_path):
    path = tmp_path / "broken.jsonl.gz"
    base = {
        "schema_version": 1,
        "stream": "public",
        "session_id": "broken",
        "frame_no": 0,
        "direction": "local",
        "kind": "session_opening",
        "received_at_ns": 1,
        "monotonic_ns": 1,
        "payload": json.dumps({"subscriptions": _public_subscriptions()[:3]}),
    }
    second = dict(base, frame_no=2, kind="connected", payload="{}", monotonic_ns=2)
    with gzip.open(path, "wt", encoding="utf-8") as handle:
        handle.write(json.dumps(base) + "\n")
        handle.write("not-json\n")
        handle.write(json.dumps(second) + "\n")

    report = analyze_file(path)

    assert not report["valid"]
    assert report["file"]["gzip_ok"]
    assert "wrapper_json_invalid" in _codes(report)
    assert "frame_sequence_gap" in _codes(report)


def test_invalid_gzip_is_explicitly_reported(tmp_path):
    path = tmp_path / "invalid.jsonl.gz"
    path.write_bytes(b"this is not gzip")

    report = analyze_file(path)

    assert not report["valid"]
    assert "gzip_read_failed" in _codes(report)
    assert not report["file"]["gzip_ok"]


def test_exchange_error_event_is_reported_with_exchange_code(tmp_path):
    path = _write_session(
        tmp_path,
        "business",
        _business_subscriptions(),
        [json.dumps({"event": "error", "code": "60033", "msg": "bad id"})],
        session_id="exchangeerror",
    )

    report = analyze_file(path)

    assert not report["valid"]
    error = next(item for item in report["reasons"] if item["code"] == "exchange_error_event")
    assert error["context"]["exchange_code"] == "60033"


def test_cli_prints_json_atomic_writes_output_and_returns_quality_status(
    tmp_path, capsys
):
    _valid_pair(tmp_path)
    output = tmp_path / "reports" / "quality.json"

    status = main([str(tmp_path / "okx"), "--output", str(output)])

    assert status == 0
    printed = json.loads(capsys.readouterr().out)
    written = json.loads(output.read_text(encoding="utf-8"))
    assert printed == written
    assert written["valid"]
    assert not list(output.parent.glob(".quality.json.*.tmp"))

    missing_status = main([str(tmp_path / "does-not-exist")])
    missing = json.loads(capsys.readouterr().out)
    assert missing_status == 1
    assert not missing["valid"]
    assert {item["code"] for item in missing["reasons"]} == {
        "input_missing",
        "no_closed_raw_files",
    }

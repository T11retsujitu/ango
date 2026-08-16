import gzip
import json
from datetime import datetime, timezone

import pytest

from mce.stream_store import GzipJsonlStreamWriter, StreamWriteError


def _read_records(path):
    with gzip.open(path, "rt", encoding="utf-8") as f:
        return [json.loads(line) for line in f]


def test_gzip_stream_preserves_payload_and_arrival_order(tmp_path):
    wall_times = iter([101, 102, 103])
    monotonic_times = iter([201, 202, 203])
    started = datetime(2026, 8, 16, 3, 4, 5, tzinfo=timezone.utc)

    writer = GzipJsonlStreamWriter(
        tmp_path,
        "public",
        session_id="session-1",
        started_at=started,
        time_ns=lambda: next(wall_times),
        monotonic_ns=lambda: next(monotonic_times),
    )
    subscribe = '{"op":"subscribe", "args":[]}'
    trade = '{ "arg": {"channel":"trades"}, "data": [] }'
    writer.append(subscribe, direction="out", kind="subscribe")
    writer.append(trade, direction="in")
    writer.append_event("connected", url="wss://example.invalid")
    path = writer.path
    partial_path = writer.partial_path
    assert not path.exists()
    assert partial_path.exists()
    assert partial_path.name.endswith(".jsonl.gz.partial")
    writer.close()

    assert path.exists()
    assert not partial_path.exists()
    assert path.parent == tmp_path / "okx" / "ws" / "public" / "2026" / "08" / "16"
    rows = _read_records(path)
    assert [row["frame_no"] for row in rows] == [0, 1, 2]
    assert [row["direction"] for row in rows] == ["out", "in", "local"]
    # JSON の空白も含め、受け取ったpayload文字列を変更しない。
    assert rows[0]["payload"] == subscribe
    assert rows[1]["payload"] == trade
    assert rows[2]["kind"] == "connected"
    assert [row["received_at_ns"] for row in rows] == [101, 102, 103]
    assert [row["monotonic_ns"] for row in rows] == [201, 202, 203]
    assert all(row["session_id"] == "session-1" for row in rows)


def test_context_manager_closes_and_rejects_late_append(tmp_path):
    with GzipJsonlStreamWriter(tmp_path, "business") as writer:
        writer.append("pong", direction="in", kind="pong")
        path = writer.path

    assert writer.closed
    assert _read_records(path)[0]["payload"] == "pong"
    with pytest.raises(StreamWriteError, match="already closed"):
        writer.append("late", direction="in")


@pytest.mark.parametrize("value", ["../public", "public/x", ""])
def test_stream_name_cannot_escape_raw_root(tmp_path, value):
    with pytest.raises(ValueError, match="unsafe stream name"):
        GzipJsonlStreamWriter(tmp_path, value)


def test_metadata_is_nested_and_does_not_override_provenance(tmp_path):
    with GzipJsonlStreamWriter(tmp_path, "public", session_id="s") as writer:
        writer.append(
            "{}",
            direction="in",
            received_at_ns=123,
            monotonic_ns=456,
            metadata={"channel": "books", "frame_no": 999},
        )
        path = writer.path

    row = _read_records(path)[0]
    assert row["frame_no"] == 0
    assert row["metadata"] == {"channel": "books", "frame_no": 999}
    assert row["received_at_ns"] == 123
    assert row["monotonic_ns"] == 456


def test_unclosed_stream_is_visibly_partial_and_not_discoverable_as_complete(tmp_path):
    writer = GzipJsonlStreamWriter(tmp_path, "public", session_id="crashfixture")
    writer.append("{}", direction="in")

    assert writer.partial_path.exists()
    assert not writer.path.exists()
    assert list(tmp_path.glob("okx/ws/public/**/*.jsonl.gz")) == []

    # test processは実際にはcrashさせないので最後にclean closeし、publishも確認する。
    writer.close()
    assert writer.path.exists()
    assert not writer.partial_path.exists()

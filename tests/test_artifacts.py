"""運用 artifact の書き込み規律(append-only ledger と atomic JSON)。"""

import json
from pathlib import Path

import pytest

from mce.artifacts import LedgerError, append_jsonl, atomic_write_json, json_line, read_jsonl


def test_append_and_read_round_trip(tmp_path: Path):
    ledger = tmp_path / "nested" / "ledger.jsonl"
    append_jsonl(ledger, {"b": 1, "a": 2})
    append_jsonl(ledger, {"a": 3})
    assert list(read_jsonl(ledger)) == [{"b": 1, "a": 2}, {"a": 3}]
    # key 順を固定するので、同じ内容なら同じ行になる。
    assert ledger.read_text(encoding="utf-8").splitlines()[0] == json_line({"a": 2, "b": 1})


def test_reading_a_missing_ledger_is_empty_not_an_error(tmp_path: Path):
    assert list(read_jsonl(tmp_path / "absent.jsonl")) == []


def test_truncated_tail_line_fails_closed(tmp_path: Path):
    ledger = tmp_path / "ledger.jsonl"
    ledger.write_text('{"a":1}\n{"a":2}', encoding="utf-8")  # 末尾に改行が無い
    with pytest.raises(LedgerError):
        list(read_jsonl(ledger))


def test_invalid_json_row_fails_closed(tmp_path: Path):
    ledger = tmp_path / "ledger.jsonl"
    ledger.write_text('{"a":1}\nnot-json\n', encoding="utf-8")
    with pytest.raises(LedgerError):
        list(read_jsonl(ledger))


def test_non_object_row_fails_closed(tmp_path: Path):
    ledger = tmp_path / "ledger.jsonl"
    ledger.write_text("[1,2]\n", encoding="utf-8")
    with pytest.raises(LedgerError):
        list(read_jsonl(ledger))


def test_atomic_write_leaves_no_temporary_file(tmp_path: Path):
    target = tmp_path / "report" / "day.json"
    atomic_write_json(target, {"date_utc": "2026-08-16", "sessions_valid": 2})
    assert json.loads(target.read_text(encoding="utf-8"))["sessions_valid"] == 2
    assert [p.name for p in target.parent.iterdir()] == ["day.json"]


def test_atomic_write_replaces_the_previous_content(tmp_path: Path):
    target = tmp_path / "day.json"
    atomic_write_json(target, {"v": 1})
    atomic_write_json(target, {"v": 2})
    assert json.loads(target.read_text(encoding="utf-8")) == {"v": 2}

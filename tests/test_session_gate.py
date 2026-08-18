"""closed raw の quality gate と quarantine。判定は valid/invalid/pending の3値だけ。"""

import json
from pathlib import Path

import pytest

import raw_fixtures as rf
from mce import session_gate as sg
from mce.artifacts import read_jsonl

FUTURE_NS = rf.BASE_NS + 30 * 86_400 * 10**9


def _run(tmp_path: Path, **overrides):
    kwargs = {
        "raw_dir": tmp_path / "raw",
        "quarantine_dir": tmp_path / "quarantine",
        "ledger_path": tmp_path / "gate_ledger.jsonl",
        "now_ns": FUTURE_NS,
    }
    kwargs.update(overrides)
    return sg.run_gate(**kwargs)


def test_valid_pair_is_promoted_and_recorded(tmp_path: Path):
    raw = tmp_path / "raw"
    public, business = rf.valid_pair(raw)

    summary = _run(tmp_path)

    assert summary["counts"]["valid"] == 2
    assert summary["counts"]["invalid"] == 0
    assert set(summary["valid_paths"]) == {str(public), str(business)}
    # valid session は raw のまま残る(隔離しない)。
    assert public.exists() and business.exists()

    rows = list(read_jsonl(tmp_path / "gate_ledger.jsonl"))
    assert len(rows) == 2
    for row in rows:
        assert row["decision"] == "valid"
        assert len(row["raw_sha256"]) == 64
        assert len(row["quality_report_sha256"]) == 64
        assert row["quarantine_path"] is None
        assert row["first_received_at_ns"] == rf.BASE_NS


def test_books_sequence_gap_is_quarantined_not_deleted(tmp_path: Path):
    raw = tmp_path / "raw"
    broken = rf.write_session(
        raw,
        "public",
        rf.public_frames(rf.BASE_NS // 1_000_000)[:2],
        session_id="brokenbooks",
        broken_books=True,
    )
    rf.write_clock_sample(raw, rf.BASE_NS)

    summary = _run(tmp_path)

    assert summary["counts"]["invalid"] == 1
    assert not broken.exists(), "invalid raw は raw 配下から外れる"
    quarantined = tmp_path / "quarantine" / broken.relative_to(raw)
    assert quarantined.exists(), "invalid raw は削除せず quarantine に残す"

    report = json.loads(quarantined.with_name(quarantined.name + ".quality.json").read_text())
    assert report["original_path"] == str(broken)
    assert any(item["code"] == "books_sequence_gap" for item in report["reasons"])

    row = next(iter(read_jsonl(tmp_path / "gate_ledger.jsonl")))
    assert row["decision"] == "invalid"
    assert "books_sequence_gap" in row["reasons"]
    assert row["quarantine_path"] == str(quarantined)


def test_missing_clock_quality_makes_the_session_invalid(tmp_path: Path):
    raw = tmp_path / "raw"
    rf.write_session(
        raw,
        "public",
        rf.public_frames(rf.BASE_NS // 1_000_000),
        session_id="noclock",
    )

    summary = _run(tmp_path)
    assert summary["counts"]["invalid"] == 1
    row = next(iter(read_jsonl(tmp_path / "gate_ledger.jsonl")))
    assert "clock_quality_missing" in row["reasons"]


def test_clock_sample_outside_the_session_window_is_not_coverage(tmp_path: Path):
    raw = tmp_path / "raw"
    rf.write_session(
        raw,
        "public",
        rf.public_frames(rf.BASE_NS // 1_000_000),
        session_id="staleclock",
        span_ns=3600 * 10**9,
    )
    # session 窓から 1 日離れた sample は「同時に取れていた」ことにならない。
    rf.write_clock_sample(raw, rf.BASE_NS + 86_400 * 10**9)

    summary = _run(tmp_path)
    assert summary["counts"]["invalid"] == 1
    row = next(iter(read_jsonl(tmp_path / "gate_ledger.jsonl")))
    assert "clock_quality_uncovered" in row["reasons"]


def test_clock_gate_can_be_disabled_explicitly(tmp_path: Path):
    raw = tmp_path / "raw"
    rf.write_session(
        raw, "public", rf.public_frames(rf.BASE_NS // 1_000_000), session_id="noclock2"
    )
    summary = _run(tmp_path, require_clock_quality=False)
    assert summary["counts"]["valid"] == 1


def test_long_session_with_a_silent_required_channel_is_invalid(tmp_path: Path):
    raw = tmp_path / "raw"
    frames = rf.public_frames(rf.BASE_NS // 1_000_000)
    # trades だけを落とす(bbo/books は生きている)。
    silent = [frame for frame in frames if '"channel":"trades"' not in frame]
    rf.write_session(
        raw, "public", silent, session_id="silenttrades", span_ns=3600 * 10**9
    )
    rf.write_clock_sample(raw, rf.BASE_NS)

    summary = _run(tmp_path)
    assert summary["counts"]["invalid"] == 1
    row = next(iter(read_jsonl(tmp_path / "gate_ledger.jsonl")))
    assert "channel_silent" in row["reasons"]


def test_short_session_does_not_trigger_the_silence_rule(tmp_path: Path):
    raw = tmp_path / "raw"
    frames = rf.public_frames(rf.BASE_NS // 1_000_000)
    silent = [frame for frame in frames if '"channel":"trades"' not in frame]
    rf.write_session(raw, "public", silent, session_id="shortsilent", step_ns=1_000_000)
    rf.write_clock_sample(raw, rf.BASE_NS)

    summary = _run(tmp_path)
    assert summary["counts"]["valid"] == 1


def test_recently_written_file_is_pending_not_judged(tmp_path: Path):
    raw = tmp_path / "raw"
    rf.valid_pair(raw)
    summary = _run(tmp_path, now_ns=rf.BASE_NS)  # mtime は実時刻なので未 settle

    assert summary["counts"]["pending"] == 2
    assert summary["counts"]["valid"] == 0
    # pending は台帳に確定させない(まだ結論ではない)。
    assert not (tmp_path / "gate_ledger.jsonl").exists()


def test_open_partial_files_are_reported_and_never_gated(tmp_path: Path):
    raw = tmp_path / "raw"
    rf.valid_pair(raw)
    partial = raw / "okx" / "ws" / "public" / "2026" / "08" / "16" / "open.jsonl.gz.partial"
    partial.write_bytes(b"not-a-closed-session")

    summary = _run(tmp_path)
    assert summary["open_partial_files"] == [str(partial)]
    assert str(partial) not in summary["invalid_paths"]
    assert partial.exists()


def test_rerun_is_idempotent_and_does_not_rejudge(tmp_path: Path):
    raw = tmp_path / "raw"
    rf.valid_pair(raw)

    first = _run(tmp_path)
    second = _run(tmp_path)

    assert first["counts"]["valid"] == 2
    assert second["counts"]["valid"] == 0
    assert second["counts"]["skipped"] == 2
    assert len(list(read_jsonl(tmp_path / "gate_ledger.jsonl"))) == 2


def test_dry_run_judges_without_moving_anything(tmp_path: Path):
    raw = tmp_path / "raw"
    broken = rf.write_session(
        raw,
        "public",
        rf.public_frames(rf.BASE_NS // 1_000_000)[:2],
        session_id="drybroken",
        broken_books=True,
    )
    rf.write_clock_sample(raw, rf.BASE_NS)

    summary = _run(tmp_path, quarantine=False)
    assert summary["counts"]["invalid"] == 1
    assert broken.exists()
    assert not (tmp_path / "quarantine").exists()


def test_quarantine_refuses_to_overwrite_different_bytes(tmp_path: Path):
    source = tmp_path / "a.jsonl.gz"
    source.write_bytes(b"one")
    target = tmp_path / "q" / "a.jsonl.gz"
    target.parent.mkdir()
    target.write_bytes(b"two")
    with pytest.raises(sg.GateError):
        sg.move_to_quarantine(source, target)
    assert source.exists() and target.read_bytes() == b"two"


def test_quarantine_is_idempotent_for_identical_bytes(tmp_path: Path):
    source = tmp_path / "a.jsonl.gz"
    source.write_bytes(b"same")
    target = tmp_path / "q" / "a.jsonl.gz"
    target.parent.mkdir()
    target.write_bytes(b"same")
    assert sg.move_to_quarantine(source, target) == target
    assert not source.exists()


def test_quarantine_target_keeps_the_raw_relative_layout(tmp_path: Path):
    raw = tmp_path / "raw"
    path = raw / "okx" / "ws" / "public" / "2026" / "08" / "16" / "s.jsonl.gz"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"x")
    target = sg.quarantine_target(raw, tmp_path / "q", path)
    assert target == tmp_path / "q" / "okx" / "ws" / "public" / "2026" / "08" / "16" / "s.jsonl.gz"


def test_cli_exit_code_is_nonzero_when_a_session_is_quarantined(tmp_path: Path, capsys):
    raw = tmp_path / "raw"
    rf.write_session(
        raw,
        "public",
        rf.public_frames(rf.BASE_NS // 1_000_000)[:2],
        session_id="clibroken",
        broken_books=True,
    )
    rf.write_clock_sample(raw, rf.BASE_NS)
    code = sg.main(
        [
            "--raw-dir",
            str(raw),
            "--quarantine-dir",
            str(tmp_path / "quarantine"),
            "--ledger",
            str(tmp_path / "gate.jsonl"),
            "--settle-seconds",
            "0",
            "--output",
            str(tmp_path / "summary.json"),
        ]
    )
    assert code == 1
    summary = json.loads((tmp_path / "summary.json").read_text(encoding="utf-8"))
    assert summary["counts"]["invalid"] == 1
    capsys.readouterr()

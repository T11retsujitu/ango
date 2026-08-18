"""日次 ingest orchestration、収集日 manifest、health ledger。"""

import json
from datetime import date
from pathlib import Path

import raw_fixtures as rf
from mce import daily_ingest as di
from mce.artifacts import read_jsonl

DAY = date(2026, 8, 16)
FUTURE_NS = rf.BASE_NS + 30 * 86_400 * 10**9
HOUR_NS = 3600 * 10**9


def _ingest(tmp_path: Path, **overrides):
    kwargs = {
        "raw_dir": tmp_path / "raw",
        "output_root": tmp_path / "normalized",
        "quarantine_dir": tmp_path / "quarantine",
        "analysis_dir": tmp_path / "analysis",
        "now_ns": FUTURE_NS,
        "settle_seconds": 0.0,
        "source_commit": "cafebabe",
    }
    kwargs.update(overrides)
    return di.run_daily_ingest(**kwargs)


def _aligned_pair(raw: Path, *, first_ns: int, span_ns: int, tag: str):
    public = rf.write_session(
        raw,
        "public",
        rf.public_frames(first_ns // 1_000_000),
        session_id=f"pub{tag}",
        first_received_ns=first_ns,
        span_ns=span_ns,
    )
    business = rf.write_session(
        raw,
        "business",
        rf.business_frames(first_ns // 1_000_000),
        session_id=f"bus{tag}",
        first_received_ns=first_ns,
        span_ns=span_ns,
    )
    rf.write_clock_sample(raw, first_ns)
    return public, business


# --- 区間演算 ---------------------------------------------------------------


def test_merge_intervals_collapses_overlap_and_adjacency():
    merged = di.merge_intervals(
        [di.Interval(10, 20), di.Interval(15, 30), di.Interval(30, 40), di.Interval(60, 70)]
    )
    assert [(item.start_ns, item.end_ns) for item in merged] == [(10, 40), (60, 70)]


def test_complement_reports_the_gaps_including_the_edges():
    gaps = di.complement_intervals([di.Interval(20, 30)], 0, 50)
    assert [(item.start_ns, item.end_ns) for item in gaps] == [(0, 20), (30, 50)]
    assert di.complement_intervals([], 0, 10)[0].seconds == 10 / 1_000_000_000


def test_day_bounds_are_utc_half_open():
    start, end = di.day_bounds_ns(DAY)
    assert di.day_of_ns(start) == DAY
    assert di.day_of_ns(end) == date(2026, 8, 17)
    assert end - start == 86_400 * 10**9


# --- orchestration ----------------------------------------------------------


def test_valid_sessions_are_normalized_and_declared_in_the_day_manifest(tmp_path: Path):
    raw = tmp_path / "raw"
    _aligned_pair(raw, first_ns=rf.BASE_NS, span_ns=HOUR_NS, tag="1")

    summary = _ingest(tmp_path, days=[DAY])

    assert summary["gate"]["counts"]["valid"] == 2
    assert summary["normalize"]["normalized"] == 2
    assert summary["normalize"]["failures"] == []
    assert list((tmp_path / "normalized").glob("**/*.parquet"))

    manifest_path = (
        tmp_path / "analysis" / "collection_days" / "collection_day_manifest_2026-08-16.json"
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["date_utc"] == "2026-08-16"
    assert manifest["sessions_valid"] == 2
    assert manifest["sessions_invalid"] == 0
    assert manifest["clock_status"] == "pass"
    assert manifest["source_commit"] == "cafebabe"
    assert manifest["expected_channels"] == list(di.EXPECTED_CHANNELS)
    assert manifest["observed_streams"] == ["business", "public"]
    assert len(manifest["normalized_shard_digests"]) == 2
    # 02:00-03:00 UTC だけが両 stream で覆われている。
    assert manifest["covered_seconds"] == 3600.0
    assert manifest["covered_intervals"][0]["start"].endswith("02:00:00+00:00")


def test_uncovered_intervals_are_declared_not_hidden(tmp_path: Path):
    raw = tmp_path / "raw"
    _aligned_pair(raw, first_ns=rf.BASE_NS, span_ns=HOUR_NS, tag="1")

    manifest = _ingest(tmp_path, days=[DAY])["manifests"][0]

    # 1 日 86400 秒のうち 3600 秒しか収集していない。残りは欠損として出す。
    assert manifest["uncovered_seconds"] == 86_400 - 3600
    boundaries = [(item["start_ns"], item["end_ns"]) for item in manifest["uncovered_intervals"]]
    start, end = di.day_bounds_ns(DAY)
    assert boundaries == [(start, rf.BASE_NS), (rf.BASE_NS + HOUR_NS, end)]
    assert manifest["covered_seconds"] + manifest["uncovered_seconds"] == 86_400


def test_a_stream_missing_for_part_of_the_window_shrinks_coverage(tmp_path: Path):
    raw = tmp_path / "raw"
    # public は 2 時間、business は最初の 1 時間だけ。共通部分は 1 時間。
    rf.write_session(
        raw,
        "public",
        rf.public_frames(rf.BASE_NS // 1_000_000),
        session_id="publong",
        first_received_ns=rf.BASE_NS,
        span_ns=2 * HOUR_NS,
    )
    rf.write_session(
        raw,
        "business",
        rf.business_frames(rf.BASE_NS // 1_000_000),
        session_id="busshort",
        first_received_ns=rf.BASE_NS,
        span_ns=HOUR_NS,
    )
    rf.write_clock_sample(raw, rf.BASE_NS)

    manifest = _ingest(tmp_path, days=[DAY])["manifests"][0]
    assert manifest["covered_seconds"] == 3600.0


def test_today_is_evaluated_only_up_to_now(tmp_path: Path):
    raw = tmp_path / "raw"
    _aligned_pair(raw, first_ns=rf.BASE_NS, span_ns=HOUR_NS, tag="1")

    # 03:00 UTC 時点で評価する。未来は無収集ではない。
    now_ns = rf.BASE_NS + HOUR_NS
    manifest = _ingest(tmp_path, days=[DAY], now_ns=now_ns)["manifests"][0]
    assert manifest["evaluated_seconds"] == 3 * 3600.0
    assert manifest["uncovered_seconds"] == 2 * 3600.0


def test_invalid_session_is_quarantined_and_never_normalized(tmp_path: Path):
    raw = tmp_path / "raw"
    _aligned_pair(raw, first_ns=rf.BASE_NS, span_ns=HOUR_NS, tag="1")
    broken = rf.write_session(
        raw,
        "public",
        rf.public_frames(rf.BASE_NS // 1_000_000)[:2],
        session_id="brokenone",
        first_received_ns=rf.BASE_NS + 4 * HOUR_NS,
        span_ns=HOUR_NS,
        broken_books=True,
    )

    summary = _ingest(tmp_path, days=[DAY])
    manifest = summary["manifests"][0]

    assert manifest["sessions_invalid"] == 1
    assert "books_sequence_gap" in manifest["invalid_reason_codes"]
    assert manifest["quarantined_paths"] == [
        str(tmp_path / "quarantine" / broken.relative_to(raw))
    ]
    # 正規化されたのは valid な 2 session だけ。
    assert summary["normalize"]["normalized"] == 2
    normalized = [row["raw_path"] for row in read_jsonl(
        tmp_path / "analysis" / "collector" / "normalize_ledger.jsonl"
    )]
    assert str(broken) not in normalized


def test_rerun_reuses_shards_and_reproduces_the_same_manifest(tmp_path: Path):
    raw = tmp_path / "raw"
    _aligned_pair(raw, first_ns=rf.BASE_NS, span_ns=HOUR_NS, tag="1")

    first = _ingest(tmp_path, days=[DAY])
    shards_after_first = sorted(str(p) for p in (tmp_path / "normalized").glob("**/*.parquet"))
    second = _ingest(tmp_path, days=[DAY])
    shards_after_second = sorted(str(p) for p in (tmp_path / "normalized").glob("**/*.parquet"))

    assert shards_after_first == shards_after_second
    assert second["normalize"]["normalized"] == 0
    assert second["normalize"]["skipped"] == 2

    keys = ("covered_intervals", "uncovered_intervals", "raw_digest", "sessions_valid")
    assert {k: first["manifests"][0][k] for k in keys} == {
        k: second["manifests"][0][k] for k in keys
    }


def test_days_are_discovered_from_the_gate_ledger_when_not_given(tmp_path: Path):
    raw = tmp_path / "raw"
    _aligned_pair(raw, first_ns=rf.BASE_NS, span_ns=HOUR_NS, tag="1")
    _aligned_pair(
        raw, first_ns=rf.BASE_NS + 86_400 * 10**9, span_ns=HOUR_NS, tag="2"
    )

    summary = _ingest(tmp_path)
    assert summary["days"] == ["2026-08-16", "2026-08-17"]


def test_session_spanning_midnight_is_clipped_into_both_days(tmp_path: Path):
    raw = tmp_path / "raw"
    # 23:00 UTC から 2 時間。前日 1 時間 + 翌日 1 時間へ分かれる。
    start_ns = rf.BASE_NS + 21 * HOUR_NS
    _aligned_pair(raw, first_ns=start_ns, span_ns=2 * HOUR_NS, tag="mid")

    summary = _ingest(tmp_path)
    per_day = {item["date_utc"]: item for item in summary["manifests"]}
    assert per_day["2026-08-16"]["covered_seconds"] == 3600.0
    assert per_day["2026-08-17"]["covered_seconds"] == 3600.0


# --- health ledger と alert -------------------------------------------------


def test_health_ledger_records_the_day_and_a_gap_alert_is_written(tmp_path: Path):
    raw = tmp_path / "raw"
    _aligned_pair(raw, first_ns=rf.BASE_NS, span_ns=HOUR_NS, tag="1")

    summary = _ingest(tmp_path, days=[DAY], max_uncovered_seconds=900.0)

    rows = list(read_jsonl(tmp_path / "analysis" / "collector" / "health_ledger.jsonl"))
    assert len(rows) == 1
    row = rows[0]
    assert row["date_utc"] == "2026-08-16"
    assert row["covered_seconds"] == 3600.0
    assert row["longest_uncovered_seconds"] == 21 * 3600.0
    assert row["sessions_valid"] == 2
    assert row["clock_status"] == "pass"
    assert row["disk_free_bytes"] is None or row["disk_free_bytes"] > 0

    alerts = [json.loads(Path(path).read_text(encoding="utf-8")) for path in summary["alerts"]]
    assert any(item["kind"] == "collection_gap" for item in alerts)


def test_no_gap_alert_when_the_threshold_is_not_exceeded(tmp_path: Path):
    raw = tmp_path / "raw"
    _aligned_pair(raw, first_ns=rf.BASE_NS, span_ns=HOUR_NS, tag="1")

    summary = _ingest(tmp_path, days=[DAY], max_uncovered_seconds=86_400.0)
    assert summary["alerts"] == []


def test_quarantine_raises_its_own_alert(tmp_path: Path):
    raw = tmp_path / "raw"
    rf.write_session(
        raw,
        "public",
        rf.public_frames(rf.BASE_NS // 1_000_000)[:2],
        session_id="alertbroken",
        first_received_ns=rf.BASE_NS,
        span_ns=HOUR_NS,
        broken_books=True,
    )
    rf.write_clock_sample(raw, rf.BASE_NS)

    summary = _ingest(tmp_path, days=[DAY], max_uncovered_seconds=86_400.0)
    kinds = {
        json.loads(Path(path).read_text(encoding="utf-8"))["kind"] for path in summary["alerts"]
    }
    assert kinds == {"quarantined_sessions"}


def test_clock_status_is_reported_as_missing_when_no_sample_exists(tmp_path: Path):
    raw = tmp_path / "raw"
    rf.write_session(
        raw,
        "public",
        rf.public_frames(rf.BASE_NS // 1_000_000),
        session_id="noclockday",
        first_received_ns=rf.BASE_NS,
        span_ns=HOUR_NS,
    )
    manifest = _ingest(tmp_path, days=[DAY])["manifests"][0]
    assert manifest["clock_status"] == "missing"
    assert manifest["sessions_valid"] == 0
    assert manifest["covered_seconds"] == 0.0
    assert manifest["uncovered_seconds"] == 86_400.0


def test_cli_writes_a_summary_and_reports_failure_on_quarantine(tmp_path: Path, capsys):
    raw = tmp_path / "raw"
    rf.write_session(
        raw,
        "public",
        rf.public_frames(rf.BASE_NS // 1_000_000)[:2],
        session_id="clibroken",
        first_received_ns=rf.BASE_NS,
        span_ns=HOUR_NS,
        broken_books=True,
    )
    rf.write_clock_sample(raw, rf.BASE_NS)

    code = di.main(
        [
            "--raw-dir",
            str(raw),
            "--output-root",
            str(tmp_path / "normalized"),
            "--quarantine-dir",
            str(tmp_path / "quarantine"),
            "--analysis-dir",
            str(tmp_path / "analysis"),
            "--settle-seconds",
            "0",
            "--output",
            str(tmp_path / "summary.json"),
        ]
    )
    assert code == 1
    summary = json.loads((tmp_path / "summary.json").read_text(encoding="utf-8"))
    assert summary["gate"]["counts"]["invalid"] == 1
    capsys.readouterr()


def test_clock_status_is_scoped_to_the_day_being_declared(tmp_path: Path):
    raw = tmp_path / "raw"
    # 8/16 は clock sample つきで健全、8/17 は sample 無しで不健全。
    _aligned_pair(raw, first_ns=rf.BASE_NS, span_ns=HOUR_NS, tag="ok")
    rf.write_session(
        raw,
        "public",
        rf.public_frames((rf.BASE_NS + 86_400 * 10**9) // 1_000_000),
        session_id="nextdaybad",
        first_received_ns=rf.BASE_NS + 86_400 * 10**9,
        span_ns=HOUR_NS,
    )

    per_day = {item["date_utc"]: item for item in _ingest(tmp_path)["manifests"]}
    assert per_day["2026-08-16"]["clock_status"] == "pass"
    # sample 自体は(前日分が)存在するので `missing` ではなく、窓を覆えていない `fail`。
    assert per_day["2026-08-17"]["clock_status"] == "fail"


def test_sequence_gaps_count_quarantined_sessions_too(tmp_path: Path):
    raw = tmp_path / "raw"
    _aligned_pair(raw, first_ns=rf.BASE_NS, span_ns=HOUR_NS, tag="1")
    rf.write_session(
        raw,
        "public",
        rf.public_frames(rf.BASE_NS // 1_000_000)[:2],
        session_id="gapone",
        first_received_ns=rf.BASE_NS + 4 * HOUR_NS,
        span_ns=HOUR_NS,
        broken_books=True,
    )

    manifest = _ingest(tmp_path, days=[DAY])["manifests"][0]
    # 隔離した session の gap を隠すと「その日は綺麗だった」ように見えてしまう。
    assert manifest["sequence_gaps"] == 1
    assert manifest["sessions_valid"] == 2

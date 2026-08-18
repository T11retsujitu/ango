"""日次 ingest orchestration と収集日台帳(P0-C / P0-D)。

新規の closed raw だけを対象に、決定的に次を行う::

    1. gate ledger から対象 session を発見する(:mod:`mce.session_gate`)
    2. quality を実行し、report digest を保存する
    3. `valid` な session だけを normalized shard へ変換する
    4. shard manifest と raw digest を更新する
    5. UTC 日単位の collection day manifest を追記保存する

日次 manifest は「その日の収集が成功した」宣言ではなく、**何がどの品質で残ったか**
を表す。したがって `uncovered_intervals` は隠さずそのまま出す。欠損はゼロ埋めせず、
欠損として残すことが research 上の価値である。

health ledger は同じ 1 回の実行から作る append-only JSONL で、無収集時間・
sequence gap・clock 品質・空き容量・quarantine 件数を時系列に残す。閾値を越えた
場合はローカルの ``data/analysis/alerts/`` に artifact を書く。外部通知はここでは
行わない。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

from mce import config
from mce.artifacts import append_jsonl, atomic_write_json, read_jsonl
from mce.normalize_microstructure import (
    DEFAULT_NORMALIZED_OUTPUT_ROOT,
    MicrostructureNormalizationError,
    normalize_raw_file,
)
from mce.session_gate import (
    DECISION_INVALID,
    DECISION_VALID,
    gate_ledger_path,
    run_gate,
)

DAY_MANIFEST_SCHEMA_VERSION = 1
HEALTH_LEDGER_SCHEMA_VERSION = 1
NS_PER_SECOND = 1_000_000_000
NS_PER_DAY = 86_400 * NS_PER_SECOND

EXPECTED_CHANNELS = ("trades", "bbo-tbt", "books", "trades-all")

#: 無収集がこの秒数を越えた日は alert artifact を書く(既定 15 分)。
DEFAULT_MAX_UNCOVERED_SECONDS = 900.0


@dataclass(frozen=True)
class Interval:
    """UTC ns の半開区間 ``[start, end)``。"""

    start_ns: int
    end_ns: int

    def __post_init__(self) -> None:
        if self.end_ns < self.start_ns:
            raise ValueError("interval end must be >= start")

    @property
    def seconds(self) -> float:
        return (self.end_ns - self.start_ns) / NS_PER_SECOND

    def as_dict(self) -> dict[str, Any]:
        return {
            "start": _iso(self.start_ns),
            "end": _iso(self.end_ns),
            "start_ns": self.start_ns,
            "end_ns": self.end_ns,
            "seconds": self.seconds,
        }


def _iso(ns: int) -> str:
    return datetime.fromtimestamp(ns / NS_PER_SECOND, tz=timezone.utc).isoformat()


def day_bounds_ns(day: date) -> tuple[int, int]:
    """UTC 日の ``[00:00, 翌 00:00)`` を ns で返す。"""

    start = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
    return int(start.timestamp()) * NS_PER_SECOND, int(
        (start + timedelta(days=1)).timestamp()
    ) * NS_PER_SECOND


def day_of_ns(value: int) -> date:
    return datetime.fromtimestamp(value / NS_PER_SECOND, tz=timezone.utc).date()


def merge_intervals(intervals: Iterable[Interval]) -> list[Interval]:
    """重なり・隣接を潰して昇順の非重複区間にする。"""

    ordered = sorted(intervals, key=lambda item: (item.start_ns, item.end_ns))
    merged: list[Interval] = []
    for interval in ordered:
        if merged and interval.start_ns <= merged[-1].end_ns:
            if interval.end_ns > merged[-1].end_ns:
                merged[-1] = Interval(merged[-1].start_ns, interval.end_ns)
            continue
        merged.append(interval)
    return merged


def complement_intervals(
    covered: Sequence[Interval], start_ns: int, end_ns: int
) -> list[Interval]:
    """``[start_ns, end_ns)`` のうち covered に含まれない区間。"""

    gaps: list[Interval] = []
    cursor = start_ns
    for interval in covered:
        if interval.end_ns <= start_ns or interval.start_ns >= end_ns:
            continue
        begin = max(interval.start_ns, start_ns)
        if begin > cursor:
            gaps.append(Interval(cursor, begin))
        cursor = max(cursor, min(interval.end_ns, end_ns))
    if cursor < end_ns:
        gaps.append(Interval(cursor, end_ns))
    return gaps


def clip_interval(interval: Interval, start_ns: int, end_ns: int) -> Interval | None:
    begin = max(interval.start_ns, start_ns)
    finish = min(interval.end_ns, end_ns)
    if finish <= begin:
        return None
    return Interval(begin, finish)


def _gate_records(ledger_path: Path) -> list[dict[str, Any]]:
    """gate ledger の最新判定(raw_path 単位)を返す。"""

    latest: dict[str, dict[str, Any]] = {}
    for record in read_jsonl(ledger_path):
        path = record.get("raw_path")
        if isinstance(path, str):
            latest[path] = record
    return [latest[key] for key in sorted(latest)]


def normalized_ledger_path(analysis_dir: Path | None = None) -> Path:
    base = Path(analysis_dir if analysis_dir is not None else config.ANALYSIS_DIR)
    return base / "collector" / "normalize_ledger.jsonl"


def health_ledger_path(analysis_dir: Path | None = None) -> Path:
    base = Path(analysis_dir if analysis_dir is not None else config.ANALYSIS_DIR)
    return base / "collector" / "health_ledger.jsonl"


def day_manifest_path(days_dir: Path, day: date) -> Path:
    return Path(days_dir) / f"collection_day_manifest_{day.isoformat()}.json"


def normalize_valid_sessions(
    records: Sequence[dict[str, Any]],
    *,
    output_root: Path,
    ledger_path: Path,
    now_ns: int,
) -> dict[str, Any]:
    """valid session だけを正規化する。既に正規化済みの raw は再実行しない。

    normalize 側が immutable shard の内容一致を検証するので、同じ raw の再実行は
    同じ shard を再利用する。ここでは ledger による skip を重ねて I/O も省く。
    """

    done = {
        record.get("raw_path")
        for record in read_jsonl(ledger_path)
        if record.get("status") == "normalized"
    }
    normalized: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    skipped = 0
    for record in records:
        raw_path = record.get("raw_path")
        if not isinstance(raw_path, str):
            continue
        if raw_path in done:
            skipped += 1
            continue
        path = Path(raw_path)
        if not path.exists():
            failures.append(
                {
                    "raw_path": raw_path,
                    "error": "valid raw file is missing",
                }
            )
            continue
        try:
            result = normalize_raw_file(path, output_root)
        except (MicrostructureNormalizationError, OSError, ValueError) as exc:
            failures.append({"raw_path": raw_path, "error": f"{type(exc).__name__}: {exc}"})
            append_jsonl(
                ledger_path,
                {
                    "normalize_ledger_schema_version": 1,
                    "raw_path": raw_path,
                    "status": "failed",
                    "error": f"{type(exc).__name__}: {exc}",
                    "recorded_at": _iso(now_ns),
                },
            )
            continue
        entry = {
            "raw_path": raw_path,
            "raw_archive_sha256": result.raw_archive_sha256,
            "raw_logical_sha256": result.raw_logical_sha256,
            "row_counts": dict(result.row_counts),
            "output_paths": [str(item) for item in result.output_paths],
        }
        normalized.append(entry)
        append_jsonl(
            ledger_path,
            {
                "normalize_ledger_schema_version": 1,
                "status": "normalized",
                "recorded_at": _iso(now_ns),
                "output_root": str(output_root),
                **entry,
            },
        )
    return {
        "normalized": normalized,
        "failures": failures,
        "skipped": skipped,
    }


def _session_intervals_by_day(
    records: Sequence[dict[str, Any]],
) -> dict[date, dict[str, list[Interval]]]:
    """valid session の受信窓を UTC 日 × stream へ割り当てる。"""

    by_day: dict[date, dict[str, list[Interval]]] = {}
    for record in records:
        first = record.get("first_received_at_ns")
        last = record.get("last_received_at_ns")
        stream = record.get("stream")
        if not isinstance(first, int) or not isinstance(last, int) or last < first:
            continue
        if not isinstance(stream, str):
            continue
        cursor = first
        while cursor <= last:
            day = day_of_ns(cursor)
            start_ns, end_ns = day_bounds_ns(day)
            clipped = clip_interval(Interval(first, last), start_ns, end_ns)
            if clipped is not None:
                by_day.setdefault(day, {}).setdefault(stream, []).append(clipped)
            cursor = end_ns
    return by_day


def build_day_manifest(
    day: date,
    *,
    gate_records: Sequence[dict[str, Any]],
    normalize_result: dict[str, Any],
    now_ns: int,
    source_commit: str | None,
    require_clock_quality: bool = True,
) -> dict[str, Any]:
    """1 UTC 日について「何がどの品質で残ったか」を宣言する manifest を作る。"""

    start_ns, end_ns = day_bounds_ns(day)
    # 未来は無収集ではない。当日は「今まで」を評価対象にする。
    horizon_ns = min(end_ns, now_ns)

    valid_records = [
        record
        for record in gate_records
        if record.get("decision") == DECISION_VALID
        and _touches_day(record, start_ns, end_ns)
    ]
    invalid_records = [
        record
        for record in gate_records
        if record.get("decision") == DECISION_INVALID
        and _touches_day(record, start_ns, end_ns)
    ]

    per_stream = _session_intervals_by_day(valid_records).get(day, {})
    covered_by_stream = {
        stream: merge_intervals(items) for stream, items in sorted(per_stream.items())
    }
    # 「収集できていた」と言えるのは、必要な全 stream が同時に生きていた区間だけ。
    covered = _intersect_all(list(covered_by_stream.values())) if covered_by_stream else []
    uncovered = (
        complement_intervals(covered, start_ns, horizon_ns) if horizon_ns > start_ns else []
    )

    # gap は valid / invalid の両方を数える。invalid を隠すと「その日は綺麗だった」
    # ように見えてしまう。
    sequence_gaps = sum(
        int(record.get("books_sequence_gaps", 0) or 0)
        for record in (*valid_records, *invalid_records)
    )
    clock_status = _clock_status_from_records(invalid_records, require_clock_quality)
    normalized_by_raw = {
        entry["raw_path"]: entry for entry in normalize_result.get("normalized", [])
    }
    shard_digests = sorted(
        {
            entry["raw_archive_sha256"]
            for record in valid_records
            if (entry := normalized_by_raw.get(record.get("raw_path"))) is not None
        }
    )
    raw_digest_body = "\n".join(
        f"{record.get('raw_path')} {record.get('raw_sha256')}"
        for record in sorted(valid_records, key=lambda item: str(item.get("raw_path")))
    )

    return {
        "manifest_schema_version": DAY_MANIFEST_SCHEMA_VERSION,
        "date_utc": day.isoformat(),
        "generated_at": _iso(now_ns),
        "expected_channels": list(EXPECTED_CHANNELS),
        "observed_streams": sorted(covered_by_stream),
        "covered_intervals": [item.as_dict() for item in covered],
        "uncovered_intervals": [item.as_dict() for item in uncovered],
        "covered_seconds": sum(item.seconds for item in covered),
        "uncovered_seconds": sum(item.seconds for item in uncovered),
        "evaluated_seconds": max(0.0, (horizon_ns - start_ns) / NS_PER_SECOND),
        "sessions_valid": len(valid_records),
        "sessions_invalid": len(invalid_records),
        "sequence_gaps": sequence_gaps,
        "clock_status": clock_status,
        "raw_digest": _sha256_text(raw_digest_body),
        "normalized_shard_digests": shard_digests,
        "quarantined_paths": sorted(
            str(record.get("quarantine_path"))
            for record in invalid_records
            if record.get("quarantine_path")
        ),
        "invalid_reason_codes": sorted(
            {
                code
                for record in invalid_records
                for code in record.get("reasons", [])
                if isinstance(code, str)
            }
        ),
        "source_commit": source_commit,
    }


def _touches_day(record: dict[str, Any], start_ns: int, end_ns: int) -> bool:
    first = record.get("first_received_at_ns")
    last = record.get("last_received_at_ns")
    if isinstance(first, int) and isinstance(last, int):
        return first < end_ns and last >= start_ns
    # 受信窓を持たない record(REST snapshot 等)は判定時刻で日に割り当てる。
    decided = record.get("decided_at_ns")
    return isinstance(decided, int) and start_ns <= decided < end_ns


def _intersect_all(groups: Sequence[Sequence[Interval]]) -> list[Interval]:
    """全 stream に共通して覆われている区間だけを残す。"""

    if not groups:
        return []
    current = list(groups[0])
    for group in groups[1:]:
        merged: list[Interval] = []
        for left in current:
            for right in group:
                begin = max(left.start_ns, right.start_ns)
                finish = min(left.end_ns, right.end_ns)
                if finish > begin:
                    merged.append(Interval(begin, finish))
        current = merge_intervals(merged)
        if not current:
            return []
    return current


def _sha256_text(body: str) -> str:
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _disk_free(path: Path) -> int | None:
    probe = Path(path)
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    try:
        return shutil.disk_usage(probe).free
    except OSError:
        return None


def run_daily_ingest(
    *,
    raw_dir: Path | None = None,
    output_root: Path | None = None,
    quarantine_dir: Path | None = None,
    analysis_dir: Path | None = None,
    days: Sequence[date] | None = None,
    now_ns: int | None = None,
    settle_seconds: float | None = None,
    require_clock_quality: bool = True,
    max_uncovered_seconds: float = DEFAULT_MAX_UNCOVERED_SECONDS,
    source_commit: str | None = None,
) -> dict[str, Any]:
    """gate → normalize → day manifest → health ledger を 1 回分実行する。"""

    raw_dir = Path(raw_dir if raw_dir is not None else config.RAW_DIR)
    output_root = Path(
        output_root if output_root is not None else DEFAULT_NORMALIZED_OUTPUT_ROOT
    )
    analysis_dir = Path(analysis_dir if analysis_dir is not None else config.ANALYSIS_DIR)
    quarantine_dir = Path(
        quarantine_dir if quarantine_dir is not None else config.QUARANTINE_DIR
    )
    now_ns = now_ns if now_ns is not None else time.time_ns()
    ledger = gate_ledger_path(analysis_dir)

    gate_kwargs: dict[str, Any] = {
        "raw_dir": raw_dir,
        "quarantine_dir": quarantine_dir,
        "ledger_path": ledger,
        "now_ns": now_ns,
        "require_clock_quality": require_clock_quality,
    }
    if settle_seconds is not None:
        gate_kwargs["settle_seconds"] = settle_seconds
    gate_summary = run_gate(**gate_kwargs)

    records = _gate_records(ledger)
    valid_records = [item for item in records if item.get("decision") == DECISION_VALID]
    normalize_result = normalize_valid_sessions(
        valid_records,
        output_root=output_root,
        ledger_path=normalized_ledger_path(analysis_dir),
        now_ns=now_ns,
    )

    target_days = list(days) if days is not None else _days_from_records(records)
    days_dir = analysis_dir / "collection_days"
    manifests: list[dict[str, Any]] = []
    alerts: list[str] = []
    free_bytes = _disk_free(raw_dir)
    for day in target_days:
        manifest = build_day_manifest(
            day,
            gate_records=records,
            normalize_result=normalize_result,
            now_ns=now_ns,
            source_commit=source_commit,
            require_clock_quality=require_clock_quality,
        )
        atomic_write_json(day_manifest_path(days_dir, day), manifest)
        manifests.append(manifest)

        health = {
            "health_ledger_schema_version": HEALTH_LEDGER_SCHEMA_VERSION,
            "date_utc": day.isoformat(),
            "recorded_at": _iso(now_ns),
            "recorded_at_ns": now_ns,
            "covered_seconds": manifest["covered_seconds"],
            "uncovered_seconds": manifest["uncovered_seconds"],
            "evaluated_seconds": manifest["evaluated_seconds"],
            "longest_uncovered_seconds": max(
                (item["seconds"] for item in manifest["uncovered_intervals"]), default=0.0
            ),
            "sessions_valid": manifest["sessions_valid"],
            "sessions_invalid": manifest["sessions_invalid"],
            "sequence_gaps": manifest["sequence_gaps"],
            "clock_status": manifest["clock_status"],
            "quarantined_count": len(manifest["quarantined_paths"]),
            "disk_free_bytes": free_bytes,
            "normalized_sessions": len(normalize_result["normalized"]),
            "normalize_failures": len(normalize_result["failures"]),
        }
        append_jsonl(health_ledger_path(analysis_dir), health)

        if health["longest_uncovered_seconds"] > max_uncovered_seconds:
            path = _write_alert(
                analysis_dir / "alerts",
                "collection_gap",
                {
                    "date_utc": day.isoformat(),
                    "longest_uncovered_seconds": health["longest_uncovered_seconds"],
                    "threshold_seconds": max_uncovered_seconds,
                    "uncovered_intervals": manifest["uncovered_intervals"][:20],
                },
                now_ns=now_ns,
            )
            alerts.append(str(path))
        if manifest["sessions_invalid"]:
            path = _write_alert(
                analysis_dir / "alerts",
                "quarantined_sessions",
                {
                    "date_utc": day.isoformat(),
                    "sessions_invalid": manifest["sessions_invalid"],
                    "invalid_reason_codes": manifest["invalid_reason_codes"],
                },
                now_ns=now_ns,
            )
            alerts.append(str(path))

    return {
        "ingest_schema_version": DAY_MANIFEST_SCHEMA_VERSION,
        "generated_at": _iso(now_ns),
        "gate": gate_summary,
        "normalize": {
            "normalized": len(normalize_result["normalized"]),
            "skipped": normalize_result["skipped"],
            "failures": normalize_result["failures"],
        },
        "days": [manifest["date_utc"] for manifest in manifests],
        "manifests": manifests,
        "alerts": alerts,
        "output_root": str(output_root),
    }


def _days_from_records(records: Sequence[dict[str, Any]]) -> list[date]:
    days: set[date] = set()
    for record in records:
        first = record.get("first_received_at_ns")
        last = record.get("last_received_at_ns")
        if isinstance(first, int) and isinstance(last, int) and last >= first:
            cursor = first
            while cursor <= last:
                day = day_of_ns(cursor)
                days.add(day)
                cursor = day_bounds_ns(day)[1]
            continue
        decided = record.get("decided_at_ns")
        if isinstance(decided, int):
            days.add(day_of_ns(decided))
    return sorted(days)


def _clock_status_from_records(
    invalid_records: Sequence[dict[str, Any]], require_clock_quality: bool
) -> str:
    """その日の invalid 理由から clock 品質の状態を要約する。

    gate を通った session は定義上 clock sample に覆われているので、
    `pass` は「その日に採用した session の窓を clock raw が覆っていた」を意味する。
    """

    if not require_clock_quality:
        return "missing"
    codes = {
        code
        for record in invalid_records
        for code in record.get("reasons", [])
        if isinstance(code, str)
    }
    if "clock_quality_missing" in codes:
        return "missing"
    if "clock_quality_uncovered" in codes:
        return "fail"
    return "pass"


def _write_alert(
    alert_dir: Path, kind: str, payload: dict[str, Any], *, now_ns: int
) -> Path:
    stamp = datetime.fromtimestamp(now_ns / NS_PER_SECOND, tz=timezone.utc).strftime(
        "%Y%m%dT%H%M%S.%fZ"
    )
    path = Path(alert_dir) / f"{stamp}_{kind}.json"
    atomic_write_json(
        path,
        {
            "alert_schema_version": 1,
            "kind": kind,
            "raised_at": _iso(now_ns),
            "raised_at_ns": now_ns,
            **payload,
        },
    )
    return path


def _parse_day(value: str) -> date:
    return date.fromisoformat(value)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="closed raw を検証・正規化し、UTC 日単位の収集台帳を更新する",
    )
    parser.add_argument("--raw-dir", type=Path, default=config.RAW_DIR)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_NORMALIZED_OUTPUT_ROOT)
    parser.add_argument("--quarantine-dir", type=Path, default=config.QUARANTINE_DIR)
    parser.add_argument("--analysis-dir", type=Path, default=config.ANALYSIS_DIR)
    parser.add_argument(
        "--day",
        dest="days",
        action="append",
        type=_parse_day,
        help="対象 UTC 日(YYYY-MM-DD)。省略時は gate ledger にある全日",
    )
    parser.add_argument("--settle-seconds", type=float, default=None)
    parser.add_argument(
        "--max-uncovered-seconds",
        type=float,
        default=DEFAULT_MAX_UNCOVERED_SECONDS,
        help="この長さを越える無収集区間があれば alert artifact を書く",
    )
    parser.add_argument(
        "--skip-clock-quality-gate",
        action="store_true",
        help="clock quality の同時取得を必須にしない(T0 評価不可)",
    )
    parser.add_argument("--output", type=Path, help="実行要約 JSON を atomic write する path")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    from mce.collector_supervisor import source_commit as _source_commit

    args = _build_parser().parse_args(argv)
    summary = run_daily_ingest(
        raw_dir=args.raw_dir,
        output_root=args.output_root,
        quarantine_dir=args.quarantine_dir,
        analysis_dir=args.analysis_dir,
        days=args.days,
        settle_seconds=args.settle_seconds,
        require_clock_quality=not args.skip_clock_quality_gate,
        max_uncovered_seconds=args.max_uncovered_seconds,
        source_commit=_source_commit(),
    )
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True, indent=2))
    if args.output is not None:
        atomic_write_json(args.output, summary)
    failed = bool(summary["normalize"]["failures"]) or bool(
        summary["gate"]["counts"][DECISION_INVALID]
    )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

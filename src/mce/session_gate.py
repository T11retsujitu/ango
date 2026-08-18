"""closed raw session の品質ゲートと quarantine(P0-B)。

収集後の raw を、分析が参照してよい状態かどうかで機械判定する::

    closed *.jsonl.gz
        ├─ valid      → normalized へ昇格してよい
        ├─ invalid    → quarantine へ隔離し、理由を台帳化する
        └─ pending    → まだ close していない / 落ち着いていない

原則:

- **不完全な区間をゼロ埋め・補間・暗黙 skip しない**。判定は 3 値だけで、
  「たぶん大丈夫」は作らない。
- **invalid を削除しない**。raw は再取得不能なので、quarantine へ移して
  再調査可能な状態で残す。移動は同一 filesystem 内の rename を優先し、
  跨ぐ場合だけ copy → verify → unlink する。
- 判定は :mod:`mce.microstructure_quality` の結果を土台にし、gate 固有の
  追加条件(clock 品質の同時取得、必須 channel の無受信)を重ねる。

gate ledger は append-only JSONL で、日次 ingest はこれを唯一の昇格条件にする。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

from mce import config
from mce.artifacts import append_jsonl, atomic_write_json, read_jsonl
from mce.microstructure_quality import REQUIRED_CHANNELS, analyze_file

GATE_LEDGER_SCHEMA_VERSION = 1

DECISION_VALID = "valid"
DECISION_INVALID = "invalid"
DECISION_PENDING = "pending"

#: WS raw の close 直後は rename が完了していても mtime が動きうる。この秒数
#: 落ち着くまでは pending として次回に回す(収集を止めないための保守側倒し)。
DEFAULT_SETTLE_SECONDS = 60.0

#: session 窓の前後どれだけ離れた clock sample までを「同時に取れていた」と
#: みなすか。collector 既定の sample 間隔 60 秒の余裕を見る。
DEFAULT_CLOCK_TOLERANCE_SECONDS = 300.0

#: この秒数以上続いた session では、必須 channel の無受信を欠陥として扱う。
DEFAULT_MIN_SESSION_SECONDS_FOR_CHANNEL_CHECK = 120.0


class GateError(RuntimeError):
    """gate が安全に判定・隔離できなかった。"""


@dataclass(frozen=True)
class GateDecision:
    """1 raw file の判定結果。"""

    path: Path
    decision: str
    reasons: tuple[dict[str, Any], ...]
    report: dict[str, Any]
    quarantine_path: Path | None = None
    raw_sha256: str | None = None

    @property
    def valid(self) -> bool:
        return self.decision == DECISION_VALID


def _reason(code: str, message: str, **context: Any) -> dict[str, Any]:
    record = {"code": code, "message": message}
    if context:
        record["context"] = context
    return record


def _iso(ns: int) -> str:
    return datetime.fromtimestamp(ns / 1_000_000_000, tz=timezone.utc).isoformat()


def file_sha256(path: Path | str, *, chunk_bytes: int = 1024 * 1024) -> str:
    """raw archive そのものの SHA-256(quality report と対で台帳化する)。"""

    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_bytes), b""):
            digest.update(chunk)
    return digest.hexdigest()


def report_sha256(report: dict[str, Any]) -> str:
    """quality report の指紋。判定の再現性を後から検証できるようにする。"""

    body = json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def discover_closed_sessions(raw_dir: Path | str) -> list[Path]:
    """gate 対象の closed raw を列挙する。``.partial`` は対象外。"""

    raw_dir = Path(raw_dir)
    found: set[Path] = set()
    for pattern in (
        "okx/ws/**/*.jsonl.gz",
        "okx/rest/instruments/**/*.jsonl.gz",
    ):
        found.update(path for path in raw_dir.glob(pattern) if path.is_file())
    return sorted(found, key=str)


def open_partial_sessions(raw_dir: Path | str) -> list[Path]:
    """まだ close していない書込み中ファイル(常に pending)。"""

    raw_dir = Path(raw_dir)
    return sorted(
        (path for path in raw_dir.glob("okx/**/*.jsonl.gz.partial") if path.is_file()),
        key=str,
    )


def clock_sample_ns_values(raw_dir: Path | str) -> list[int]:
    """clock quality sample の wall ns を昇順で返す(file 名だけを読む)。"""

    values: list[int] = []
    for candidate in (Path(raw_dir) / "host" / "clock_quality").glob("**/*.jsonl.gz"):
        parts = candidate.name.split("_")
        if len(parts) >= 2 and parts[1].isdigit():
            values.append(int(parts[1]))
    return sorted(values)


def _covers_window(
    samples: Sequence[int],
    first_ns: int,
    last_ns: int,
    tolerance_seconds: float,
) -> bool:
    tolerance_ns = int(tolerance_seconds * 1_000_000_000)
    return any(first_ns - tolerance_ns <= value <= last_ns + tolerance_ns for value in samples)


def _is_ws_session(report: dict[str, Any]) -> bool:
    return isinstance(report.get("session", {}).get("stream"), str)


def gate_reasons(
    report: dict[str, Any],
    *,
    clock_samples: Sequence[int],
    require_clock_quality: bool = True,
    clock_tolerance_seconds: float = DEFAULT_CLOCK_TOLERANCE_SECONDS,
    min_session_seconds_for_channel_check: float = (
        DEFAULT_MIN_SESSION_SECONDS_FOR_CHANNEL_CHECK
    ),
) -> list[dict[str, Any]]:
    """quality report に gate 固有の追加条件を重ねて invalid 理由を返す。"""

    reasons: list[dict[str, Any]] = list(report.get("reasons", []))
    if not _is_ws_session(report):
        # REST instruments snapshot は WS session の条件を持たない。
        return reasons

    session = report.get("session", {})
    stream = session.get("stream")
    first_ns = session.get("first_received_at_ns")
    last_ns = session.get("last_received_at_ns")

    if require_clock_quality:
        if not clock_samples:
            reasons.append(
                _reason(
                    "clock_quality_missing",
                    "no clock quality sample exists; session cannot be used for T0 evaluation",
                )
            )
        elif isinstance(first_ns, int) and isinstance(last_ns, int):
            if not _covers_window(clock_samples, first_ns, last_ns, clock_tolerance_seconds):
                reasons.append(
                    _reason(
                        "clock_quality_uncovered",
                        "no clock quality sample overlaps the session window",
                        first_received_at_ns=first_ns,
                        last_received_at_ns=last_ns,
                        tolerance_seconds=clock_tolerance_seconds,
                    )
                )

    duration = session.get("duration_seconds")
    if (
        isinstance(duration, (int, float))
        and duration >= min_session_seconds_for_channel_check
    ):
        channels = report.get("channels", {})
        for channel in sorted(REQUIRED_CHANNELS.get(stream, set())):
            messages = int(channels.get(channel, {}).get("messages", 0))
            if messages == 0:
                reasons.append(
                    _reason(
                        "channel_silent",
                        "required channel received no message for the whole session",
                        stream=stream,
                        channel=channel,
                        duration_seconds=duration,
                    )
                )
    return reasons


def quarantine_target(raw_dir: Path, quarantine_dir: Path, path: Path) -> Path:
    """raw 配下の相対構造をそのまま保った隔離先。"""

    raw_dir = Path(raw_dir).resolve()
    resolved = Path(path).resolve()
    try:
        relative = resolved.relative_to(raw_dir)
    except ValueError:
        relative = Path(resolved.name)
    return Path(quarantine_dir) / relative


def move_to_quarantine(source: Path, target: Path) -> Path:
    """raw を削除せず隔離する。同一 fs なら rename、跨ぐなら copy→verify→unlink。"""

    source = Path(source)
    target = Path(target)
    if target.exists():
        # 同じ session を二度隔離しようとしている。上書きは証拠を壊すので拒否する。
        if file_sha256(target) == file_sha256(source):
            source.unlink()
            return target
        raise GateError(f"quarantine target already exists with different bytes: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.replace(source, target)
        return target
    except OSError:
        # cross-device link。copy してから hash 一致を確認できた場合のみ元を消す。
        pass
    shutil.copy2(source, target)
    if file_sha256(source) != file_sha256(target):
        target.unlink()
        raise GateError(f"quarantine copy did not match source bytes: {source}")
    source.unlink()
    return target


def gate_ledger_path(analysis_dir: Path | None = None) -> Path:
    base = Path(analysis_dir if analysis_dir is not None else config.ANALYSIS_DIR)
    return base / "collector" / "gate_ledger.jsonl"


def gated_paths(ledger_path: Path | str) -> dict[str, dict[str, Any]]:
    """既に判定済みの raw path → 最新 ledger record。再実行を冪等にする。"""

    latest: dict[str, dict[str, Any]] = {}
    for record in read_jsonl(ledger_path):
        path = record.get("raw_path")
        if isinstance(path, str):
            latest[path] = record
    return latest


def evaluate_session(
    path: Path | str,
    *,
    clock_samples: Sequence[int],
    now_ns: int,
    settle_seconds: float = DEFAULT_SETTLE_SECONDS,
    require_clock_quality: bool = True,
) -> GateDecision:
    """1 file を判定する。副作用は起こさない(隔離は :func:`run_gate`)。"""

    path = Path(path)
    try:
        modified_ns = path.stat().st_mtime_ns
    except OSError as exc:
        raise GateError(f"failed to stat raw file {path}") from exc
    if (now_ns - modified_ns) < settle_seconds * 1_000_000_000:
        return GateDecision(
            path=path,
            decision=DECISION_PENDING,
            reasons=(
                _reason(
                    "not_settled",
                    "raw file changed too recently to be treated as closed",
                    modified_at=_iso(modified_ns),
                    settle_seconds=settle_seconds,
                ),
            ),
            report={},
        )

    report = analyze_file(path)
    reasons = gate_reasons(
        report,
        clock_samples=clock_samples,
        require_clock_quality=require_clock_quality,
    )
    decision = DECISION_VALID if not reasons else DECISION_INVALID
    return GateDecision(
        path=path,
        decision=decision,
        reasons=tuple(reasons),
        report=report,
        raw_sha256=file_sha256(path),
    )


def run_gate(
    *,
    raw_dir: Path | None = None,
    quarantine_dir: Path | None = None,
    ledger_path: Path | None = None,
    inputs: Iterable[Path | str] | None = None,
    now_ns: int | None = None,
    settle_seconds: float = DEFAULT_SETTLE_SECONDS,
    require_clock_quality: bool = True,
    quarantine: bool = True,
) -> dict[str, Any]:
    """closed raw を判定し、invalid を隔離して ledger を確定する。

    既に ledger にある path は再判定しない。再実行しても同じ結論・同じ隔離先
    になるので、日次ジョブから何度呼んでも安全である。
    """

    raw_dir = Path(raw_dir if raw_dir is not None else config.RAW_DIR)
    quarantine_dir = Path(
        quarantine_dir if quarantine_dir is not None else config.QUARANTINE_DIR
    )
    ledger_path = Path(ledger_path if ledger_path is not None else gate_ledger_path())
    now_ns = now_ns if now_ns is not None else time.time_ns()

    already = gated_paths(ledger_path)
    candidates = (
        [Path(item) for item in inputs]
        if inputs is not None
        else discover_closed_sessions(raw_dir)
    )
    clock_samples = clock_sample_ns_values(raw_dir)

    decisions: list[GateDecision] = []
    counts = {DECISION_VALID: 0, DECISION_INVALID: 0, DECISION_PENDING: 0, "skipped": 0}
    for path in candidates:
        key = str(path)
        if key in already and already[key].get("decision") in {
            DECISION_VALID,
            DECISION_INVALID,
        }:
            counts["skipped"] += 1
            continue
        decision = evaluate_session(
            path,
            clock_samples=clock_samples,
            now_ns=now_ns,
            settle_seconds=settle_seconds,
            require_clock_quality=require_clock_quality,
        )
        if decision.decision == DECISION_INVALID and quarantine:
            target = quarantine_target(raw_dir, quarantine_dir, decision.path)
            moved = move_to_quarantine(decision.path, target)
            atomic_write_json(
                moved.with_name(moved.name + ".quality.json"),
                {
                    "quarantine_schema_version": 1,
                    "original_path": str(decision.path),
                    "quarantined_at": _iso(now_ns),
                    "raw_sha256": decision.raw_sha256,
                    "reasons": list(decision.reasons),
                    "quality_report": decision.report,
                },
            )
            decision = GateDecision(
                path=decision.path,
                decision=decision.decision,
                reasons=decision.reasons,
                report=decision.report,
                quarantine_path=moved,
                raw_sha256=decision.raw_sha256,
            )
        decisions.append(decision)
        counts[decision.decision] += 1
        if decision.decision != DECISION_PENDING:
            session = decision.report.get("session", {})
            append_jsonl(
                ledger_path,
                {
                    "gate_ledger_schema_version": GATE_LEDGER_SCHEMA_VERSION,
                    "raw_path": str(decision.path),
                    "decision": decision.decision,
                    "decided_at": _iso(now_ns),
                    "decided_at_ns": now_ns,
                    "raw_sha256": decision.raw_sha256,
                    "quality_report_sha256": report_sha256(decision.report),
                    "reasons": [item["code"] for item in decision.reasons],
                    "reason_details": list(decision.reasons),
                    "quarantine_path": (
                        str(decision.quarantine_path) if decision.quarantine_path else None
                    ),
                    "stream": session.get("stream"),
                    "session_id": session.get("session_id"),
                    "first_received_at_ns": session.get("first_received_at_ns"),
                    "last_received_at_ns": session.get("last_received_at_ns"),
                    "books_last_seq_id": decision.report.get("books", {}).get("last_seq_id"),
                    "books_sequence_gaps": decision.report.get("books", {}).get(
                        "sequence_gaps", 0
                    ),
                },
            )

    return {
        "gate_schema_version": GATE_LEDGER_SCHEMA_VERSION,
        "evaluated_at": _iso(now_ns),
        "raw_dir": str(raw_dir),
        "ledger_path": str(ledger_path),
        "counts": counts,
        "open_partial_files": [str(path) for path in open_partial_sessions(raw_dir)],
        "valid_paths": [str(item.path) for item in decisions if item.valid],
        "invalid_paths": [
            str(item.path) for item in decisions if item.decision == DECISION_INVALID
        ],
        "pending_paths": [
            str(item.path) for item in decisions if item.decision == DECISION_PENDING
        ],
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="closed raw を valid/invalid/pending へ判定し invalid を quarantine する",
    )
    parser.add_argument("--raw-dir", type=Path, default=config.RAW_DIR)
    parser.add_argument("--quarantine-dir", type=Path, default=config.QUARANTINE_DIR)
    parser.add_argument("--ledger", type=Path, default=None)
    parser.add_argument(
        "--settle-seconds",
        type=float,
        default=DEFAULT_SETTLE_SECONDS,
        help="この秒数以内に更新された file は pending として次回へ回す",
    )
    parser.add_argument(
        "--skip-clock-quality-gate",
        action="store_true",
        help="clock quality の同時取得を必須にしない(T0 評価不可)",
    )
    parser.add_argument(
        "--no-quarantine",
        action="store_true",
        help="判定だけ行い、invalid を移動しない(dry run)",
    )
    parser.add_argument("--output", type=Path, help="要約 JSON を atomic write する path")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    summary = run_gate(
        raw_dir=args.raw_dir,
        quarantine_dir=args.quarantine_dir,
        ledger_path=args.ledger,
        settle_seconds=args.settle_seconds,
        require_clock_quality=not args.skip_clock_quality_gate,
        quarantine=not args.no_quarantine,
    )
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True, indent=2))
    if args.output is not None:
        atomic_write_json(args.output, summary)
    return 1 if summary["counts"][DECISION_INVALID] else 0


if __name__ == "__main__":
    raise SystemExit(main())

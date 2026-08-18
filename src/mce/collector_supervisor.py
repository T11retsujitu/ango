"""collector のプロセス健全性だけを管理する supervisor(P0-A)。

:mod:`mce.collect_microstructure` は収集の責務だけを持つ。OS 再起動、ネットワーク
異常、未捕捉例外、ディスク満杯で child が落ちると、板・BBO・約定は**後から復元
できない**まま欠損する。この module は child を起動し直す最小限の運用層である。

設計上の境界:

- supervisor は **取引所データの内容を判断しない**。板 sequence や約定の整合性は
  :mod:`mce.microstructure_quality` の責務で、supervisor は exit code・signal・
  raw の到着有無・空き容量・時計品質だけを見る。
- **無限に再起動しない**。連続異常終了が上限に達した、空き容量が下限を割った、
  clock 品質が取得できない場合は fail-closed で停止し、欠損を正直に台帳化する。
  壊れた raw を増やす方が、止まって記録するより悪い。
- ledger は append-only JSONL。run と raw session を後から突き合わせられるよう、
  run ごとに 1 行を確定させる。

実行例::

    uv run python -m mce.collector_supervisor -- --inst-id BTC-USDT-SWAP

``--`` 以降は collector へそのまま渡す。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import random
import shutil
import signal
import subprocess
import sys
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Sequence

from mce import config
from mce.artifacts import append_jsonl, atomic_write_json

RUN_LEDGER_SCHEMA_VERSION = 1

#: exit code 0。``--duration`` の満了など、計画された終了。
TERMINATION_CLEAN_EXIT = "clean_exit"
#: SIGINT / SIGTERM。運用者による意図的な停止。
TERMINATION_OPERATOR_STOP = "operator_stop"
#: それ以外の異常終了(未捕捉例外、OOM kill、write error など)。
TERMINATION_CRASH = "crash"
#: child を起動すらできなかった。
TERMINATION_START_FAILED = "start_failed"

#: preflight で収集を続けてはならないと判断した。
STOP_DISK_EXHAUSTED = "disk_below_floor"
STOP_CLOCK_QUALITY = "clock_quality_unavailable"
STOP_RESTART_BUDGET = "restart_budget_exhausted"
STOP_CONSECUTIVE_FAILURES = "consecutive_failure_limit"
STOP_OPERATOR = "operator_stop"
STOP_CLEAN_EXIT = "clean_exit"

_OPERATOR_SIGNALS = {int(signal.SIGINT), int(signal.SIGTERM)}
_STDERR_TAIL_LINES = 40


class SupervisorError(RuntimeError):
    """supervisor 自身が続行できない。"""


@dataclass(frozen=True)
class SupervisorPolicy:
    """再起動を「限定回数」に閉じるための境界値。

    Attributes
    ----------
    max_restarts:
        1 回の supervise 呼び出しで許す再起動の総数。
    max_consecutive_failures:
        連続 crash がこの数に達したら fail-closed で停止する。
    stable_run_seconds:
        この秒数以上動いた run は「一旦安定した」とみなし、連続失敗数を戻す。
    backoff_min_seconds / backoff_max_seconds:
        再起動待機の下限・上限。full jitter を掛ける。
    min_free_disk_bytes:
        preflight の空き容量下限。割った時点で収集を止める(raw は消さない)。
    require_clock_quality:
        clock quality raw を確認できない場合に fail-closed するか。
        T0 評価には時計品質が必要なので既定は True。
    max_clock_sample_age_seconds:
        直近 clock sample がこれより古ければ「取得できていない」とみなす。
    """

    max_restarts: int = 12
    max_consecutive_failures: int = 4
    stable_run_seconds: float = 300.0
    backoff_min_seconds: float = 2.0
    backoff_max_seconds: float = 300.0
    min_free_disk_bytes: int = 5 * 1024**3
    require_clock_quality: bool = True
    max_clock_sample_age_seconds: float = 3600.0

    def __post_init__(self) -> None:
        if self.max_restarts < 0:
            raise ValueError("max_restarts must be >= 0")
        if self.max_consecutive_failures < 1:
            raise ValueError("max_consecutive_failures must be >= 1")
        if not 0 < self.backoff_min_seconds <= self.backoff_max_seconds:
            raise ValueError("backoff bounds must satisfy 0 < min <= max")
        if self.min_free_disk_bytes < 0:
            raise ValueError("min_free_disk_bytes must be >= 0")


@dataclass(frozen=True)
class ChildOutcome:
    """child process 1 回分の結果。内容判断は含まない。"""

    exit_code: int | None
    signal_number: int | None = None
    stderr_tail: str = ""
    error: str | None = None

    @property
    def termination_kind(self) -> str:
        if self.error is not None:
            return TERMINATION_START_FAILED
        if self.signal_number is not None:
            return (
                TERMINATION_OPERATOR_STOP
                if self.signal_number in _OPERATOR_SIGNALS
                else TERMINATION_CRASH
            )
        if self.exit_code == 0:
            return TERMINATION_CLEAN_EXIT
        return TERMINATION_CRASH


@dataclass(frozen=True)
class RunRecord:
    """ledger へ確定した 1 run。"""

    run_id: str
    restart_ordinal: int
    started_at_ns: int
    ended_at_ns: int
    termination_kind: str
    exit_code: int | None
    signal_number: int | None
    backoff_seconds: float
    disk_free_bytes: int | None
    clock_quality_status: str

    @property
    def duration_seconds(self) -> float:
        return max(0.0, (self.ended_at_ns - self.started_at_ns) / 1_000_000_000)


@dataclass
class SupervisorReport:
    """supervise の戻り値。CLI の exit code はここから決める。"""

    supervisor_id: str
    runs: list[RunRecord] = field(default_factory=list)
    stop_reason: str = ""
    stop_detail: dict[str, Any] = field(default_factory=dict)
    alert_paths: list[Path] = field(default_factory=list)

    @property
    def fail_closed(self) -> bool:
        """欠損を伴う停止か(運用者の停止と計画終了だけが正常)。"""

        return self.stop_reason not in {STOP_OPERATOR, STOP_CLEAN_EXIT}


def _iso(ns: int) -> str:
    return datetime.fromtimestamp(ns / 1_000_000_000, tz=timezone.utc).isoformat()


def source_commit(repo_root: Path | None = None) -> str | None:
    """再現性のために実装 commit を記録する。git が無ければ None。"""

    repo_root = repo_root or Path(__file__).resolve().parents[2]
    try:
        completed = subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    revision = completed.stdout.strip()
    return revision or None


def collector_config_sha256(argv: Sequence[str]) -> str:
    """collector 起動引数の指紋。設定変更を run 境界で見分ける。"""

    body = "\n".join(str(item) for item in argv)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def latest_raw_observation(raw_dir: Path | str) -> dict[str, Any]:
    """直近に書かれた raw の座標。欠損区間の上端を確定するために使う。

    ``.partial`` も含めて見る。書きかけのファイルが更新されている限り、
    collector は「受信できていた」からである。内容は一切 parse しない。
    """

    raw_dir = Path(raw_dir)
    latest_path: Path | None = None
    latest_ns: int | None = None
    for pattern in ("okx/ws/**/*.jsonl.gz", "okx/ws/**/*.jsonl.gz.partial"):
        for candidate in raw_dir.glob(pattern):
            try:
                modified_ns = candidate.stat().st_mtime_ns
            except OSError:
                continue
            if latest_ns is None or modified_ns > latest_ns:
                latest_ns = modified_ns
                latest_path = candidate
    if latest_path is None or latest_ns is None:
        return {"path": None, "modified_at_ns": None, "modified_at": None}
    return {
        "path": str(latest_path),
        "modified_at_ns": latest_ns,
        "modified_at": _iso(latest_ns),
    }


def latest_clock_sample_ns(raw_dir: Path | str) -> int | None:
    """直近 clock quality sample の wall ns。file 名に埋まった値だけを読む。"""

    raw_dir = Path(raw_dir)
    latest: int | None = None
    for candidate in (raw_dir / "host" / "clock_quality").glob("**/*.jsonl.gz"):
        parts = candidate.name.split("_")
        if len(parts) < 2 or not parts[1].isdigit():
            continue
        value = int(parts[1])
        if latest is None or value > latest:
            latest = value
    return latest


def clock_quality_status(
    raw_dir: Path | str, *, now_ns: int, max_age_seconds: float
) -> str:
    """``pass`` / ``stale`` / ``missing``。内容の良否ではなく取得できているか。"""

    sample_ns = latest_clock_sample_ns(raw_dir)
    if sample_ns is None:
        return "missing"
    age_seconds = (now_ns - sample_ns) / 1_000_000_000
    return "pass" if age_seconds <= max_age_seconds else "stale"


DiskUsage = Callable[[Path], int]
Runner = Callable[[Sequence[str]], ChildOutcome]


def _default_disk_free(path: Path) -> int:
    probe = Path(path)
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    return shutil.disk_usage(probe).free


class SubprocessRunner:
    """child を実際に起動する既定 runner。stderr の末尾だけ保持する。

    supervisor 自身が SIGINT/SIGTERM を受けた場合、child にも同じ signal を
    転送して clean close(gzip footer + atomic rename)の機会を与える。
    """

    def __init__(self, *, tail_lines: int = _STDERR_TAIL_LINES) -> None:
        self.tail_lines = tail_lines
        self.process: subprocess.Popen[str] | None = None

    def terminate(self, signal_number: int) -> None:
        process = self.process
        if process is not None and process.poll() is None:
            try:
                process.send_signal(signal_number)
            except (OSError, ValueError):
                pass

    def __call__(self, argv: Sequence[str]) -> ChildOutcome:
        tail: deque[str] = deque(maxlen=self.tail_lines)
        try:
            process = subprocess.Popen(
                list(argv),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )
        except OSError as exc:
            return ChildOutcome(exit_code=None, error=str(exc))
        self.process = process
        try:
            assert process.stderr is not None
            for line in process.stderr:
                tail.append(line.rstrip("\n"))
                # collector の stderr は運用者が見る唯一の即時情報なので素通しする。
                print(line, end="", file=sys.stderr)
            returncode = process.wait()
        finally:
            self.process = None
            if process.stderr is not None:
                process.stderr.close()
        if returncode < 0:
            return ChildOutcome(
                exit_code=None,
                signal_number=-returncode,
                stderr_tail="\n".join(tail),
            )
        return ChildOutcome(exit_code=returncode, stderr_tail="\n".join(tail))


def backoff_seconds(
    policy: SupervisorPolicy,
    consecutive_failures: int,
    *,
    jitter: Callable[[], float] = random.random,
) -> float:
    """full jitter の指数backoff。``consecutive_failures`` は 1 始まり。"""

    if consecutive_failures < 1:
        return 0.0
    exponential = policy.backoff_min_seconds * (2 ** (consecutive_failures - 1))
    ceiling = min(policy.backoff_max_seconds, exponential)
    return ceiling * jitter()


def collector_argv(
    extra_args: Sequence[str] = (),
    *,
    executable: str | None = None,
) -> list[str]:
    """child の起動 argv。module 実行にして PATH 依存を作らない。"""

    return [executable or sys.executable, "-m", "mce.collect_microstructure", *extra_args]


def _write_alert(
    alert_dir: Path, kind: str, payload: dict[str, Any], *, now_ns: int
) -> Path:
    """外部通知の前に、まずローカルへ機械可読な alert を残す。"""

    stamp = datetime.fromtimestamp(now_ns / 1_000_000_000, tz=timezone.utc).strftime(
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


def supervise(
    *,
    collector_args: Sequence[str] = (),
    raw_dir: Path | None = None,
    policy: SupervisorPolicy | None = None,
    ledger_path: Path | None = None,
    alert_dir: Path | None = None,
    runner: Runner | None = None,
    clock: Callable[[], int] = time.time_ns,
    sleeper: Callable[[float], None] = time.sleep,
    disk_free: DiskUsage = _default_disk_free,
    jitter: Callable[[], float] = random.random,
    should_stop: Callable[[], bool] = lambda: False,
    supervisor_id: str | None = None,
    commit: str | None = None,
) -> SupervisorReport:
    """collector を監督し、run ごとに ledger を確定して report を返す。

    純粋に注入可能な副作用(runner / clock / sleeper / disk_free)だけを使うので、
    kill・例外・ディスク不足は fixture で再現できる。
    """

    policy = policy or SupervisorPolicy()
    raw_dir = Path(raw_dir if raw_dir is not None else config.RAW_DIR)
    ledger_path = Path(
        ledger_path if ledger_path is not None else config.COLLECTOR_LEDGER_DIR / "run_ledger.jsonl"
    )
    alert_dir = Path(alert_dir if alert_dir is not None else config.ALERTS_DIR)
    runner = runner or SubprocessRunner()
    argv = collector_argv(collector_args)
    config_sha256 = collector_config_sha256(argv)
    commit = commit if commit is not None else source_commit()
    report = SupervisorReport(supervisor_id=supervisor_id or uuid.uuid4().hex)

    host_id = platform.node()
    restart_ordinal = 0
    consecutive_failures = 0
    pending_backoff = 0.0

    while True:
        now_ns = clock()
        free_bytes: int | None
        try:
            free_bytes = int(disk_free(raw_dir))
        except OSError:
            free_bytes = None
        clock_status = clock_quality_status(
            raw_dir, now_ns=now_ns, max_age_seconds=policy.max_clock_sample_age_seconds
        )

        # --- preflight: 収集を「続けてよい状態か」だけを判定する ---
        blocker: str | None = None
        detail: dict[str, Any] = {}
        if free_bytes is not None and free_bytes < policy.min_free_disk_bytes:
            blocker = STOP_DISK_EXHAUSTED
            detail = {
                "disk_free_bytes": free_bytes,
                "min_free_disk_bytes": policy.min_free_disk_bytes,
                "raw_dir": str(raw_dir),
            }
        elif (
            policy.require_clock_quality
            and restart_ordinal > 0
            and clock_status != "pass"
        ):
            # 初回起動時は collector 自身がまだ sample を書いていないので見ない。
            blocker = STOP_CLOCK_QUALITY
            detail = {"clock_quality_status": clock_status, "raw_dir": str(raw_dir)}
        if blocker is not None:
            report.stop_reason = blocker
            report.stop_detail = detail
            report.alert_paths.append(
                _write_alert(
                    alert_dir,
                    blocker,
                    {
                        "supervisor_id": report.supervisor_id,
                        "restart_ordinal": restart_ordinal,
                        "runs_completed": len(report.runs),
                        "detail": detail,
                        "action": "collector stopped fail-closed; gap is intentional and recorded",
                    },
                    now_ns=now_ns,
                )
            )
            break

        if should_stop():
            report.stop_reason = STOP_OPERATOR
            break

        if pending_backoff > 0:
            sleeper(pending_backoff)

        started_at_ns = clock()
        run_id = uuid.uuid4().hex
        outcome = runner(argv)
        ended_at_ns = clock()
        raw_observation = latest_raw_observation(raw_dir)

        record = RunRecord(
            run_id=run_id,
            restart_ordinal=restart_ordinal,
            started_at_ns=started_at_ns,
            ended_at_ns=ended_at_ns,
            termination_kind=outcome.termination_kind,
            exit_code=outcome.exit_code,
            signal_number=outcome.signal_number,
            backoff_seconds=pending_backoff,
            disk_free_bytes=free_bytes,
            clock_quality_status=clock_status,
        )
        report.runs.append(record)
        append_jsonl(
            ledger_path,
            {
                "run_ledger_schema_version": RUN_LEDGER_SCHEMA_VERSION,
                "supervisor_id": report.supervisor_id,
                "run_id": run_id,
                "host_id": host_id,
                "source_commit": commit,
                "collector_config_sha256": config_sha256,
                "collector_argv": list(argv),
                "restart_ordinal": restart_ordinal,
                "backoff_seconds": pending_backoff,
                "started_at": _iso(started_at_ns),
                "started_at_ns": started_at_ns,
                "ended_at": _iso(ended_at_ns),
                "ended_at_ns": ended_at_ns,
                "duration_seconds": record.duration_seconds,
                "termination_kind": outcome.termination_kind,
                "exit_code": outcome.exit_code,
                "signal": outcome.signal_number,
                "start_error": outcome.error,
                "stderr_tail": outcome.stderr_tail,
                "last_raw_path": raw_observation["path"],
                "last_raw_received_at": raw_observation["modified_at"],
                "last_raw_received_at_ns": raw_observation["modified_at_ns"],
                "disk_free_bytes": free_bytes,
                "clock_quality_status": clock_status,
            },
        )

        kind = outcome.termination_kind
        if kind == TERMINATION_CLEAN_EXIT:
            report.stop_reason = STOP_CLEAN_EXIT
            break
        if kind == TERMINATION_OPERATOR_STOP:
            report.stop_reason = STOP_OPERATOR
            break

        if record.duration_seconds >= policy.stable_run_seconds:
            # 一度は安定稼働した run なので、連続失敗の系列としては数え直す。
            consecutive_failures = 1
        else:
            consecutive_failures += 1

        if consecutive_failures >= policy.max_consecutive_failures:
            report.stop_reason = STOP_CONSECUTIVE_FAILURES
            report.stop_detail = {
                "consecutive_failures": consecutive_failures,
                "max_consecutive_failures": policy.max_consecutive_failures,
                "last_termination_kind": kind,
                "stderr_tail": outcome.stderr_tail,
            }
        elif restart_ordinal >= policy.max_restarts:
            report.stop_reason = STOP_RESTART_BUDGET
            report.stop_detail = {
                "restarts_used": restart_ordinal,
                "max_restarts": policy.max_restarts,
                "last_termination_kind": kind,
            }
        if report.stop_reason:
            report.alert_paths.append(
                _write_alert(
                    alert_dir,
                    report.stop_reason,
                    {
                        "supervisor_id": report.supervisor_id,
                        "runs_completed": len(report.runs),
                        "detail": report.stop_detail,
                        "action": "collector stopped fail-closed; gap is intentional and recorded",
                    },
                    now_ns=ended_at_ns,
                )
            )
            break

        if should_stop():
            report.stop_reason = STOP_OPERATOR
            break

        restart_ordinal += 1
        pending_backoff = backoff_seconds(policy, consecutive_failures, jitter=jitter)

    return report


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="collect_microstructure のプロセス健全性を監督し run ledger を残す",
    )
    parser.add_argument("--raw-dir", type=Path, default=config.RAW_DIR)
    parser.add_argument(
        "--ledger",
        type=Path,
        default=config.COLLECTOR_LEDGER_DIR / "run_ledger.jsonl",
        help="append-only の run ledger JSONL",
    )
    parser.add_argument("--alert-dir", type=Path, default=config.ALERTS_DIR)
    parser.add_argument("--max-restarts", type=int, default=12)
    parser.add_argument("--max-consecutive-failures", type=int, default=4)
    parser.add_argument("--stable-run-seconds", type=float, default=300.0)
    parser.add_argument("--backoff-min-seconds", type=float, default=2.0)
    parser.add_argument("--backoff-max-seconds", type=float, default=300.0)
    parser.add_argument(
        "--min-free-disk-bytes",
        type=int,
        default=5 * 1024**3,
        help="この空き容量を割ったら再起動せず停止する(raw は削除しない)",
    )
    parser.add_argument(
        "--skip-clock-quality-guard",
        action="store_true",
        help="clock quality raw の有無で fail-closed しない(T0 評価不可)",
    )
    parser.add_argument(
        "--max-clock-sample-age-seconds",
        type=float,
        default=3600.0,
    )
    parser.add_argument(
        "collector_args",
        nargs="*",
        help="`--` の後ろに置いた引数をそのまま collector へ渡す",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    policy = SupervisorPolicy(
        max_restarts=args.max_restarts,
        max_consecutive_failures=args.max_consecutive_failures,
        stable_run_seconds=args.stable_run_seconds,
        backoff_min_seconds=args.backoff_min_seconds,
        backoff_max_seconds=args.backoff_max_seconds,
        min_free_disk_bytes=args.min_free_disk_bytes,
        require_clock_quality=not args.skip_clock_quality_guard,
        max_clock_sample_age_seconds=args.max_clock_sample_age_seconds,
    )
    runner = SubprocessRunner()
    stop_requested = False

    def _request_stop(signal_number: int, _frame: Any) -> None:
        nonlocal stop_requested
        stop_requested = True
        # child にも転送し、gzip footer を閉じた clean close の機会を与える。
        runner.terminate(signal_number)

    installed: list[int] = []
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _request_stop)
            installed.append(int(sig))
        except (OSError, ValueError):
            pass

    report = supervise(
        collector_args=args.collector_args,
        raw_dir=args.raw_dir,
        policy=policy,
        ledger_path=args.ledger,
        alert_dir=args.alert_dir,
        runner=runner,
        should_stop=lambda: stop_requested,
    )
    for sig in installed:
        try:
            signal.signal(sig, signal.SIG_DFL)
        except (OSError, ValueError):
            pass

    print(
        json.dumps(
            {
                "supervisor_id": report.supervisor_id,
                "runs": len(report.runs),
                "stop_reason": report.stop_reason,
                "stop_detail": report.stop_detail,
                "fail_closed": report.fail_closed,
                "alerts": [str(path) for path in report.alert_paths],
            },
            ensure_ascii=False,
        )
    )
    return 1 if report.fail_closed else 0


if __name__ == "__main__":
    raise SystemExit(main())

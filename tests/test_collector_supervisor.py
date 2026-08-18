"""collector supervisor(プロセス健全性のみ)。実 child は起動しない。"""

import json
import sys
from pathlib import Path

import pytest

from mce import collector_supervisor as cs
from mce.artifacts import read_jsonl


def _policy(**overrides) -> cs.SupervisorPolicy:
    base = {
        "max_restarts": 5,
        "max_consecutive_failures": 3,
        "stable_run_seconds": 300.0,
        "backoff_min_seconds": 2.0,
        "backoff_max_seconds": 60.0,
        "min_free_disk_bytes": 0,
        "require_clock_quality": False,
    }
    base.update(overrides)
    return cs.SupervisorPolicy(**base)


class FakeClock:
    """run ごとに一定秒進む注入可能な wall clock。"""

    def __init__(self, start_ns: int = 1_700_000_000 * 10**9, step_ns: int = 10**9):
        self.now = start_ns
        self.step = step_ns

    def __call__(self) -> int:
        value = self.now
        self.now += self.step
        return value


def _supervise(tmp_path: Path, outcomes, *, policy=None, **kwargs):
    queue = list(outcomes)
    calls: list[list[str]] = []

    def runner(argv):
        calls.append(list(argv))
        return queue.pop(0)

    slept: list[float] = []
    report = cs.supervise(
        collector_args=["--duration", "60"],
        raw_dir=tmp_path / "raw",
        policy=policy or _policy(),
        ledger_path=tmp_path / "ledger.jsonl",
        alert_dir=tmp_path / "alerts",
        runner=runner,
        clock=FakeClock(),
        sleeper=slept.append,
        disk_free=lambda _path: 100 * 1024**3,
        jitter=lambda: 1.0,
        commit="deadbeef",
        **kwargs,
    )
    return report, calls, slept


def test_clean_exit_stops_without_restart(tmp_path: Path):
    report, calls, slept = _supervise(tmp_path, [cs.ChildOutcome(exit_code=0)])
    assert len(calls) == 1
    assert report.stop_reason == cs.STOP_CLEAN_EXIT
    assert report.fail_closed is False
    assert slept == []
    assert not list((tmp_path / "alerts").glob("*.json"))


def test_operator_signal_stops_without_restart(tmp_path: Path):
    report, calls, _ = _supervise(
        tmp_path, [cs.ChildOutcome(exit_code=None, signal_number=15)]
    )
    assert len(calls) == 1
    assert report.runs[0].termination_kind == cs.TERMINATION_OPERATOR_STOP
    assert report.stop_reason == cs.STOP_OPERATOR
    assert report.fail_closed is False


def test_crash_restarts_with_exponential_backoff_then_fails_closed(tmp_path: Path):
    crash = cs.ChildOutcome(exit_code=1, stderr_tail="Traceback ...")
    report, calls, slept = _supervise(tmp_path, [crash, crash, crash])

    assert len(calls) == 3
    # jitter=1.0 なので full jitter の上限がそのまま出る: 2s, 4s
    assert slept == [2.0, 4.0]
    assert report.stop_reason == cs.STOP_CONSECUTIVE_FAILURES
    assert report.fail_closed is True

    rows = list(read_jsonl(tmp_path / "ledger.jsonl"))
    assert [row["restart_ordinal"] for row in rows] == [0, 1, 2]
    assert {row["termination_kind"] for row in rows} == {cs.TERMINATION_CRASH}
    assert rows[0]["source_commit"] == "deadbeef"
    assert rows[0]["collector_config_sha256"] == rows[-1]["collector_config_sha256"]
    assert rows[-1]["stderr_tail"] == "Traceback ..."

    alerts = list((tmp_path / "alerts").glob("*.json"))
    assert len(alerts) == 1
    payload = json.loads(alerts[0].read_text(encoding="utf-8"))
    assert payload["kind"] == cs.STOP_CONSECUTIVE_FAILURES
    assert payload["detail"]["consecutive_failures"] == 3


def test_restart_budget_stops_before_consecutive_limit(tmp_path: Path):
    crash = cs.ChildOutcome(exit_code=2)
    report, calls, _ = _supervise(
        tmp_path,
        [crash, crash],
        policy=_policy(max_restarts=1, max_consecutive_failures=99),
    )
    assert len(calls) == 2
    assert report.stop_reason == cs.STOP_RESTART_BUDGET
    assert report.fail_closed is True


def test_disk_below_floor_never_starts_the_collector(tmp_path: Path):
    calls: list[list[str]] = []
    report = cs.supervise(
        collector_args=[],
        raw_dir=tmp_path / "raw",
        policy=_policy(min_free_disk_bytes=10 * 1024**3),
        ledger_path=tmp_path / "ledger.jsonl",
        alert_dir=tmp_path / "alerts",
        runner=lambda argv: calls.append(list(argv)),
        clock=FakeClock(),
        sleeper=lambda _seconds: None,
        disk_free=lambda _path: 1024,
        commit=None,
    )
    assert calls == []
    assert report.stop_reason == cs.STOP_DISK_EXHAUSTED
    assert report.fail_closed is True
    payload = json.loads(next((tmp_path / "alerts").glob("*.json")).read_text(encoding="utf-8"))
    assert payload["detail"]["disk_free_bytes"] == 1024
    # 停止しても raw は消さない。台帳に理由だけを残す。
    assert not (tmp_path / "ledger.jsonl").exists()


def test_missing_clock_quality_blocks_restart_but_not_first_start(tmp_path: Path):
    crash = cs.ChildOutcome(exit_code=1)
    calls: list[list[str]] = []

    def runner(argv):
        calls.append(list(argv))
        return crash

    report = cs.supervise(
        collector_args=[],
        raw_dir=tmp_path / "raw",
        policy=_policy(require_clock_quality=True),
        ledger_path=tmp_path / "ledger.jsonl",
        alert_dir=tmp_path / "alerts",
        runner=runner,
        clock=FakeClock(),
        sleeper=lambda _seconds: None,
        disk_free=lambda _path: 100 * 1024**3,
        jitter=lambda: 1.0,
        commit=None,
    )
    # 初回は collector 自身が sample を書く前なので起動を許し、再起動時に止める。
    assert len(calls) == 1
    assert report.stop_reason == cs.STOP_CLOCK_QUALITY


def test_stable_run_resets_the_consecutive_failure_series(tmp_path: Path):
    crash = cs.ChildOutcome(exit_code=1)
    queue = [crash, crash, crash, crash]
    calls: list[list[str]] = []

    def runner(argv):
        calls.append(list(argv))
        return queue.pop(0)

    # 各 run が 600 秒続く clock。stable_run_seconds=300 を越えるので連続失敗が伸びない。
    report = cs.supervise(
        collector_args=[],
        raw_dir=tmp_path / "raw",
        policy=_policy(max_restarts=3, max_consecutive_failures=3),
        ledger_path=tmp_path / "ledger.jsonl",
        alert_dir=tmp_path / "alerts",
        runner=runner,
        clock=FakeClock(step_ns=600 * 10**9),
        sleeper=lambda _seconds: None,
        disk_free=lambda _path: 100 * 1024**3,
        jitter=lambda: 1.0,
        commit=None,
    )
    assert len(calls) == 4
    assert report.stop_reason == cs.STOP_RESTART_BUDGET


def test_start_failure_is_recorded_as_such(tmp_path: Path):
    outcome = cs.ChildOutcome(exit_code=None, error="No such file or directory")
    assert outcome.termination_kind == cs.TERMINATION_START_FAILED
    report, _, _ = _supervise(
        tmp_path, [outcome, outcome, outcome], policy=_policy(max_consecutive_failures=3)
    )
    rows = list(read_jsonl(tmp_path / "ledger.jsonl"))
    assert rows[0]["start_error"] == "No such file or directory"
    assert report.fail_closed is True


def test_backoff_is_capped_and_jittered():
    policy = _policy(backoff_min_seconds=2.0, backoff_max_seconds=10.0)
    assert cs.backoff_seconds(policy, 1, jitter=lambda: 1.0) == 2.0
    assert cs.backoff_seconds(policy, 4, jitter=lambda: 1.0) == 10.0
    assert cs.backoff_seconds(policy, 4, jitter=lambda: 0.5) == 5.0
    assert cs.backoff_seconds(policy, 0, jitter=lambda: 1.0) == 0.0


def test_latest_raw_observation_prefers_the_newest_file(tmp_path: Path):
    raw = tmp_path / "raw" / "okx" / "ws" / "public" / "2026" / "08" / "18"
    raw.mkdir(parents=True)
    older = raw / "a.jsonl.gz"
    newer = raw / "b.jsonl.gz.partial"
    older.write_bytes(b"x")
    newer.write_bytes(b"y")
    import os

    os.utime(older, ns=(1_000 * 10**9, 1_000 * 10**9))
    os.utime(newer, ns=(2_000 * 10**9, 2_000 * 10**9))
    observation = cs.latest_raw_observation(tmp_path / "raw")
    assert observation["path"] == str(newer)
    assert observation["modified_at_ns"] == 2_000 * 10**9


def test_clock_quality_status_uses_sample_age(tmp_path: Path):
    directory = tmp_path / "raw" / "host" / "clock_quality" / "2026" / "08" / "18"
    directory.mkdir(parents=True)
    sample_ns = 1_700_000_000 * 10**9
    (directory / f"20260818T000000Z_{sample_ns}_1_abc.jsonl.gz").write_bytes(b"x")

    assert cs.clock_quality_status(tmp_path / "raw", now_ns=sample_ns, max_age_seconds=60) == "pass"
    assert (
        cs.clock_quality_status(
            tmp_path / "raw", now_ns=sample_ns + 3600 * 10**9, max_age_seconds=60
        )
        == "stale"
    )
    assert cs.clock_quality_status(tmp_path / "empty", now_ns=sample_ns, max_age_seconds=60) == "missing"


def test_policy_rejects_impossible_bounds():
    with pytest.raises(ValueError):
        cs.SupervisorPolicy(backoff_min_seconds=10.0, backoff_max_seconds=1.0)
    with pytest.raises(ValueError):
        cs.SupervisorPolicy(max_consecutive_failures=0)


def test_collector_argv_runs_the_module_not_a_path():
    argv = cs.collector_argv(["--inst-id", "BTC-USDT-SWAP"], executable="/usr/bin/python3")
    assert argv[:3] == ["/usr/bin/python3", "-m", "mce.collect_microstructure"]
    assert argv[3:] == ["--inst-id", "BTC-USDT-SWAP"]
    assert cs.collector_config_sha256(argv) != cs.collector_config_sha256(argv[:3])


def test_subprocess_runner_reports_exit_code_and_stderr_tail(capsys):
    runner = cs.SubprocessRunner(tail_lines=2)
    outcome = runner(
        [
            sys.executable,
            "-c",
            "import sys;print('a',file=sys.stderr);print('b',file=sys.stderr);sys.exit(3)",
        ]
    )
    assert outcome.exit_code == 3
    assert outcome.signal_number is None
    assert outcome.stderr_tail == "a\nb"
    assert outcome.termination_kind == cs.TERMINATION_CRASH
    capsys.readouterr()


def test_subprocess_runner_reports_a_fatal_signal(capsys):
    runner = cs.SubprocessRunner()
    outcome = runner(
        [sys.executable, "-c", "import os, signal;os.kill(os.getpid(), signal.SIGKILL)"]
    )
    assert outcome.exit_code is None
    assert outcome.signal_number == 9
    # SIGKILL は運用者の停止ではないので crash として再起動対象になる。
    assert outcome.termination_kind == cs.TERMINATION_CRASH
    capsys.readouterr()


def test_subprocess_runner_reports_a_missing_executable():
    outcome = cs.SubprocessRunner()([str(Path("/nonexistent/collector"))])
    assert outcome.termination_kind == cs.TERMINATION_START_FAILED
    assert outcome.error

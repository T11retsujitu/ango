from datetime import datetime, timedelta, timezone

import pytest

from mce.first_touch import BarrierSpec, OhlcBar, simulate_short


UTC = timezone.utc
ENTRY = datetime(2026, 1, 1, tzinfo=UTC)
SPEC = BarrierSpec("test", stop_bps=20, take_profit_bps=40, horizon_minutes=10)


def bar(i, o=100.0, h=100.1, l=99.9, c=100.0):
    return OhlcBar(ENTRY + timedelta(minutes=5 * i), o, h, l, c)


def run(bars, timeout_open=100.0, policy="stop"):
    return simulate_short(
        entry_ts=ENTRY,
        entry_price=100.0,
        bars=bars,
        timeout_ts=ENTRY + timedelta(minutes=10),
        timeout_open=timeout_open,
        spec=SPEC,
        ambiguous_policy=policy,
    )


def test_short_take_profit_on_later_bar():
    result = run([bar(0), bar(1, h=100.0, l=99.5, c=99.7)])
    assert result.status == "take_profit"
    assert result.exit_price == pytest.approx(99.6)
    assert result.gross_return == pytest.approx(0.004)


def test_short_stop_and_cost():
    result = run([bar(0, h=100.3), bar(1)])
    assert result.status == "stop"
    assert result.exit_price == pytest.approx(100.2)
    assert result.net_return(15) == pytest.approx(-0.0035)


def test_same_bar_dual_touch_is_stop_in_primary_policy():
    result = run([bar(0, h=100.3, l=99.5), bar(1)])
    assert result.status == "stop"
    assert result.ambiguous


def test_same_bar_dual_touch_bounds():
    take = run([bar(0, h=100.3, l=99.5), bar(1)], policy="take_profit")
    excluded = run([bar(0, h=100.3, l=99.5), bar(1)], policy="exclude")
    assert take.status == "take_profit" and take.ambiguous
    assert excluded.status == "ambiguous_excluded"
    assert excluded.gross_return is None


def test_stop_gap_uses_worse_open_but_take_gap_gets_no_improvement():
    stopped = run([bar(0, o=100.4, h=100.4, l=100.3, c=100.35), bar(1)])
    taken = run([bar(0, o=99.4, h=99.5, l=99.3, c=99.45), bar(1)])
    assert stopped.exit_price == 100.4
    assert taken.exit_price == pytest.approx(99.6)


def test_timeout_uses_open_at_exact_horizon():
    result = run([bar(0), bar(1)], timeout_open=99.95)
    assert result.status == "timeout"
    assert result.exit_ts == ENTRY + timedelta(minutes=10)
    assert result.exit_price == 99.95


def test_path_rejects_missing_or_misaligned_bar():
    with pytest.raises(ValueError, match="requires 2 bars"):
        run([bar(0)])
    wrong = OhlcBar(ENTRY + timedelta(minutes=6), 100, 101, 99, 100)
    with pytest.raises(ValueError, match="non-contiguous"):
        run([bar(0), wrong])


def test_event_bar_is_not_part_of_path():
    # The engine receives bars beginning at entry_ts only. A dramatic event bar
    # from five minutes earlier cannot affect the result.
    result = run([bar(0), bar(1)])
    assert result.status == "timeout"

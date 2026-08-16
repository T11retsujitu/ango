"""執行エンジンのテスト。

- next-bar open fill(close 執行の禁止)
- Execution Delay Test: 執行を1バー遅らせると結果が変わる
- Missing Bar: 欠損バー跨ぎの fill とキャンセル規則
- forced exit / position 遷移 / round trip 集計
"""

import polars as pl
import pytest

from conftest import make_ohlcv_oc
from mce.backtest.execution import ExecutionConfig, bar_returns, execute

MIN_MS = 60_000


def _target(vals):
    return pl.Series(vals, dtype=pl.Int8)


def test_fill_is_next_bar_open_not_close():
    # bar0: open100/close110, bar1: open120/close130, bar2: open140/close150
    bars = make_ohlcv_oc([(0, 100, 110), (5, 120, 130), (10, 140, 150)])
    res = execute(bars, _target([1, 1, 1]))
    fill = res.fills.row(0, named=True)
    assert fill["fill_price"] == 120  # close[0]=110 ではなく open[1]
    assert fill["fill_ts"].timestamp() * 1000 == 5 * MIN_MS
    assert fill["signal_ts"].timestamp() * 1000 == 5 * MIN_MS  # close[0] = ts0 + 5m
    assert res.positions["position"].to_list() == [0, 1, 1]


def test_execution_delay_changes_result():
    bars = make_ohlcv_oc([(0, 100, 110), (5, 120, 130), (10, 140, 150), (15, 160, 170)])
    base = execute(bars, _target([1, 1, 1, 1]))
    delayed = execute(bars, _target([0, 1, 1, 1]))  # signal を意図的に1バー遅延
    assert base.fills.row(0, named=True)["fill_price"] == 120
    assert delayed.fills.row(0, named=True)["fill_price"] == 140
    r_base = bar_returns(bars, base.positions)["gross_return"].sum()
    r_delayed = bar_returns(bars, delayed.positions)["gross_return"].sum()
    assert r_base != r_delayed


def test_signal_on_last_bar_never_fills():
    bars = make_ohlcv_oc([(0, 100, 100), (5, 100, 100)])
    res = execute(bars, _target([0, 1]))
    assert res.fills.height == 0
    assert res.cancelled_count == 0


def test_missing_bar_fill_at_next_existing_open_within_limit():
    # bar 5分の次は 25分(20分ギャップ)。signal at close[5分]=10分, fill=25分 → 遅延15分
    bars = make_ohlcv_oc([(0, 100, 100), (5, 100, 100), (25, 111, 111), (30, 120, 120)])
    res = execute(bars, _target([0, 1, 1, 1]), ExecutionConfig(cancel_after_ms=30 * MIN_MS))
    fill = res.fills.row(0, named=True)
    assert fill["fill_price"] == 111
    assert fill["fill_ts"].timestamp() * 1000 == 25 * MIN_MS
    assert res.cancelled_count == 0


def test_missing_bar_cancels_beyond_limit():
    # 遅延 = 50分 − (5分+5分) = 40分 > 30分 → キャンセル。次の close で再シグナル → fill
    bars = make_ohlcv_oc([(0, 100, 100), (5, 100, 100), (50, 111, 111), (55, 120, 120)])
    res = execute(bars, _target([0, 1, 1, 1]), ExecutionConfig(cancel_after_ms=30 * MIN_MS))
    assert res.cancelled_count == 1
    assert res.fills.height == 1
    fill = res.fills.row(0, named=True)
    assert fill["fill_ts"].timestamp() * 1000 == 55 * MIN_MS  # 50分closeの再シグナルが55分openで約定
    assert res.positions["position"].to_list() == [0, 0, 0, 1]


def test_long_short_flip_creates_two_trades():
    bars = make_ohlcv_oc([(0, 100, 100), (5, 100, 100), (10, 110, 110), (15, 99, 99), (20, 90, 90)])
    res = execute(bars, _target([1, 1, -1, -1, 0]))
    # fills: flat→long @open[1]=100, long→short @open[3]=99, short→flat @open... 20分のcloseはlast bar
    assert res.fills["to_pos"].to_list() == [1, -1]
    closed = res.trades.filter(pl.col("closed"))
    assert closed.height == 1
    t = closed.row(0, named=True)
    assert t["side"] == 1
    assert abs(t["gross_return"] - (99 / 100 - 1)) < 1e-12
    open_trades = res.trades.filter(~pl.col("closed"))
    assert open_trades.height == 1
    assert open_trades.row(0, named=True)["side"] == -1


def test_forced_exit_after_max_holding_bars():
    bars = make_ohlcv_oc([(5 * i, 100, 100) for i in range(6)])
    res = execute(bars, _target([1, 1, 1, 1, 1, 1]), ExecutionConfig(max_holding_bars=2))
    # entry @open[1]。保有2本目(bar2)の close で強制 exit → fill @open[3]。
    # その後 target=1 が続くので bar3 close で再エントリー → fill @open[4]
    assert res.positions["position"].to_list() == [0, 1, 1, 0, 1, 1]
    first_closed = res.trades.filter(pl.col("closed")).row(0, named=True)
    assert first_closed["bars_held"] == 2


def test_invalid_target_rejected():
    bars = make_ohlcv_oc([(0, 100, 100), (5, 100, 100)])
    with pytest.raises(ValueError):
        execute(bars, _target([2, 0]))


def test_bar_returns_and_turnover():
    bars = make_ohlcv_oc([(0, 100, 100), (5, 100, 100), (10, 110, 110), (15, 121, 121)])
    res = execute(bars, _target([1, 1, 0, 0]))
    br = bar_returns(bars, res.positions)
    # pos: [0,1,1,0]。r1 = 1*(110/100−1)=0.10, r2 = 1*(121/110−1)=0.10, r3=最終バー0
    assert br["gross_return"].to_list() == pytest.approx([0.0, 0.10, 0.10, 0.0])
    assert br["turnover"].to_list() == [0.0, 1.0, 0.0, 1.0]

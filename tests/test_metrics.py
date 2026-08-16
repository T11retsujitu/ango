"""metrics の手計算一致テスト。"""

import math

import polars as pl
import pytest

from conftest import make_ohlcv_oc
from mce.backtest.costs import SCENARIOS, apply_costs
from mce.backtest.execution import bar_returns, execute
from mce.backtest.metrics import BARS_PER_YEAR, compute_metrics


def _run(bars_spec, target, scenario="zero"):
    bars = make_ohlcv_oc(bars_spec)
    res = execute(bars, pl.Series(target, dtype=pl.Int8))
    cfg = SCENARIOS[scenario]
    bar_df = apply_costs(bar_returns(bars, res.positions), cfg)
    return compute_metrics(bar_df, res.trades, cfg)


def test_always_flat_metrics():
    m = _run([(5 * i, 100, 100) for i in range(5)], [0] * 5)
    assert m["total_return"] == 0.0
    assert m["max_drawdown"] == 0.0
    assert m["turnover_total"] == 0.0
    assert m["trade_count"] == 0
    assert m["hit_rate"] is None
    assert m["sharpe"] is None  # 分散ゼロ
    assert m["exposure"] == 0.0
    assert m["break_even_cost_bps"] is None


def test_buy_and_hold_total_return_is_open_to_open_sum():
    # entry @open[1]=100。open系列 100→110→121: 算術合算 0.10 + 0.10
    m = _run([(0, 90, 95), (5, 100, 105), (10, 110, 115), (15, 121, 125)], [1, 1, 1, 1])
    assert m["total_return"] == pytest.approx(0.10 + 0.10)
    assert m["exposure"] == pytest.approx(3 / 4)
    assert m["trade_count"] == 0  # 未決済 trade は count しない


def test_sharpe_and_annualization():
    # net returns: [0, +1%, -1%, 0](最終バー0)。mean=0 → sharpe 0
    m = _run([(0, 100, 100), (5, 100, 100), (10, 101, 101), (15, 99.99, 99.99)], [1, 1, 1, 0])
    bar_rets = [0.0, 0.01, 99.99 / 101 - 1, 0.0]
    mean, n = sum(bar_rets) / 4, 4
    std = math.sqrt(sum((r - mean) ** 2 for r in bar_rets) / (n - 1))
    assert m["sharpe"] == pytest.approx(mean / std * math.sqrt(BARS_PER_YEAR))
    assert m["annualized_return"] == pytest.approx(mean * BARS_PER_YEAR)


def test_max_drawdown_non_compounded():
    # pos常時1、open: 100 → 110 → 99 → 99 → returns [+10%, -10%, 0, 0]
    m = _run([(0, 100, 100), (5, 110, 110), (10, 99, 99), (15, 99, 99)], [1, 1, 1, 1])
    assert m["max_drawdown"] == pytest.approx(0.10)


def test_hit_rate_and_profit_factor_net_of_costs():
    # trade1: 100→121 (+21%), trade2: 121→110 long (−9.09%)
    spec = [(0, 100, 100), (5, 100, 100), (10, 121, 121), (15, 121, 121), (20, 121, 121), (25, 110, 110)]
    m = _run(spec, [1, 1, 0, 1, 0, 0], scenario="base_taker")
    assert m["trade_count"] == 2
    assert m["hit_rate"] == pytest.approx(0.5)
    win = 0.21 - 10e-4  # 往復10bps控除
    loss = abs(110 / 121 - 1 - 10e-4)  # コスト控除で損失は拡大する
    assert m["profit_factor"] == pytest.approx(win / loss)

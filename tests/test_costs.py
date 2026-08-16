"""cost model のテスト(Cost Monotonicity Test 含む)。"""

import polars as pl
import pytest

from conftest import make_ohlcv_oc
from mce.backtest.costs import SCENARIOS, CostConfig, apply_costs, break_even_cost_bps
from mce.backtest.execution import bar_returns, execute


def _bar_df(target):
    bars = make_ohlcv_oc([(0, 100, 100), (5, 100, 100), (10, 110, 110), (15, 121, 121)])
    res = execute(bars, pl.Series(target, dtype=pl.Int8))
    return bar_returns(bars, res.positions)


def test_zero_cost_net_equals_gross():
    df = apply_costs(_bar_df([1, 1, 0, 0]), SCENARIOS["zero"])
    assert df["net_return"].to_list() == df["gross_return"].to_list()


def test_cost_reduces_net_by_turnover_times_rate():
    df = apply_costs(_bar_df([1, 1, 0, 0]), SCENARIOS["base_taker"])
    # turnover 合計 2(entry+exit)、片道 5bps
    assert df["cost"].sum() == pytest.approx(2 * 5e-4)
    assert df["net_return"].sum() == pytest.approx(df["gross_return"].sum() - 2 * 5e-4)


def test_cost_monotonicity():
    """コストを増やしても net が改善しない(異常が起きない)。"""
    base = _bar_df([1, -1, 1, 0])  # turnover の多い系列
    totals = []
    for bps in [0, 1, 5, 10, 20]:
        cfg = CostConfig(f"c{bps}", fee_bps=bps)
        totals.append(apply_costs(base, cfg)["net_return"].sum())
    assert all(a >= b for a, b in zip(totals, totals[1:]))
    assert totals[0] > totals[-1]  # turnover > 0 なら厳密に悪化する


def test_component_sum():
    cfg = SCENARIOS["stress"]
    assert cfg.per_side_bps == 10.0
    assert cfg.roundtrip_bps == 20.0


def test_break_even_cost():
    df = _bar_df([1, 1, 0, 0])  # gross 合計 0.20(0.10+0.10)、turnover 2
    be = break_even_cost_bps(df)
    assert be == pytest.approx(0.20 / 2 * 1e4)
    # break-even ちょうどのコストで net PnL ≈ 0
    net = apply_costs(df, CostConfig("be", fee_bps=be))["net_return"].sum()
    assert net == pytest.approx(0.0, abs=1e-12)


def test_break_even_none_when_no_trading():
    assert break_even_cost_bps(_bar_df([0, 0, 0, 0])) is None


def test_funding_not_implemented_in_phase0():
    with pytest.raises(NotImplementedError):
        apply_costs(_bar_df([1, 1, 0, 0]), CostConfig("f", funding_bps_per_bar=1.0))

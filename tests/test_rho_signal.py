"""ρ / Arm R シグナル層の適合テスト(凍結プロトコル v1.8.2 §4.2 / §6.3)。

**すべて合成データ。** Layer 1/2/3 の結果データも封印済み事前予想レジスタも読まない。
バックテストも実行しない。
"""

import math
from datetime import datetime, timedelta, timezone

import pytest

from mce import phase8_prereg as P
from mce.backtest.rho import (
    RateObservation,
    RhoInputs,
    Signal,
    arb_bound_lower,
    arb_bound_upper,
    arm_r_signal,
    generate_arm_r_signals,
    point_in_time_rate,
    require_resolved_cost,
    rho_exact,
)

UTC = timezone.utc
T0 = datetime(2025, 6, 2, tzinfo=UTC)
HOUR = timedelta(hours=1)


# --------------------------------------------------------------------------
# ρ の式
# --------------------------------------------------------------------------


def test_rho_matches_the_frozen_formula():
    perp, spot, r = 100_500.0, 100_000.0, 0.05
    expected = P.KAPPA * (1 - math.exp(-(math.log(perp) - math.log(spot)))) - (r - P.R_PRIME)
    assert rho_exact(perp, spot, r) == pytest.approx(expected)


def test_rho_is_zero_when_no_gap_and_no_rate():
    assert rho_exact(100.0, 100.0, 0.0) == pytest.approx(0.0)


def test_rho_rate_term_is_subtracted_one_for_one():
    base = rho_exact(100_500.0, 100_000.0, 0.0)
    assert rho_exact(100_500.0, 100_000.0, 0.07) == pytest.approx(base - 0.07)


def test_rho_uses_r_prime_from_the_frozen_spec():
    """Arm R は spot をショートしないので r' = 0(§4.2 / H12)。"""
    assert P.R_PRIME == 0.0
    assert rho_exact(100_500.0, 100_000.0, 0.05) == pytest.approx(
        rho_exact(100_500.0, 100_000.0, 0.05, r_prime=0.0)
    )


def test_rho_sign_follows_the_perp_spot_gap():
    assert rho_exact(100_500.0, 100_000.0, 0.0) > 0  # perp が高い
    assert rho_exact(99_500.0, 100_000.0, 0.0) < 0  # perp が安い


def test_rho_rejects_non_positive_prices():
    for bad in ((0.0, 100.0), (100.0, 0.0), (-1.0, 100.0)):
        with pytest.raises(ValueError):
            rho_exact(bad[0], bad[1], 0.0)


# --------------------------------------------------------------------------
# no-arbitrage 帯
# --------------------------------------------------------------------------


def test_bounds_match_a2_table3_caption():
    c = 0.0030
    assert arb_bound_upper(c) == pytest.approx(P.KAPPA * math.log(1 + c))
    assert arb_bound_lower(c) == pytest.approx(P.KAPPA * math.log(1 - c))


def test_bounds_straddle_zero_and_are_asymmetric():
    c = 0.0030
    up, lo = arb_bound_upper(c), arb_bound_lower(c)
    assert up > 0 > lo
    # log(1+C) < |log(1-C)| なので下側の方が絶対値が大きい
    assert abs(lo) > abs(up)


def test_zero_cost_collapses_the_band_to_zero():
    assert arb_bound_upper(0.0) == pytest.approx(0.0)
    assert arb_bound_lower(0.0) == pytest.approx(0.0)


def test_bounds_widen_monotonically_with_cost():
    ups = [arb_bound_upper(c) for c in (0.0005, 0.0030, 0.0050)]
    assert ups == sorted(ups)


def test_bounds_reject_out_of_range_cost():
    for bad in (-0.001, 1.0, 1.5):
        with pytest.raises(ValueError):
            arb_bound_upper(bad)


# --------------------------------------------------------------------------
# Arm R の判定と**厳密な境界挙動**
# --------------------------------------------------------------------------


def test_entry_is_strictly_above_the_upper_bound():
    c = 0.0030
    up = arb_bound_upper(c)
    assert arm_r_signal(up, c, in_position=False) is Signal.HOLD, "境界ちょうどは建てない"
    assert arm_r_signal(math.nextafter(up, math.inf), c, False) is Signal.ENTER
    assert arm_r_signal(math.nextafter(up, -math.inf), c, False) is Signal.HOLD


def test_exit_is_at_or_below_zero_not_at_the_band():
    c = 0.0030
    assert arm_r_signal(0.0, c, in_position=True) is Signal.EXIT, "0 ちょうどで解消する"
    assert arm_r_signal(math.nextafter(0.0, -math.inf), c, True) is Signal.EXIT
    assert arm_r_signal(math.nextafter(0.0, math.inf), c, True) is Signal.HOLD
    # 帯の下限では解消しない(exit は境界ではなく 0)
    assert arm_r_signal(arb_bound_lower(c), c, True) is Signal.EXIT
    assert arm_r_signal(arb_bound_upper(c) / 2, c, True) is Signal.HOLD


def test_entry_and_exit_are_asymmetric_by_design():
    """建てる閾値は rho_u、解消する閾値は 0。同じ値ではない。"""
    c = 0.0030
    mid = arb_bound_upper(c) / 2
    assert arm_r_signal(mid, c, in_position=False) is Signal.HOLD
    assert arm_r_signal(mid, c, in_position=True) is Signal.HOLD


def test_arm_r_is_long_spot_only_never_uses_the_lower_bound_for_entry():
    """§6.3 / Y54: Arm R は long-spot-only 変種。rho_l 側では建てない。"""
    c = 0.0030
    deep = arb_bound_lower(c) * 2
    assert arm_r_signal(deep, c, in_position=False) is Signal.HOLD
    assert P.A2_VARIANT == "long_spot_only"


# --------------------------------------------------------------------------
# 欠測・陳腐化した r(補完しない)
# --------------------------------------------------------------------------


def test_missing_rate_yields_no_signal_not_an_imputed_value():
    assert arm_r_signal(None, 0.0030, in_position=False) is Signal.NO_RATE
    assert arm_r_signal(None, 0.0030, in_position=True) is Signal.NO_RATE


def test_point_in_time_rate_never_looks_ahead():
    obs = [
        RateObservation(T0 - HOUR, 0.05),
        RateObservation(T0 + HOUR, 0.99),  # 未来。使ってはならない
    ]
    assert point_in_time_rate(obs, T0) == pytest.approx(0.05)


def test_point_in_time_rate_returns_none_when_stale():
    obs = [RateObservation(T0 - timedelta(seconds=P.MAX_STALE_SECONDS + 1), 0.05)]
    assert point_in_time_rate(obs, T0) is None, "陳腐化した値を前方補完しない"


def test_point_in_time_rate_accepts_value_exactly_at_the_stale_boundary():
    obs = [RateObservation(T0 - timedelta(seconds=P.MAX_STALE_SECONDS), 0.05)]
    assert point_in_time_rate(obs, T0) == pytest.approx(0.05)


def test_point_in_time_rate_returns_none_when_nothing_is_available():
    assert point_in_time_rate([], T0) is None
    assert point_in_time_rate([RateObservation(T0 + HOUR, 0.05)], T0) is None


def test_no_forward_fill_across_a_gap_in_the_signal_stream():
    """欠測バーで補完値を作らず、建玉状態も変えないこと。"""
    c = 0.0030
    hi = arb_bound_upper(c) + 1.0
    perp, spot = 100_500.0, 100_000.0
    # r を選んで rho を狙った値にする: rho = base - r
    base = rho_exact(perp, spot, 0.0)
    rows = [
        RhoInputs(T0, perp, spot, base - hi),  # ENTER
        RhoInputs(T0 + HOUR, perp, spot, None),  # 欠測
        RhoInputs(T0 + 2 * HOUR, perp, spot, base - hi),  # まだ帯の外
    ]
    pts = generate_arm_r_signals(rows, c)
    assert pts[0].signal is Signal.ENTER and pts[0].in_position
    assert pts[1].signal is Signal.NO_RATE
    assert pts[1].rho is None, "補完値を作っていない"
    assert pts[1].in_position, "欠測で建玉状態を変えない"
    assert pts[2].signal is Signal.HOLD


def test_signal_stream_enters_once_and_exits_at_zero():
    c = 0.0030
    perp, spot = 100_500.0, 100_000.0
    base = rho_exact(perp, spot, 0.0)
    rows = [
        RhoInputs(T0, perp, spot, base - (arb_bound_upper(c) + 1.0)),  # ENTER
        RhoInputs(T0 + HOUR, perp, spot, base - (arb_bound_upper(c) + 0.5)),  # HOLD
        RhoInputs(T0 + 2 * HOUR, perp, spot, base - 0.0),  # rho = 0 -> EXIT
        RhoInputs(T0 + 3 * HOUR, perp, spot, base - 0.0),  # 建てない
    ]
    sigs = [p.signal for p in generate_arm_r_signals(rows, c)]
    assert sigs == [Signal.ENTER, Signal.HOLD, Signal.EXIT, Signal.HOLD]


# --------------------------------------------------------------------------
# H13 ブロック(placeholder を実体化しない)
# --------------------------------------------------------------------------


def test_h13_blocks_instantiating_base_taker():
    """`base_taker` の perp taker は実測値ではない(§22.1)。"""
    assert P.COMMISSION_RATE_STATUS != "resolved"
    with pytest.raises(ValueError, match="H13 未解決"):
        require_resolved_cost("base_taker")


def test_h13_does_not_block_a_tier_without_the_placeholder():
    spot_bps, perp_bps = require_resolved_cost("maker_low")
    assert (spot_bps, perp_bps) == (1.0, 1.0)


def test_cost_must_be_supplied_explicitly_to_signal_generation():
    """シグナル生成は凍結階層を暗黙に読まない(明示引数のみ)。"""
    import inspect

    sig = inspect.signature(generate_arm_r_signals)
    assert "round_trip_cost" in sig.parameters
    assert sig.parameters["round_trip_cost"].default is inspect.Parameter.empty


def test_rate_must_be_supplied_explicitly_to_rho():
    import inspect

    sig = inspect.signature(rho_exact)
    assert sig.parameters["r"].default is inspect.Parameter.empty


# --------------------------------------------------------------------------
# H15(Aave 市場の同定)は未解決
# --------------------------------------------------------------------------


def test_h15_is_registered_as_unresolved():
    assert P.RATE_MARKET_IDENTITY_STATUS == "unresolved_source_fidelity_limitation"
    assert P.RATE_MARKET_VERSION is None
    assert P.RATE_MARKET_NETWORK is None
    assert P.RATE_MARKET_INSTANCE is None


def test_rho_layer_works_without_h15_being_resolved():
    """r を明示入力にしたので、ソース同定と独立に検証できる。"""
    assert P.RATE_MARKET_IDENTITY_STATUS.startswith("unresolved")
    assert rho_exact(100_500.0, 100_000.0, 0.05) == pytest.approx(
        P.KAPPA * (1 - math.exp(-math.log(100_500.0 / 100_000.0))) - 0.05
    )

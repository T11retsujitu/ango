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
    obs = [RateObservation(T0 - timedelta(seconds=P.RATE_MAX_STALE_SECONDS + 1), 0.05)]
    assert point_in_time_rate(obs, T0) is None, "陳腐化した値を前方補完しない"


def test_point_in_time_rate_accepts_value_exactly_at_the_stale_boundary():
    obs = [RateObservation(T0 - timedelta(seconds=P.RATE_MAX_STALE_SECONDS), 0.05)]
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


def test_h15_is_adopted_as_a_partial_proxy_not_an_exact_reconstruction():
    """§27.1: 厳密再現だと主張しない。"""
    assert P.RATE_SOURCE_FIDELITY == "partial_proxy_not_exact_A2"
    assert P.RATE_MARKET_IDENTITY_STATUS == "adopted_partial_proxy"
    assert P.RATE_MARKET_NETWORK == "ethereum_mainnet"


def test_rho_layer_is_independent_of_the_rate_source():
    """r を明示入力にしたので、ソース選択と独立に検証できる。"""
    assert rho_exact(100_500.0, 100_000.0, 0.05) == pytest.approx(
        P.KAPPA * (1 - math.exp(-math.log(100_500.0 / 100_000.0))) - 0.05
    )


# ==========================================================================
# v1.8.3 §29.1 — H15 proxy と H16 の陳腐化分離(T42-T48)
# ==========================================================================

from mce.backtest.rho import aave_basket_mean, aave_market_for  # noqa: E402

DAY = timedelta(days=1)


def _snapshot(day: datetime, rate: float) -> RateObservation:
    """その日 00:00 UTC のスナップショット(§27.4)。"""
    return RateObservation(day.replace(hour=0, minute=0, second=0, microsecond=0), rate)


def test_t42_daily_rate_stays_valid_through_the_same_utc_day():
    """§27.4 / §28: 00:00 UTC のスナップショットは同じ暦日を通じて有効。"""
    snap = _snapshot(T0, 0.05)
    for hh in (0, 1, 9, 12, 23):
        ts = T0.replace(hour=hh, minute=59 if hh else 0)
        assert point_in_time_rate([snap], ts) == pytest.approx(0.05), hh
    # 日の終わりでもまだ有効
    assert point_in_time_rate([snap], T0 + DAY - timedelta(seconds=1)) == pytest.approx(0.05)


def test_t43_rate_goes_stale_after_the_frozen_horizon_when_the_next_snapshot_is_missing():
    """翌日のスナップショットが欠けたら、24h を過ぎて陳腐化する。"""
    snap = _snapshot(T0, 0.05)
    assert P.RATE_MAX_STALE_SECONDS == 24 * 3600
    # ちょうど 24h は有効(境界は包含)
    assert point_in_time_rate([snap], T0 + DAY) == pytest.approx(0.05)
    # 24h を1秒でも過ぎたら None
    assert point_in_time_rate([snap], T0 + DAY + timedelta(seconds=1)) is None
    assert point_in_time_rate([snap], T0 + 2 * DAY) is None


def test_t44_funding_staleness_constant_cannot_affect_rho():
    """§28: funding の 9h が金利入力に効いてはならない。"""
    assert P.FUNDING_MAX_STALE_SECONDS == 9 * 3600
    assert P.RATE_MAX_STALE_SECONDS == 24 * 3600
    assert P.RATE_MAX_STALE_SECONDS != P.FUNDING_MAX_STALE_SECONDS
    # 廃止された単一定数が復活していないこと(§25 規則4)
    assert not hasattr(P, "MAX_STALE_SECONDS"), "MAX_STALE_SECONDS は廃止された"
    # 9h を超え 24h 未満の時点で、9h 基準なら None、正しくは有効
    snap = _snapshot(T0, 0.05)
    ts = T0 + timedelta(hours=12)
    assert point_in_time_rate([snap], ts) == pytest.approx(0.05)
    assert point_in_time_rate([snap], ts, max_stale_seconds=P.FUNDING_MAX_STALE_SECONDS) is None
    # 既定が rate 側であること
    import inspect

    default = inspect.signature(point_in_time_rate).parameters["max_stale_seconds"].default
    assert default == P.RATE_MAX_STALE_SECONDS


def test_t45_all_three_stablecoins_are_required():
    """§27.3: 3成分すべて必要。黙って構成を変えない。"""
    assert P.RATE_BASKET_REQUIRE_ALL is True
    assert aave_basket_mean({"USDT": 0.04, "USDC": 0.06, "DAI": 0.08}) == pytest.approx(0.06)
    for missing in ("USDT", "USDC", "DAI"):
        rates = {"USDT": 0.04, "USDC": 0.06, "DAI": 0.08}
        rates[missing] = None
        assert aave_basket_mean(rates) is None, f"{missing} 欠測で r なしになること"


def test_t45_basket_composition_cannot_be_silently_changed():
    with pytest.raises(ValueError, match="basket 構成"):
        aave_basket_mean({"USDT": 0.04, "USDC": 0.06})  # DAI を落とす
    with pytest.raises(ValueError, match="basket 構成"):
        aave_basket_mean({"USDT": 0.04, "USDC": 0.06, "DAI": 0.08, "FRAX": 0.05})


def test_t46_no_interpolation_across_missing_days():
    """§27.4: 欠測日をまたいで補間しない。"""
    assert P.RATE_INTERPOLATION == "none"
    d0, d3 = T0, T0 + 3 * DAY
    obs = [_snapshot(d0, 0.04), _snapshot(d3, 0.10)]
    # 欠測日 (d0+1, d0+2) では補間値(0.06/0.08)を作らず None を返す
    assert point_in_time_rate(obs, d0 + DAY) == pytest.approx(0.04), "24h ちょうどは有効"
    assert point_in_time_rate(obs, d0 + DAY + timedelta(seconds=1)) is None
    assert point_in_time_rate(obs, d0 + 2 * DAY) is None
    # d3 のスナップショットが来たら、その値がそのまま使われる(平滑化しない)
    assert point_in_time_rate(obs, d3) == pytest.approx(0.10)


def test_t46_gap_produces_no_signal_rather_than_an_imputed_one():
    c = 0.0030
    perp, spot = 100_500.0, 100_000.0
    base = rho_exact(perp, spot, 0.0)
    obs = [_snapshot(T0, base - (arb_bound_upper(c) + 1.0))]
    rows = [
        RhoInputs(T0, perp, spot, point_in_time_rate(obs, T0)),
        RhoInputs(T0 + 2 * DAY, perp, spot, point_in_time_rate(obs, T0 + 2 * DAY)),
    ]
    pts = generate_arm_r_signals(rows, c)
    assert pts[0].signal is Signal.ENTER
    assert pts[1].signal is Signal.NO_RATE and pts[1].rho is None


def test_t47_market_splices_stop_at_v3_core_and_never_reach_v4():
    """§27.2: V4 へ移行しない。V3 Core が終端。"""
    assert "aave_v4" in P.RATE_MARKET_EXCLUDED_VERSIONS
    cases = {
        datetime(2019, 6, 1, tzinfo=UTC): None,  # V1 稼働前
        datetime(2020, 1, 8, tzinfo=UTC): "aave_v1",
        datetime(2020, 12, 2, tzinfo=UTC): "aave_v1",
        datetime(2020, 12, 3, tzinfo=UTC): "aave_v2",
        datetime(2023, 1, 26, tzinfo=UTC): "aave_v2",
        datetime(2023, 1, 27, tzinfo=UTC): "aave_v3_core",
        datetime(2026, 3, 30, tzinfo=UTC): "aave_v3_core",  # V4 ローンチ日でも V3
        datetime(2027, 1, 1, tzinfo=UTC): "aave_v3_core",
    }
    for ts, expected in cases.items():
        assert aave_market_for(ts) == expected, ts
    # layer 2 と layer 3 は同じ世代であること(§27.2 の帰結)
    assert aave_market_for(P.LAYER1_END) == aave_market_for(P.LAYER3_START) == "aave_v3_core"


def test_t47_splices_are_contiguous_and_recorded():
    """接合日が provenance に残せる形で凍結されていること。"""
    spl = P.RATE_MARKET_SPLICES
    assert spl[0][1] == datetime(2020, 1, 8, tzinfo=UTC), "V1 genesis"
    for (_, _, end), (_, nxt, _) in zip(spl, spl[1:]):
        assert end == nxt, "接合に隙間や重なりがない"
    assert spl[-1][2] is None, "最終区間は開いている(V4 へ移らない)"


def test_t48_source_sensitivity_disposition_is_frozen():
    """§29: 符号が逆なら GO ではなく source_sensitive。"""
    assert P.SOURCE_SENSITIVE_DISPOSITION == "source_sensitive"
    rule = P.SOURCE_SENSITIVITY_RULE
    assert "kenneth_french" in rule and "source_sensitive" in rule
    assert "not GO" in rule
    # 感応度は維持されている(primary の置換ではない)
    assert P.RATE_SENSITIVITY_SOURCE == "kenneth_french_daily_rf"
    assert P.RATE_SOURCE != P.RATE_SENSITIVITY_SOURCE

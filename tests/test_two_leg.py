"""two_leg 執行器の適合テスト(凍結プロトコル §21.2 のうち engine 層)。

対象: T3 / T4 / T5 / T6 / T13 / T16 / T18(代理規則) / T19 / T30 / T31 / T32 / T34。
data 層・runner 層のテスト(T1/T2/T7–T12/T14/T15/T17/T20–T29/T33)は本ファイルの
対象外である。

**実験は実行しない。** すべて合成データによる会計の検算である。
"""

from datetime import datetime, timedelta, timezone

import pytest

from mce import phase8_prereg as P
from mce.backtest.costs import TwoLegCostConfig
from mce.backtest.two_leg import (
    Bar,
    FundingEvent,
    TwoLegConfig,
    UNFROZEN_PARAMETERS,
    _funding_in_window,
    _liquidation_price,
    round_to_lot,
    simulate_trade,
    size_position,
)

UTC = timezone.utc
T0 = datetime(2025, 6, 2, 0, 0, tzinfo=UTC)
BAR = timedelta(minutes=5)


def _cfg(**kw) -> TwoLegConfig:
    """H14a は未凍結なので**テストが明示的に値を与える**(既定に頼らない)。

    ここで渡す 0.0 は「凍結値」ではなくテスト用の既知入力である。
    実験実行は `liquidation_cost_is_resolved` で別途ブロックされる。
    v1.8.5 §31 で滑りは設定項目ではなくなった(観測価格から決まる)。
    """
    base = dict(
        cost=TwoLegCostConfig("base_taker", P.SPOT_TAKER_BPS, P.PERP_TAKER_BPS),
        liquidation_clearance_fee_rate=0.0,
    )
    base.update(kw)
    return TwoLegConfig(**base)  # type: ignore[arg-type]


def _bars(n: int, spot: float, perp: float, *, mark_high=None, perp_high=None,
          status: str = "observed", step=0.0) -> list[Bar]:
    out = []
    for i in range(n):
        s = spot + step * i
        p = perp + step * i
        out.append(
            Bar(
                ts=T0 + BAR * i,
                spot_open=s,
                spot_close=s,
                perp_open=p,
                perp_close=p,
                perp_high=(
                    perp_high[i] if perp_high else (mark_high[i] if mark_high else p)
                ),
                mark_high=(mark_high[i] if mark_high else p),
                mark_close=p,
                mark_path_status=status,
            )
        )
    return out


# --------------------------------------------------------------------------
# T3 — 損益恒等式
# --------------------------------------------------------------------------


def test_t3_pnl_identity_holds_between_basis_form_and_leg_form():
    """PnL = q(D_in − D_out) + Funding が脚ごとの積み上げと一致すること(§6.2)。"""
    bars = _bars(12, spot=100_000.0, perp=100_050.0)
    # exit バーで basis を縮める
    bars[10] = Bar(bars[10].ts, 100_200.0, 100_200.0, 100_210.0, 100_210.0, perp_high=100_210.0, mark_high=100_210.0, mark_close=100_210.0)
    r = simulate_trade(bars, [], T0, T0 + BAR * 9, _cfg())
    assert r.opened
    assert r.pnl_gross == pytest.approx(r.pnl_gross_by_legs, rel=1e-12, abs=1e-9)


def test_t3_identity_holds_with_funding():
    bars = _bars(12, spot=100_000.0, perp=100_050.0)
    ev = FundingEvent(T0 + BAR * 5, 0.0001, 100_040.0, 8.0)
    r = simulate_trade(bars, [ev], T0, T0 + BAR * 9, _cfg())
    assert r.funding_events_applied == 1
    assert r.funding_total == pytest.approx(r.q_btc * 100_040.0 * 0.0001)
    assert r.pnl_gross == pytest.approx(r.pnl_gross_by_legs, rel=1e-12, abs=1e-9)


def test_t3_delta_neutrality_price_move_alone_does_not_move_pnl():
    """basis 一定で価格だけ動いても gross PnL は 0(方向を予測しない設計の検算)。"""
    bars = _bars(12, spot=100_000.0, perp=100_050.0, step=25.0)
    r = simulate_trade(bars, [], T0, T0 + BAR * 9, _cfg())
    assert r.basis_in == pytest.approx(r.basis_out)
    assert r.pnl_gross == pytest.approx(0.0, abs=1e-6)


# --------------------------------------------------------------------------
# T4 — 両脚が open[t+1] で約定
# --------------------------------------------------------------------------


def test_t4_both_legs_fill_at_the_next_bar_open():
    bars = _bars(12, spot=100_000.0, perp=100_050.0, step=10.0)
    r = simulate_trade(bars, [], T0, T0 + BAR * 8, _cfg())
    assert r.entry_fill_ts == T0 + BAR  # signal バー自身では約定しない
    assert r.spot_in == bars[1].spot_open
    assert r.perp_in == bars[1].perp_open  # 同じバーの open。合成の1約定に畳まない
    assert r.exit_fill_ts == T0 + BAR * 9
    assert r.spot_out == bars[9].spot_open
    assert r.perp_out == bars[9].perp_open


def test_t4_legs_are_modelled_separately_not_as_one_synthetic_fill():
    """spot と perp の約定価格が別物として保持されていること。"""
    bars = _bars(6, spot=100_000.0, perp=100_500.0)
    r = simulate_trade(bars, [], T0, T0 + BAR * 3, _cfg())
    assert r.spot_in != r.perp_in
    assert r.basis_in == pytest.approx(500.0)


# --------------------------------------------------------------------------
# T5 — M6a は skip、M6b は roll-forward
# --------------------------------------------------------------------------


def test_t5_m6a_missing_leg_at_entry_skips_the_trade():
    bars = _bars(6, spot=100_000.0, perp=100_050.0)
    bars[1] = Bar(bars[1].ts, None, None, 100_050.0, 100_050.0, perp_high=100_050.0, mark_high=100_050.0, mark_close=100_050.0)
    cfg = _cfg()
    object.__setattr__(cfg.execution, "cancel_after_ms", 5 * 60_000)  # 1 バーで打ち切り
    r = simulate_trade(bars, [], T0, T0 + BAR * 3, cfg)
    assert not r.opened
    assert r.reject_reason == "no_entry_fill_bar_with_both_legs"


def test_t5_m6b_missing_leg_at_exit_rolls_forward_without_voiding_entry():
    bars = _bars(10, spot=100_000.0, perp=100_050.0)
    # exit 予定バー(index 5)の spot が欠損 → 次に両脚が揃うバーへ roll-forward
    bars[5] = Bar(bars[5].ts, None, None, 100_050.0, 100_050.0, perp_high=100_050.0, mark_high=100_050.0, mark_close=100_050.0)
    r = simulate_trade(bars, [], T0, T0 + BAR * 4, _cfg())
    assert r.opened, "exit 側の欠損で entry を遡って無効化してはならない"
    assert r.exit_fill_ts == bars[6].ts


# --------------------------------------------------------------------------
# T6 — funding 境界 entry < s <= exit
# --------------------------------------------------------------------------


def test_t6_funding_boundary_is_entry_exclusive_exit_inclusive():
    entry, exit_ = T0 + BAR, T0 + BAR * 5
    events = [
        FundingEvent(entry, 0.01, 100.0, 8.0),  # 建てた瞬間 → 受け取らない
        FundingEvent(entry + BAR, 0.01, 100.0, 8.0),  # 保有中 → 受け取る
        FundingEvent(exit_, 0.01, 100.0, 8.0),  # 決済の瞬間 → 受け取る
        FundingEvent(exit_ + BAR, 0.01, 100.0, 8.0),  # 決済後 → 受け取らない
    ]
    got = _funding_in_window(events, entry, exit_)
    assert [e.settlement_ts for e in got] == [entry + BAR, exit_]
    assert P.FUNDING_BOUNDARY == "entry_exclusive_exit_inclusive"


def test_t6_settlement_at_entry_instant_is_not_credited_end_to_end():
    bars = _bars(10, spot=100_000.0, perp=100_050.0)
    at_entry = FundingEvent(T0 + BAR, 0.001, 100_050.0, 8.0)
    r = simulate_trade(bars, [at_entry], T0, T0 + BAR * 5, _cfg())
    assert r.funding_events_applied == 0
    assert r.funding_total == 0.0


# --------------------------------------------------------------------------
# T19 — funding_interval_hours を行ごとに読む
# --------------------------------------------------------------------------


def test_t19_funding_interval_is_carried_per_row_not_hardcoded():
    assert P.READ_FUNDING_INTERVAL_PER_ROW is True
    ev8 = FundingEvent(T0 + BAR * 2, 0.0001, 100.0, 8.0)
    ev1 = FundingEvent(T0 + BAR * 3, 0.0001, 100.0, 1.0)  # cap 到達後の 1h 切替
    assert ev8.interval_hours == 8.0 and ev1.interval_hours == 1.0
    bars = _bars(10, spot=100_000.0, perp=100_050.0)
    r = simulate_trade(bars, [ev8, ev1], T0, T0 + BAR * 5, _cfg())
    # 間隔が違っても cashflow は q·mark·f で、間隔で割ったり掛けたりしない
    assert r.funding_total == pytest.approx(2 * r.q_btc * 100.0 * 0.0001)


# --------------------------------------------------------------------------
# T13 / T30 — 清算は mark の不利側 intrabar 極値で判定される
# --------------------------------------------------------------------------


def test_t30_liquidation_uses_adverse_mark_extreme_not_perp_close():
    """close は無害でも mark high が清算水準を超えたら清算されること。"""
    n = 8
    highs = [100_050.0] * n
    highs[4] = 200_000.0  # 一瞬だけ跳ねる。close は据え置き
    bars = _bars(n, spot=100_000.0, perp=100_050.0, mark_high=highs)
    r = simulate_trade(bars, [], T0, T0 + BAR * 6, _cfg())
    assert r.liquidated, "mark の intrabar 極値で清算されるべき"
    assert any(b.liquidated for b in r.bar_states)


def test_t13_no_liquidation_when_adverse_extreme_stays_below_trigger():
    n = 8
    highs = [100_050.0] * n
    highs[4] = 120_000.0  # +20% は L=3 の +32.8% 未満
    bars = _bars(n, spot=100_000.0, perp=100_050.0, mark_high=highs)
    r = simulate_trade(bars, [], T0, T0 + BAR * 6, _cfg())
    assert not r.liquidated


def test_t13_liquidation_threshold_matches_the_protocol_formula():
    """(1 + 1/L)/(1 + m) − 1 ≈ +32.8%(§11.4)。"""
    q, entry = 0.075, 100_000.0
    margin = q * entry / P.LEVERAGE
    px = _liquidation_price(entry, margin, q, P.MAINT_MARGIN_RATE_TIER1)
    expected = (1 + 1 / P.LEVERAGE) / (1 + P.MAINT_MARGIN_RATE_TIER1) - 1
    assert px / entry - 1 == pytest.approx(expected, rel=1e-9)


# --------------------------------------------------------------------------
# T31 — lot step 丸めと MIN_NOTIONAL 棄却
# --------------------------------------------------------------------------


def test_t31_lot_step_rounds_down_and_records_residual():
    assert round_to_lot(0.0759, 0.001) == pytest.approx(0.075)
    assert round_to_lot(0.0999, 0.001) == pytest.approx(0.099)
    cfg = _cfg()
    q, residual = size_position(99_999.0, cfg)
    assert q == pytest.approx(round_to_lot(q, cfg.lot_step))
    assert 0 <= residual < cfg.lot_step


def test_t31_below_min_notional_is_rejected_with_a_reason():
    # C を小さくする。R も同じ導出 C/(L+2) でスケールさせる(20)
    cfg = _cfg(capital_base_usdt=100.0, reserve_usdt=20.0)
    bars = _bars(6, spot=100_000.0, perp=100_050.0)
    r = simulate_trade(bars, [], T0, T0 + BAR * 3, cfg)
    assert not r.opened
    assert r.reject_reason in {"below_min_notional", "quantity_rounds_to_zero"}


def test_t31_min_notional_uses_the_frozen_exchange_value():
    assert P.PERP_MIN_NOTIONAL_USDT == 50.0
    assert P.PERP_LOT_STEP == 0.001


# --------------------------------------------------------------------------
# T32 — 追証が発火し、予備資金枯渇で清算する
# --------------------------------------------------------------------------


def test_t32_margin_topup_fires_and_consumes_reserve():
    n = 8
    highs = [100_050.0] * n
    highs[3] = 132_500.0  # ratio 0.0068: 追証トリガ 0.01 を割るが mmr 0.004 の手前
    bars = _bars(n, spot=100_000.0, perp=100_050.0, mark_high=highs)
    r = simulate_trade(bars, [], T0, T0 + BAR * 6, _cfg())
    assert r.topup_count >= 1, "維持証拠金率が閾値を割ったら追証が発火すること"
    assert r.topup_total_usdt > 0
    assert not r.liquidated
    assert r.bar_states[-1].reserve_usdt < P.MARGIN_RESERVE_USDT


def test_t32_liquidation_when_reserve_is_exhausted():
    n = 8
    highs = [100_050.0] * n
    highs[3] = 133_000.0  # 清算水準超え。予備資金ゼロでは支えられない
    bars = _bars(n, spot=100_000.0, perp=100_050.0, mark_high=highs)
    # 予備資金を明示的に空にした対照(凍結値 2000 ではなくテスト用の 0)
    r = simulate_trade(bars, [], T0, T0 + BAR * 6, _cfg(reserve_usdt=0.0))
    assert r.liquidated


def test_t32_reserve_is_frozen_and_part_of_deployed_capital():
    """§24.1: R = C/(L+2) = 2000。deployed_capital は C のまま。"""
    cfg = _cfg()
    assert P.MARGIN_RESERVE_USDT == pytest.approx(
        P.CAPITAL_BASE_USDT / (P.LEVERAGE + 2)
    ), "R は初期証拠金1トランシェ分として導出される"
    assert cfg.reserve_usdt == 2_000.0
    assert cfg.deployed_capital_usdt == P.CAPITAL_BASE_USDT == 10_000.0
    assert cfg.position_capital_usdt == 8_000.0
    # 内訳が C を過不足なく使い切ること
    spot_notional = cfg.position_capital_usdt * cfg.leverage / (cfg.leverage + 1)
    init_margin = spot_notional / cfg.leverage
    assert spot_notional + init_margin + cfg.reserve_usdt == pytest.approx(
        cfg.capital_base_usdt
    )


def test_reserve_is_constant_across_leverage_sensitivities():
    """§24.1: R は L を変えても 2000 のまま。"""
    for lev in P.LEVERAGE_SENSITIVITY:
        cfg = _cfg(leverage=lev)
        assert cfg.reserve_usdt == 2_000.0
        assert cfg.position_capital_usdt == 8_000.0


# --------------------------------------------------------------------------
# T16 — NARDC(L)·(1+1/L) が清算ゼロ時に L 不変
# --------------------------------------------------------------------------


def test_t16_leverage_invariance_is_exact_before_lot_rounding():
    """§11.1 / T16: `q·P·(1+1/L)` は L に依存しない。

    これは**丸め前の連続量**について厳密に成立する。丸めた q で検査すると
    lot 量子化の分だけずれるので、両方を別々に検査する(下のテスト)。
    """
    spot_in = 100_000.0
    exact = []
    for lev in P.LEVERAGE_SENSITIVITY:
        cfg = _cfg(leverage=lev)
        q_raw = cfg.position_capital_usdt * lev / ((lev + 1.0) * spot_in)
        exact.append(q_raw * spot_in * (1.0 + 1.0 / lev))
    assert max(exact) - min(exact) < 1e-9, exact
    # §24.1: 基準は C ではなく C − R
    assert exact[0] == pytest.approx(P.POSITION_CAPITAL_USDT)
    assert P.POSITION_CAPITAL_USDT == pytest.approx(8_000.0)


def test_t16_leverage_invariance_holds_up_to_lot_quantisation():
    """丸め後は lot step 1つ分までしかずれないこと(清算ゼロ時)。"""
    bars = _bars(12, spot=100_000.0, perp=100_400.0)
    bars[10] = Bar(bars[10].ts, 100_000.0, 100_000.0, 100_100.0, 100_100.0, perp_high=100_100.0, mark_high=100_100.0, mark_close=100_100.0)
    seen = []
    for lev in P.LEVERAGE_SENSITIVITY:
        cfg = _cfg(leverage=lev)
        r = simulate_trade(bars, [], T0, T0 + BAR * 9, cfg)
        assert not r.liquidated
        seen.append((r.q_btc * r.perp_in * (1.0 + 1.0 / lev), lev, r.perp_in))
    values = [v for v, _, _ in seen]
    worst_lot = max(P.PERP_LOT_STEP * px * (1.0 + 1.0 / lev) for _, lev, px in seen)
    assert max(values) - min(values) <= worst_lot, (values, worst_lot)


# --------------------------------------------------------------------------
# T34 — spot 片道 > perp 片道
# --------------------------------------------------------------------------


def test_t34_spot_side_is_more_expensive_than_perp_side():
    c = TwoLegCostConfig("base_taker", P.SPOT_TAKER_BPS, P.PERP_TAKER_BPS)
    assert c.spot_bps > c.perp_bps
    assert c.round_trip_bps == pytest.approx(30.0)
    bars = _bars(8, spot=100_000.0, perp=100_000.0)
    r = simulate_trade(bars, [], T0, T0 + BAR * 5, _cfg())
    assert r.cost_spot_in > r.cost_perp_in
    assert r.cost_total == pytest.approx(
        r.cost_spot_in + r.cost_perp_in + r.cost_spot_out + r.cost_perp_out
    )


def test_four_fills_are_charged_not_two():
    bars = _bars(8, spot=100_000.0, perp=100_000.0)
    r = simulate_trade(bars, [], T0, T0 + BAR * 5, _cfg())
    for c in (r.cost_spot_in, r.cost_perp_in, r.cost_spot_out, r.cost_perp_out):
        assert c > 0, "4約定すべてに手数料がかかること"


# --------------------------------------------------------------------------
# 分離会計・時系列・A3 semantics
# --------------------------------------------------------------------------


def test_wallets_stay_separate_and_leverage_is_a_time_series():
    bars = _bars(10, spot=100_000.0, perp=100_050.0)
    ev = FundingEvent(T0 + BAR * 3, 0.0002, 100_050.0, 8.0)
    r = simulate_trade(bars, [ev], T0, T0 + BAR * 7, _cfg())
    assert len(r.bar_states) > 1, "leverage は系列として記録されること"
    assert all(b.leverage_t > 0 for b in r.bar_states)
    # funding は独立列として累積する(A3 レビュー S7)
    assert r.bar_states[-1].cum_funding_cashflow == pytest.approx(r.funding_total)
    assert any(b.funding_cashflow != 0 for b in r.bar_states)


def test_g2_funding_moves_the_futures_wallet_in_both_signs():
    """§24.2: 正負いずれの funding も先物ウォレット残高を動かす。"""
    bars = _bars(10, spot=100_000.0, perp=100_050.0)
    base = simulate_trade(bars, [], T0, T0 + BAR * 7, _cfg())
    pos = simulate_trade(
        bars, [FundingEvent(T0 + BAR * 3, 0.005, 100_050.0, 8.0)], T0, T0 + BAR * 7, _cfg()
    )
    neg = simulate_trade(
        bars, [FundingEvent(T0 + BAR * 3, -0.005, 100_050.0, 8.0)], T0, T0 + BAR * 7, _cfg()
    )
    b0 = base.bar_states[-1].futures_margin_usdt
    assert pos.bar_states[-1].futures_margin_usdt > b0
    assert neg.bar_states[-1].futures_margin_usdt < b0
    assert pos.funding_total > 0 > neg.funding_total


def test_tracking_error_is_recorded_every_bar_without_threshold_exclusion():
    bars = _bars(10, spot=100_000.0, perp=100_050.0)
    r = simulate_trade(bars, [], T0, T0 + BAR * 7, _cfg())
    assert all(b.tracking_error >= 0 for b in r.bar_states)
    # entry fill = bar 1、exit fill = bar 8 の閉区間 → 8 本。閾値で間引いていない
    assert len(r.bar_states) == 8


def test_only_h14a_remains_unfrozen_and_has_no_default():
    """v1.8.5 §31 で H14b が解決し、残るのは H14a(清算手数料)だけ。"""
    assert set(UNFROZEN_PARAMETERS) == {"liquidation_clearance_fee_rate"}
    with pytest.raises(TypeError):
        TwoLegConfig(cost=TwoLegCostConfig("x", 1.0, 1.0))  # type: ignore[call-arg]
    # v1.8.1 で凍結された3件は既定で入る
    cfg = _cfg()
    assert cfg.reserve_usdt == P.MARGIN_RESERVE_USDT
    assert cfg.funding_counts_toward_margin is P.FUNDING_COUNTS_TOWARD_MARGIN is True
    assert cfg.post_liquidation == P.POST_LIQUIDATION_RULE == "unwind"


def test_rehedge_is_rejected_because_v1_8_1_froze_unwind():
    """§24.3: POST_LIQUIDATION_RULE = 'unwind'。再ヘッジは実装しない。"""
    with pytest.raises(ValueError, match="unwind"):
        _cfg(post_liquidation="rehedge")


def test_config_rejects_incoherent_margin_thresholds():
    with pytest.raises(ValueError):
        _cfg(topup_trigger=0.05, topup_target=0.01)
    with pytest.raises(ValueError):
        _cfg(maint_margin_rate=0.02, topup_trigger=0.01)


# ==========================================================================
# v1.8.1 §24.7 — 修正条項のテスト(T35–T41)
# ==========================================================================


def _liq_bars(n: int = 10, spike_at: int = 3, spike: float = 200_000.0) -> list[Bar]:
    highs = [100_050.0] * n
    highs[spike_at] = spike
    return _bars(n, spot=100_000.0, perp=100_050.0, mark_high=highs)


def test_t35_pnl_identity_holds_on_the_liquidation_path():
    """§24.5: 脚が別時刻で終了しても恒等式は保たれる。"""
    bars = _liq_bars()
    r = simulate_trade(bars, [], T0, T0 + BAR * 8, _cfg(reserve_usdt=0.0))
    assert r.liquidated
    assert r.perp_out == r.liquidation_fill
    assert r.spot_out == r.spot_unwind_fill
    assert r.pnl_gross == pytest.approx(r.pnl_gross_by_legs, rel=1e-12, abs=1e-9)


def test_t35_identity_holds_on_liquidation_path_with_funding():
    bars = _liq_bars()
    ev = FundingEvent(T0 + BAR * 2, 0.0003, 100_050.0, 8.0)
    r = simulate_trade(bars, [ev], T0, T0 + BAR * 8, _cfg(reserve_usdt=0.0))
    assert r.liquidated and r.funding_events_applied == 1
    assert r.pnl_gross == pytest.approx(r.pnl_gross_by_legs, rel=1e-12, abs=1e-9)


def test_t36_scheduled_perp_exit_price_is_not_used_after_liquidation():
    """§24.5 a: 清算後に予定 perp exit 価格を使わない。"""
    bars = _liq_bars()
    scheduled = bars[9].perp_open
    r = simulate_trade(bars, [], T0, T0 + BAR * 8, _cfg(reserve_usdt=0.0))
    assert r.liquidated
    assert r.perp_out != scheduled
    assert r.perp_out == pytest.approx(r.liquidation_fill)


def test_t36_no_ordinary_perp_exit_fee_after_forced_close():
    """§24.5 b: 強制決済に通常の taker 手数料を掛けない。"""
    bars = _liq_bars()
    r = simulate_trade(bars, [], T0, T0 + BAR * 8, _cfg(reserve_usdt=0.0))
    assert r.liquidated
    assert r.cost_perp_out == 0.0
    assert r.cost_spot_out > 0.0, "spot 側は通常どおり掛かる"
    assert r.cost_total == pytest.approx(
        r.cost_spot_in + r.cost_perp_in + r.cost_spot_out
    )


def test_t37_liquidation_fee_does_not_double_count_price_pnl():
    """§24.5 c: 価格損失は q(P_in − P_liq) に一度だけ現れる。"""
    bars = _liq_bars()
    free = simulate_trade(bars, [], T0, T0 + BAR * 8, _cfg(reserve_usdt=0.0))
    assert free.liquidation_fee_usdt == 0.0, "fee rate 0 なら手数料は 0"
    # 価格損失は gross に含まれている(ショートが逆行しているので負)
    assert free.pnl_gross < 0
    assert free.pnl_net == pytest.approx(free.pnl_gross - free.cost_total)

    # fee rate を入れた分だけ、ちょうど net が下がる(二重計上しない)
    fee_rate = 0.0050
    paid = simulate_trade(
        bars, [], T0, T0 + BAR * 8, _cfg(reserve_usdt=0.0, liquidation_clearance_fee_rate=fee_rate)
    )
    assert paid.pnl_gross == pytest.approx(free.pnl_gross)
    assert paid.liquidation_fee_usdt == pytest.approx(
        fee_rate * paid.q_btc * paid.liquidation_fill
    )
    assert paid.pnl_net == pytest.approx(free.pnl_net - paid.liquidation_fee_usdt)


def test_t38_spot_unwinds_at_first_causal_open_after_liquidation():
    """§24.3: 清算バーより後の最初に因果的に執行可能な spot open。"""
    bars = _liq_bars(spike_at=3)
    r = simulate_trade(bars, [], T0, T0 + BAR * 8, _cfg(reserve_usdt=0.0))
    assert r.liquidation_ts == bars[3].ts
    assert r.spot_unwind_ts == bars[4].ts, "清算バーの次のバー"
    assert r.spot_unwind_ts > r.liquidation_ts, "因果順序を守ること"
    assert r.spot_out == bars[4].spot_open


def test_t38_spot_unwind_rolls_forward_over_a_missing_spot_bar():
    """欠損 spot バーは §9 M6b の roll-forward で飛ばす。"""
    bars = _liq_bars(spike_at=3)
    bars[4] = Bar(bars[4].ts, None, None, 100_050.0, 100_050.0, perp_high=100_050.0, mark_high=100_050.0, mark_close=100_050.0)
    r = simulate_trade(bars, [], T0, T0 + BAR * 8, _cfg(reserve_usdt=0.0))
    assert r.liquidated
    assert r.spot_unwind_ts == bars[5].ts
    assert r.naked_spot_bars == 1, "飛ばした 1 本が naked spot 期間"


def test_t39_naked_spot_exposure_appears_in_tracking_error():
    """§24.3: perp が消えてから spot を解消するまでの裸エクスポージャ。"""
    bars = _liq_bars(spike_at=3)
    r = simulate_trade(bars, [], T0, T0 + BAR * 8, _cfg(reserve_usdt=0.0))
    liq_state = [b for b in r.bar_states if b.ts == r.liquidation_ts][0]
    assert liq_state.perp_btc == 0.0, "perp 脚が消えている"
    assert liq_state.tracking_error == pytest.approx(1.0), (
        "perp が 0 なら spot がまるごと裸。tracking_error は 1.0 になる"
    )


def test_t40_event_order_is_funding_then_margin_then_topup_then_liquidation():
    """§24.4: funding を先に適用すると、その分だけ清算が回避されうる。"""
    assert P.EVENT_ORDER == "funding_then_margin_then_topup_then_liquidation"
    n = 8
    highs = [100_050.0] * n
    highs[3] = 132_900.0  # funding が無ければ維持証拠金割れ寸前
    bars = _bars(n, spot=100_000.0, perp=100_050.0, mark_high=highs)

    without = simulate_trade(bars, [], T0, T0 + BAR * 6, _cfg(reserve_usdt=0.0))
    big = FundingEvent(T0 + BAR * 3, 0.010, 100_050.0, 8.0)  # 同じバーで大きな受取
    with_funding = simulate_trade(bars, [big], T0, T0 + BAR * 6, _cfg(reserve_usdt=0.0))

    assert without.liquidated, "funding 無しでは清算される水準に置いた"
    assert not with_funding.liquidated, (
        "funding を先に適用する順序なら、同じバーで清算を免れる"
    )
    assert with_funding.funding_events_applied == 1


def test_t40_topup_is_processed_before_liquidation():
    """TOPUP_TRIGGER は維持証拠金より上にあるので必ず先に到達する。"""
    assert P.MARGIN_TOPUP_TRIGGER > P.MAINT_MARGIN_RATE_TIER1
    n = 8
    highs = [100_050.0] * n
    highs[3] = 133_500.0
    bars = _bars(n, spot=100_000.0, perp=100_050.0, mark_high=highs)
    no_reserve = simulate_trade(bars, [], T0, T0 + BAR * 6, _cfg(reserve_usdt=0.0))
    with_reserve = simulate_trade(bars, [], T0, T0 + BAR * 6, _cfg())
    assert no_reserve.liquidated
    assert with_reserve.topup_count >= 1
    assert not with_reserve.liquidated, "追証が清算より先に処理されること"


def test_t41_h14a_unresolved_blocks_the_liquidation_path():
    """§34: H14a 未解決なら清算経路を評価できない(既定のゼロを黙って使わない)。"""
    cfg = TwoLegConfig(
        cost=TwoLegCostConfig("base_taker", P.SPOT_TAKER_BPS, P.PERP_TAKER_BPS),
        liquidation_clearance_fee_rate=None,
        reserve_usdt=0.0,
    )
    assert not cfg.liquidation_cost_is_resolved
    assert P.LIQUIDATION_CLEARANCE_FEE_RATE is None
    assert P.LIQUIDATION_FEE_STATUS == "pending_authoritative_read"
    with pytest.raises(ValueError, match="H14a 未解決"):
        simulate_trade(_liq_bars(), [], T0, T0 + BAR * 8, cfg)


def test_t41_non_liquidating_path_still_runs_with_h14a_unresolved():
    """清算しない限り H14a は必要ない(不必要に止めない)。"""
    cfg = TwoLegConfig(
        cost=TwoLegCostConfig("base_taker", P.SPOT_TAKER_BPS, P.PERP_TAKER_BPS),
        liquidation_clearance_fee_rate=None,
    )
    r = simulate_trade(_bars(10, 100_000.0, 100_050.0), [], T0, T0 + BAR * 7, cfg)
    assert r.opened and not r.liquidated

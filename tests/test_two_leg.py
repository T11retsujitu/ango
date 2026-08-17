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
    base = dict(
        cost=TwoLegCostConfig("base_taker", P.SPOT_TAKER_BPS, P.PERP_TAKER_BPS),
        reserve_usdt=0.0,
        funding_counts_toward_margin=True,
        post_liquidation="unwind",
    )
    base.update(kw)
    return TwoLegConfig(**base)  # type: ignore[arg-type]


def _bars(n: int, spot: float, perp: float, *, mark_high=None, step=0.0) -> list[Bar]:
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
                mark_high=(mark_high[i] if mark_high else p),
                mark_close=p,
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
    bars[10] = Bar(bars[10].ts, 100_200.0, 100_200.0, 100_210.0, 100_210.0, 100_210.0, 100_210.0)
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
    bars[1] = Bar(bars[1].ts, None, None, 100_050.0, 100_050.0, 100_050.0, 100_050.0)
    cfg = _cfg()
    object.__setattr__(cfg.execution, "cancel_after_ms", 5 * 60_000)  # 1 バーで打ち切り
    r = simulate_trade(bars, [], T0, T0 + BAR * 3, cfg)
    assert not r.opened
    assert r.reject_reason == "no_entry_fill_bar_with_both_legs"


def test_t5_m6b_missing_leg_at_exit_rolls_forward_without_voiding_entry():
    bars = _bars(10, spot=100_000.0, perp=100_050.0)
    # exit 予定バー(index 5)の spot が欠損 → 次に両脚が揃うバーへ roll-forward
    bars[5] = Bar(bars[5].ts, None, None, 100_050.0, 100_050.0, 100_050.0, 100_050.0)
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
    cfg = _cfg(capital_base_usdt=100.0)  # 名目 75 USDT、BTC 10万で q→0.000 に丸まる
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
    r = simulate_trade(bars, [], T0, T0 + BAR * 6, _cfg(reserve_usdt=3_000.0))
    assert r.topup_count >= 1, "維持証拠金率が閾値を割ったら追証が発火すること"
    assert r.topup_total_usdt > 0
    assert not r.liquidated
    assert r.bar_states[-1].reserve_usdt < 3_000.0


def test_t32_liquidation_when_reserve_is_exhausted():
    n = 8
    highs = [100_050.0] * n
    highs[3] = 133_000.0  # 清算水準超え。予備資金ゼロでは支えられない
    bars = _bars(n, spot=100_000.0, perp=100_050.0, mark_high=highs)
    r = simulate_trade(bars, [], T0, T0 + BAR * 6, _cfg(reserve_usdt=0.0))
    assert r.liquidated


def test_t32_reserve_is_part_of_deployed_capital():
    """§11.4: 予備資金は deployed_capital に含める(含めなければ資本の過少申告)。"""
    cfg = _cfg(reserve_usdt=2_000.0)
    assert cfg.deployed_capital_usdt == cfg.capital_base_usdt
    assert cfg.position_capital_usdt == cfg.capital_base_usdt - 2_000.0


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
    assert exact[0] == pytest.approx(P.CAPITAL_BASE_USDT)


def test_t16_leverage_invariance_holds_up_to_lot_quantisation():
    """丸め後は lot step 1つ分までしかずれないこと(清算ゼロ時)。"""
    bars = _bars(12, spot=100_000.0, perp=100_400.0)
    bars[10] = Bar(bars[10].ts, 100_000.0, 100_000.0, 100_100.0, 100_100.0, 100_100.0, 100_100.0)
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


def test_funding_can_be_excluded_from_margin_when_configured():
    """§9 M7 が『明示的に凍結する』とした選択肢が両方実装されていること。"""
    bars = _bars(10, spot=100_000.0, perp=100_050.0)
    ev = FundingEvent(T0 + BAR * 3, 0.005, 100_050.0, 8.0)
    inc = simulate_trade(bars, [ev], T0, T0 + BAR * 7, _cfg(funding_counts_toward_margin=True))
    exc = simulate_trade(bars, [ev], T0, T0 + BAR * 7, _cfg(funding_counts_toward_margin=False))
    assert inc.funding_total == pytest.approx(exc.funding_total)
    assert inc.bar_states[-1].futures_margin_usdt > exc.bar_states[-1].futures_margin_usdt


def test_tracking_error_is_recorded_every_bar_without_threshold_exclusion():
    bars = _bars(10, spot=100_000.0, perp=100_050.0)
    r = simulate_trade(bars, [], T0, T0 + BAR * 7, _cfg())
    assert all(b.tracking_error >= 0 for b in r.bar_states)
    # entry fill = bar 1、exit fill = bar 8 の閉区間 → 8 本。閾値で間引いていない
    assert len(r.bar_states) == 8


def test_unfrozen_parameters_have_no_defaults():
    """凍結プロトコルに値が無いものを黙って既定値で埋めていないこと。"""
    assert set(UNFROZEN_PARAMETERS) == {
        "reserve_usdt",
        "funding_counts_toward_margin",
        "post_liquidation",
    }
    with pytest.raises(TypeError):
        TwoLegConfig(cost=TwoLegCostConfig("x", 1.0, 1.0))  # type: ignore[call-arg]


def test_rehedge_rule_is_refused_because_it_is_not_frozen():
    n = 8
    highs = [100_050.0] * n
    highs[3] = 200_000.0
    bars = _bars(n, spot=100_000.0, perp=100_050.0, mark_high=highs)
    with pytest.raises(NotImplementedError, match="凍結プロトコルに無い"):
        simulate_trade(bars, [], T0, T0 + BAR * 6, _cfg(post_liquidation="rehedge"))


def test_config_rejects_incoherent_margin_thresholds():
    with pytest.raises(ValueError):
        _cfg(topup_trigger=0.05, topup_target=0.01)
    with pytest.raises(ValueError):
        _cfg(maint_margin_rate=0.02, topup_trigger=0.01)

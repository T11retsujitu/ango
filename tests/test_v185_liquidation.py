"""v1.8.5 §31–§35 の適合テスト。

**取引系列を作らない。** rho / シグナル / return / PnL / Layer 評価をしない。
検査するのは執行モデル・mark 経路の観測可能性・gate の順序だけである。
"""

from datetime import datetime, timedelta, timezone

import pytest

from mce import phase8_prereg as P
from mce.backtest import mark_path as mp
from mce.backtest.two_leg import (
    UNFROZEN_PARAMETERS,
    Bar,
    TwoLegConfig,
    TwoLegCostConfig,
    simulate_trade,
)

UTC = timezone.utc
T0 = datetime(2024, 6, 1, tzinfo=UTC)
BAR = timedelta(minutes=5)


def _cfg(**kw) -> TwoLegConfig:
    base = dict(
        cost=TwoLegCostConfig("base_taker", P.SPOT_TAKER_BPS, P.PERP_TAKER_BPS),
        liquidation_clearance_fee_rate=0.0,
        reserve_usdt=0.0,
    )
    base.update(kw)
    return TwoLegConfig(**base)  # type: ignore[arg-type]


def _bars(marks, perps=None, *, status="observed", spot=100_000.0, perp=100_050.0):
    """`marks[i]` が mark_high、`perps[i]` が perp_high。**別物として渡す。**"""
    perps = perps if perps is not None else marks
    return [
        Bar(
            ts=T0 + BAR * i, spot_open=spot, spot_close=spot,
            perp_open=perp, perp_close=perp,
            perp_high=perps[i], mark_high=marks[i], mark_close=perp,
            mark_path_status=status,
        )
        for i in range(len(marks))
    ]


#: 清算を確実に起こす mark 経路(leverage 3 なので +30% 超で維持証拠金を割る)
_CALM = 100_050.0
_SPIKE = 190_000.0


def _liq_marks(n=10, at=4):
    return [_CALM if i != at else _SPIKE for i in range(n)]


# --------------------------------------------------------------------------
# §31 H14b — 執行モデル
# --------------------------------------------------------------------------


def test_fixed_slippage_is_gone_from_the_config_and_the_frozen_set():
    assert set(UNFROZEN_PARAMETERS) == {"liquidation_clearance_fee_rate"}
    assert P.LIQUIDATION_FIXED_SLIPPAGE_ALLOWED is False
    assert P.LIQUIDATION_EXECUTION_MODEL == "adverse_trade_extreme_capped_at_bankruptcy"
    with pytest.raises(TypeError):
        TwoLegConfig(
            cost=TwoLegCostConfig("x", 1.0, 1.0),
            liquidation_clearance_fee_rate=0.0,
            liquidation_slippage_bps=1.0,  # type: ignore[call-arg]
        )


def test_fill_is_capped_at_the_bankruptcy_price():
    """perp_high が破産価格を大きく超えても、fill は破産価格で止まる。"""
    r = simulate_trade(
        _bars(_liq_marks(), perps=[_CALM] * 4 + [10**7] + [_CALM] * 5),
        [], T0, T0 + BAR * 8, _cfg(),
    )
    assert r.liquidated and r.fill_rule_binding == "cap"
    trigger_upper = r.liquidation_fill / (1.0 + P.MAINT_MARGIN_RATE_TIER1)
    assert r.liquidation_fill == pytest.approx(
        trigger_upper * (1.0 + P.MAINT_MARGIN_RATE_TIER1)
    )
    assert r.liquidation_fill < 10**7


def test_fill_floors_at_the_trigger_when_perp_high_is_better():
    """short にとってトリガーより良い(低い)約定は仮定しない。"""
    r = simulate_trade(
        _bars(_liq_marks(), perps=[_CALM] * 10), [], T0, T0 + BAR * 8, _cfg()
    )
    assert r.liquidated and r.fill_rule_binding == "floor"


def test_fill_uses_the_observed_extreme_inside_the_band():
    """トリガーと破産価格の間に perp_high があれば、その観測値が効く。"""
    calm = simulate_trade(
        _bars(_liq_marks(), perps=[_CALM] * 10), [], T0, T0 + BAR * 8, _cfg()
    )
    trigger = calm.liquidation_fill
    inside = trigger * (1.0 + P.MAINT_MARGIN_RATE_TIER1 / 2)
    r = simulate_trade(
        _bars(_liq_marks(), perps=[_CALM] * 4 + [inside] + [_CALM] * 5),
        [], T0, T0 + BAR * 8, _cfg(),
    )
    assert r.liquidated and r.fill_rule_binding == "observed"
    assert r.liquidation_fill == pytest.approx(inside)
    assert trigger < r.liquidation_fill < trigger * (1.0 + P.MAINT_MARGIN_RATE_TIER1)


def test_fill_rule_binding_is_always_one_of_the_frozen_values():
    for perps in ([_CALM] * 10, [_CALM] * 4 + [10**7] + [_CALM] * 5):
        r = simulate_trade(_bars(_liq_marks(), perps=perps), [], T0, T0 + BAR * 8, _cfg())
        assert r.fill_rule_binding in P.FILL_RULE_BINDINGS


def test_mark_high_is_trigger_only_and_perp_high_is_execution_only():
    """**入れ替えたら結果が変わる**こと(取り違えが検出できる)。

    片方をバンド内(トリガー〜破産価格)に、もう片方をバンド外に置く。
    入れ替えると binding も fill も変わる。
    """
    calm = simulate_trade(
        _bars(_liq_marks(), perps=[_CALM] * 10), [], T0, T0 + BAR * 8, _cfg()
    )
    trigger = calm.liquidation_fill
    inside = trigger * (1.0 + P.MAINT_MARGIN_RATE_TIER1 / 2)

    marks = _liq_marks()                                   # バンド外(大きく飛ぶ)
    perps = [_CALM] * 4 + [inside] + [_CALM] * 5           # バンド内
    normal = simulate_trade(_bars(marks, perps), [], T0, T0 + BAR * 8, _cfg())
    swapped = simulate_trade(_bars(perps, marks), [], T0, T0 + BAR * 8, _cfg())

    assert normal.liquidated and swapped.liquidated
    assert normal.fill_rule_binding == "observed"
    assert swapped.fill_rule_binding == "cap"
    assert normal.liquidation_fill != swapped.liquidation_fill
    assert P.LIQUIDATION_TRIGGER_FIELD == "mark_high"
    assert P.LIQUIDATION_ADVERSE_FIELD == "perp_high"


def test_missing_perp_high_on_the_liquidation_bar_is_state_unknown():
    """執行代理が無い bar で清算を評価しない。**清算なしとも仮定しない。**"""
    bars = _bars(_liq_marks(), perps=[_CALM] * 10)
    bars[4] = Bar(bars[4].ts, 100_000.0, 100_000.0, 100_050.0, 100_050.0,
                  perp_high=None, mark_high=_SPIKE, mark_close=100_050.0)
    r = simulate_trade(bars, [], T0, T0 + BAR * 8, _cfg())
    assert r.disposition == P.LIQUIDATION_STATE_UNKNOWN_DISPOSITION
    assert not r.liquidated
    assert r.unobservable_mark_bars == 1


def test_liquidation_path_identity_still_holds_with_the_new_fill():
    """清算経路でも q(D_in − D_out) + Funding の脚別積み上げと一致する。"""
    for perps in ([_CALM] * 10, [_CALM] * 4 + [10**7] + [_CALM] * 5):
        r = simulate_trade(_bars(_liq_marks(), perps=perps), [], T0, T0 + BAR * 8, _cfg())
        assert r.liquidated
        assert r.pnl_gross == pytest.approx(r.pnl_gross_by_legs, rel=1e-12)


# --------------------------------------------------------------------------
# §32 mark 経路の観測可能性
# --------------------------------------------------------------------------


def test_frozen_status_set_and_acceptable_subset():
    assert P.MARK_PATH_STATUSES == (
        "observed", "verified_repair", "route_unverified",
        "stale_unverified", "source_unobservable",
    )
    assert P.MARK_PATH_ACCEPTABLE == ("observed", "verified_repair")
    assert mp.is_acceptable("observed") and mp.is_acceptable("verified_repair")
    for bad in ("route_unverified", "stale_unverified", "source_unobservable"):
        assert not mp.is_acceptable(bad)
    with pytest.raises(ValueError):
        mp.is_acceptable("looks_fine")


@pytest.mark.parametrize(
    "status", ["route_unverified", "stale_unverified", "source_unobservable"]
)
def test_unacceptable_mark_path_while_open_aborts_and_is_not_no_liquidation(status):
    r = simulate_trade(
        _bars([_CALM] * 10, status=status), [], T0, T0 + BAR * 8, _cfg()
    )
    assert r.disposition == P.LIQUIDATION_STATE_UNKNOWN_DISPOSITION
    assert r.unobservable_mark_bars >= 1
    assert not r.liquidated  # 清算が「無かった」ことの証明にはならない


def test_current_p1_and_p2_statuses_are_frozen_as_unverified_not_unobservable():
    assert P.MARK_PATH_CURRENT_P1_STATUS == "route_unverified"
    assert P.MARK_PATH_CURRENT_P2_STATUS == "stale_unverified"
    assert P.MARK_PATH_CURRENT_P1_STATUS != "source_unobservable"


# --------------------------------------------------------------------------
# canonical タイムライン — **inner join で消えない**
# --------------------------------------------------------------------------


def _rows(*specs):
    return [{"ts": T0 + BAR * i, "mark_high": 1.0, "mark_close": 1.0,
             "mark_samples": n} for i, n in specs]


def test_missing_bars_survive_as_rows_with_a_status():
    rows = _rows((0, 300), (3, 300))  # 1,2 が欠測
    line = mp.canonical_timeline(rows)
    assert [b.ts for b in line] == [T0 + BAR * i for i in range(4)]
    assert [b.mark_path_status for b in line] == [
        "observed", "route_unverified", "route_unverified", "observed"
    ]
    assert line[1].mark_high is None  # 値は捏造しない


def test_zero_sample_bars_are_stale_unverified():
    line = mp.canonical_timeline(_rows((0, 300), (1, 0), (2, 300)))
    assert [b.mark_path_status for b in line] == [
        "observed", "stale_unverified", "observed"
    ]


def test_probe_classifications_map_to_statuses():
    assert mp.PROBE_CLASS_TO_STATUS == {
        "candidate_deterministic_repair": "verified_repair",
        "mark_path_unobservable": "source_unobservable",
        "probe_blocked_by_egress": "route_unverified",
    }


def test_probe_result_upgrades_a_missing_bar_to_verified_repair():
    missing = T0 + BAR
    probe = {"intervals": [{
        "classification": "candidate_deterministic_repair",
        "target_open_times": [int(missing.timestamp() * 1000)],
    }]}
    line = mp.canonical_timeline(_rows((0, 300), (2, 300)), probe=probe)
    assert line[1].mark_path_status == "verified_repair"
    assert line[1].acceptable


def test_blocked_probe_never_downgrades_to_source_unobservable():
    """遮断は source の欠測に化けない。"""
    missing = T0 + BAR
    probe = {"intervals": [{
        "classification": "probe_blocked_by_egress",
        "target_open_times": [int(missing.timestamp() * 1000)],
    }]}
    line = mp.canonical_timeline(_rows((0, 300), (2, 300)), probe=probe)
    assert line[1].mark_path_status == "route_unverified"


def test_timeline_can_be_extended_beyond_the_available_rows():
    line = mp.canonical_timeline(_rows((0, 300)), start=T0, end=T0 + BAR * 2)
    assert len(line) == 3
    assert [b.mark_path_status for b in line][1:] == ["route_unverified"] * 2


# --------------------------------------------------------------------------
# §33 gate の順序 / §34 H14a
# --------------------------------------------------------------------------


def test_gate_order_is_frozen():
    assert P.GATE_ORDER == (
        "mark_path_observability", "liquidation_detection", "liquidation_count",
        "h14a_fee_gate", "economic_metrics",
    )


def test_mark_path_gate_precedes_the_liquidation_count():
    """観測できない経路を liquidation_count == 0 として通さない。"""
    assert mp.evaluate_gates(
        mark_path_ok=False, liquidation_count=0, clearance_fee_resolved=True
    ) == P.LIQUIDATION_STATE_UNKNOWN_DISPOSITION
    # 手数料が解決済みでも、件数が 0 でも、mark 経路が先に効く
    assert mp.evaluate_gates(
        mark_path_ok=False, liquidation_count=5, clearance_fee_resolved=False
    ) == P.LIQUIDATION_STATE_UNKNOWN_DISPOSITION


def test_h14a_is_non_binding_without_liquidations():
    assert mp.evaluate_gates(
        mark_path_ok=True, liquidation_count=0, clearance_fee_resolved=False
    ) is None


def test_h14a_blocks_when_liquidations_exist_and_the_fee_is_unresolved():
    assert mp.evaluate_gates(
        mark_path_ok=True, liquidation_count=1, clearance_fee_resolved=False
    ) == P.LIQUIDATION_MODEL_BLOCKED_DISPOSITION
    assert P.LIQUIDATION_FEE_ZERO_SUBSTITUTION_ALLOWED is False


def test_everything_resolved_passes_to_economic_metrics():
    assert mp.evaluate_gates(
        mark_path_ok=True, liquidation_count=3, clearance_fee_resolved=True
    ) is None


def test_layer_disposition_trips_on_a_single_bad_bar():
    good = mp.MarkBar(T0, 1.0, 1.0, 300, "observed")
    bad = mp.MarkBar(T0 + BAR, None, None, None, "route_unverified")
    assert mp.layer_disposition(
        [good, good], liquidation_count=0, clearance_fee_resolved=False
    ) is None
    assert mp.layer_disposition(
        [good, bad], liquidation_count=0, clearance_fee_resolved=True
    ) == P.LIQUIDATION_STATE_UNKNOWN_DISPOSITION


# --------------------------------------------------------------------------
# §35 H13
# --------------------------------------------------------------------------


def test_h13_is_the_sole_hard_blocker_for_arm_r_signals():
    assert P.ARM_R_SIGNAL_HARD_BLOCKER == "H13"
    assert P.COMMISSION_RATE_STATUS == "pending_authenticated_read"

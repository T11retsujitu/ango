"""Phase 8.1 の凍結が守られていることの機械的検査。

Phase 7 の教訓(dev findings §6.2 / confirmation §6.1):
    - 凍結は「仕様」だけでなく「実行器が仕様を実装しきっていること」まで確認して行う
    - 実行後に凍結仕様を編集したら検出できなければならない
"""

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from mce import phase8_prereg as P
from mce.backtest import splits

REPO = Path(__file__).resolve().parents[1]
# v1.8 は不変の歴史記録として保存する。整合検査は**現行(active)記録**に対して行う。
FREEZE_V1_8 = REPO / "experiments" / "phase8" / "carry_freeze.json"
FREEZE_V1_8_1 = REPO / "experiments" / "phase8" / "carry_freeze_v1_8_1.json"
FREEZE_V1_8_2 = REPO / "experiments" / "phase8" / "carry_freeze_v1_8_2.json"
FREEZE_V1_8_3 = REPO / "experiments" / "phase8" / "carry_freeze_v1_8_3.json"
FREEZE_V1_8_4 = REPO / "experiments" / "phase8" / "carry_freeze_v1_8_4.json"
FREEZE = REPO / "experiments" / "phase8" / "carry_freeze_v1_8_5.json"  # active
PREDECESSORS = (
    (FREEZE_V1_8, "v1.8"),
    (FREEZE_V1_8_1, "v1.8.1"),
    (FREEZE_V1_8_2, "v1.8.2"),
    (FREEZE_V1_8_3, "v1.8.3"),
    (FREEZE_V1_8_4, "v1.8.4"),
)

pytestmark = pytest.mark.skipif(not FREEZE.exists(), reason="phase8 freeze 記録が無い")

UTC = timezone.utc


def _record() -> dict:
    return json.loads(FREEZE.read_text(encoding="utf-8"))


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# --------------------------------------------------------------------------
# 封印不変条件(決定ログの制約)
# --------------------------------------------------------------------------


def test_final_oos_start_was_not_modified_or_weakened():
    """決定ログ: FINAL_OOS_START を変更も弱化もしない。"""
    assert splits.FINAL_OOS_START == datetime(2026, 1, 1, tzinfo=UTC)
    rec = _record()["seal_invariants"]
    assert rec["FINAL_OOS_START"] == splits.FINAL_OOS_START.isoformat()
    assert rec["FINAL_OOS_START_modified_by_phase8"] is False


def test_phase8_prospective_start_is_the_approved_value():
    assert splits.PHASE8_PROSPECTIVE_START == datetime(2026, 9, 1, tzinfo=UTC)
    assert P.LAYER3_START == splits.PHASE8_PROSPECTIVE_START


def test_contaminated_band_is_closed_to_phase8():
    """2026-01-01 〜 2026-08-31 は Phase 8 の結果評価に使わない。"""
    start, end = splits.PHASE8_CONTAMINATED_BAND
    assert start == splits.FINAL_OOS_START
    assert end == splits.PHASE8_PROSPECTIVE_START
    assert _record()["seal_invariants"][
        "phase8_contaminated_band_readable_for_outcome_evaluation"
    ] is False
    # 帯の内側は phase8_contaminated に分類されること
    for ts in (
        datetime(2026, 1, 1, tzinfo=UTC),
        datetime(2026, 5, 15, tzinfo=UTC),
        datetime(2026, 8, 31, 23, 55, tzinfo=UTC),
    ):
        assert splits.phase8_layer(ts) == "phase8_contaminated"


def test_phase8_layers_partition_the_timeline():
    cases = {
        datetime(2020, 6, 1, tzinfo=UTC): "literature_in_sample",
        datetime(2025, 5, 31, tzinfo=UTC): "literature_in_sample",
        datetime(2025, 6, 1, tzinfo=UTC): "contaminated_confirmation",
        datetime(2025, 12, 31, tzinfo=UTC): "contaminated_confirmation",
        datetime(2026, 1, 1, tzinfo=UTC): "phase8_contaminated",
        datetime(2026, 9, 1, tzinfo=UTC): "phase8_prospective_final",
        datetime(2027, 1, 1, tzinfo=UTC): "phase8_prospective_final",
    }
    for ts, expected in cases.items():
        assert splits.phase8_layer(ts) == expected, ts


def test_existing_split_semantics_are_unchanged():
    """Phase 8 の追加は既存 split の意味を変えていない。"""
    assert splits.assign(datetime(2026, 9, 1, tzinfo=UTC)) == splits.SEALED_SPLIT
    assert splits.assign(datetime(2026, 5, 1, tzinfo=UTC)) == splits.SEALED_SPLIT
    assert splits.RESEARCH_START == datetime(2023, 11, 19, tzinfo=UTC)
    assert splits.VALIDATION_START == datetime(2025, 7, 1, tzinfo=UTC)


# --------------------------------------------------------------------------
# 凍結違反の検出
# --------------------------------------------------------------------------


def test_frozen_spec_was_not_edited_after_the_freeze():
    """事前登録文書と凍結モジュールが freeze 記録と一致すること。"""
    rec = _record()
    for key in (
        "prereg_doc",
        "prereg_module",
        "prior_register",
        "a3_source_review",
        "splits_module",
        "conformance_notes",
        "h15_investigation",
        "signal_module",
        "rate_adapter",
        "probe_findings",
        "mark_path_module",
        "input_plumbing_notes",
        "mark_probe_findings",
    ):
        entry = rec[key]
        path = REPO / entry["path"]
        assert path.exists(), f"{entry['path']} が存在しない"
        assert _sha256(path) == entry["sha256"], (
            f"{entry['path']} が凍結後に編集されている(凍結違反)"
        )


def test_protocol_declares_itself_frozen():
    rec = _record()
    assert rec["state"] == "FROZEN"
    assert rec["protocol_version"] == P.PROTOCOL_VERSION == "v1.8.5"
    text = (REPO / rec["prereg_doc"]["path"]).read_text(encoding="utf-8")
    assert "FROZEN" in text


def test_prior_register_is_sealed_and_hashed_only():
    """恒久ルール3: 事前予想は封印し、ハッシュだけを記録する。"""
    rec = _record()["prior_register"]
    assert rec["sealed"] is True
    assert len(rec["sha256"]) == 64


# --------------------------------------------------------------------------
# H12 / H13 と実験ブロック
# --------------------------------------------------------------------------


def test_h12_rate_term_matches_the_decision_log():
    assert P.RATE_SOURCE == "aave_variable_borrow_apr"
    assert set(P.RATE_ASSETS) == {"USDT", "USDC", "DAI"}
    assert P.RATE_AGGREGATION == "equal_weight_mean"
    assert P.R_PRIME == 0.0
    assert P.RATE_POINT_IN_TIME is True
    # Kenneth-French は感応度であって primary ではない
    assert P.RATE_SENSITIVITY_SOURCE == "kenneth_french_daily_rf"
    assert P.RATE_SOURCE != P.RATE_SENSITIVITY_SOURCE


def test_commission_rate_is_declared_unresolved_and_blocks_experiments():
    """H13 が未解決である限り実験を走らせてはならない。"""
    assert P.COMMISSION_RATE_STATUS == "pending_authenticated_read"
    h13 = _record()["unresolved_at_freeze"]["H13"]
    assert h13["http_status"] == 401
    assert h13["orders_placed"] is False
    assert h13["raw_response_sha256"] == hashlib.sha256(
        h13["raw_response"].encode()
    ).hexdigest()
    assert _record()["post_freeze_policy"]["experiments_permitted"] is False


def test_spot_leg_is_more_expensive_than_perp_leg():
    """Binance の実勢: spot VIP-0 taker 0.100% > USD-M taker。"""
    spot, perp = P.COST_SCENARIOS[P.PRIMARY_COST_SCENARIO]
    assert spot == P.SPOT_TAKER_BPS == 10.0
    assert perp == P.PERP_TAKER_BPS
    assert spot > perp


# --------------------------------------------------------------------------
# A2 の式と family
# --------------------------------------------------------------------------


def test_arb_bounds_match_a2_table3_caption():
    import math

    c = 0.0030
    assert P.KAPPA == 1095.0
    assert P.arb_bound_upper(c) == pytest.approx(P.KAPPA * math.log(1 + c))
    assert P.arb_bound_lower(c) == pytest.approx(P.KAPPA * math.log(1 - c))
    assert P.arb_bound_upper(c) > 0 > P.arb_bound_lower(c)


def test_family_covers_cost_tiers_only_and_excludes_arm_e():
    assert P.FAMILY_SIZE == len(P.COST_TIERS)
    assert P.PRIMARY_ARM == "R"
    assert P.A2_VARIANT == "long_spot_only"
    # Arm E の horizon が family に混ざっていない
    assert P.FAMILY_SIZE < len(P.ARM_E_HORIZONS_HOURS)


def test_funding_boundary_and_interval_handling():
    assert P.FUNDING_BOUNDARY == "entry_exclusive_exit_inclusive"
    assert P.READ_FUNDING_INTERVAL_PER_ROW is True
    # publication-delay シフトと陳腐化ガードは別物
    assert P.DELTA_PUB_SECONDS >= 0
    # H16(§28): 陳腐化ガードは系列ごとに分離された
    assert P.FUNDING_MAX_STALE_SECONDS > P.DELTA_PUB_SECONDS
    assert not hasattr(P, "MAX_STALE_SECONDS"), "単一定数は廃止された"


def test_sample_floor_and_randomization_are_frozen():
    assert P.MIN_TRADES["layer2"] >= 20
    assert P.MIN_TRADES["layer3"] >= 20
    assert P.K_RANDOM >= 1000
    assert 0 < P.EXPOSURE_GUARD < 1
    assert P.BOOTSTRAP_UNIT == "nonoverlapping_trade"


def test_a3_diagnostic_thresholds_were_not_copied():
    """決定ログ: A3 の diagnostic threshold を protocol parameter にしない。"""
    frozen = {
        name: getattr(P, name)
        for name in dir(P)
        if name.isupper() and isinstance(getattr(P, name), (int, float))
    }
    # A3 の REL_REBAL_THRESHOLD = 1e-4 をそのまま持ち込んでいないこと
    assert 1e-4 not in frozen.values(), "A3 の REL_REBAL_THRESHOLD が混入している"
    review = (REPO / "docs" / "phase8" / "a3_source_review_v1.md").read_text(encoding="utf-8")
    assert "REL_REBAL_THRESHOLD" in review and "採用しない" in review


# --------------------------------------------------------------------------
# v1.8.1 修正条項(§24)
# --------------------------------------------------------------------------


def test_v1_8_record_is_preserved_immutably():
    """v1.8 の凍結記録が残っており、v1.8.1 に上書きされていないこと。"""
    assert FREEZE_V1_8.exists(), "v1.8 の凍結記録が消えている"
    old = json.loads(FREEZE_V1_8.read_text(encoding="utf-8"))
    assert old["protocol_version"] == "v1.8"
    assert old["state"] == "FROZEN"
    # v1.8.1 は v1.8 を明示的に supersede すると宣言していること
    rec = _record()
    assert rec["supersedes"]["version"] == "v1.8.4"
    assert any("v1.8" in c for c in rec["supersedes"]["chain"])
    for path, ver in PREDECESSORS:
        assert path.exists(), f"{ver} の凍結記録が消えている"
        assert json.loads(path.read_text(encoding="utf-8"))["protocol_version"] == ver
    # v1.8 のハッシュは**現行ファイルと一致しない**(改訂したのだから当然)
    assert old["prereg_doc"]["sha256"] != rec["prereg_doc"]["sha256"]


def test_amendment_did_not_touch_the_hypothesis():
    """§24: パラメータ確定のみ。仮説・family・layer・昇格規則は不変。"""
    scope = _record()["amendment_scope"]
    for item in (
        "layer boundaries",
        "promotion rules",
        "family",
        "arm definitions",
        "horizon set",
        "GO/NO-GO",
    ):
        assert item in scope["unchanged"], item
    # 実際の定数でも確認する
    assert P.FAMILY_SIZE == len(P.COST_TIERS) == 3
    assert P.LAYER1_END == datetime(2025, 6, 1, tzinfo=UTC)
    assert P.LAYER3_START == datetime(2026, 9, 1, tzinfo=UTC)
    assert P.PRIMARY_ARM == "R"


def test_g1_reserve_is_frozen_and_derived():
    assert P.MARGIN_RESERVE_USDT == 2_000.0
    assert P.MARGIN_RESERVE_USDT == pytest.approx(P.CAPITAL_BASE_USDT / (P.LEVERAGE + 2))
    assert P.POSITION_CAPITAL_USDT == P.CAPITAL_BASE_USDT - P.MARGIN_RESERVE_USDT == 8_000.0


def test_g2_g3_g4_are_frozen():
    assert P.FUNDING_COUNTS_TOWARD_MARGIN is True
    assert P.POST_LIQUIDATION_RULE == "unwind"
    assert P.EVENT_ORDER == "funding_then_margin_then_topup_then_liquidation"
    # 追証が清算より先に到達する構造であること
    assert P.MARGIN_TOPUP_TRIGGER > P.MAINT_MARGIN_RATE_TIER1


def test_h14a_is_unresolved_and_blocks_experiments():
    assert P.LIQUIDATION_CLEARANCE_FEE_RATE is None
    assert P.LIQUIDATION_FEE_STATUS == "pending_authoritative_read"
    h14 = _record()["unresolved_at_freeze"]["H14a"]
    assert h14["orders_placed"] is False
    assert h14["established"], "確認できた事実が記録されていること"
    assert h14["not_obtainable"], "取得できなかった経路が記録されていること"
    policy = _record()["post_freeze_policy"]
    assert policy["experiments_permitted"] is False
    # 全体集合は test_all_three_blockers_gate_experiments が検査する
    assert "H14a" in policy["blocked_by"]


def test_liquidation_slippage_zero_was_never_silently_retained():
    """決定ログ: liquidation_slippage_bps=0.0 を黙って維持しない。

    v1.8.1〜v1.8.4 では**未凍結パラメータとして保持**することで維持を防いだ。
    v1.8.5 §31 で H14b が解決し、**パラメータ自体を廃止**した(滑りは市場状態
    依存であり取引所の固定値ではないため)。ゼロが既定として入り込む経路が
    存在しないことを、より強い形で固定する。
    """
    from mce.backtest import two_leg
    from mce.backtest.two_leg import UNFROZEN_PARAMETERS

    assert set(UNFROZEN_PARAMETERS) == {"liquidation_clearance_fee_rate"}
    assert P.LIQUIDATION_FIXED_SLIPPAGE_ALLOWED is False
    import dataclasses

    names = {f.name for f in dataclasses.fields(two_leg.TwoLegConfig)}
    assert "liquidation_slippage_bps" not in names
    src = (REPO / "src" / "mce" / "backtest" / "two_leg.py").read_text(encoding="utf-8")
    code = "\n".join(
        ln for ln in src.splitlines() if not ln.lstrip().startswith("#")
    )
    assert "liquidation_slippage_bps" not in code, "コメント以外に識別子が残っている"
    assert "slip_bps" not in code
    with pytest.raises(TypeError):
        two_leg.TwoLegConfig(
            cost=two_leg.TwoLegCostConfig("x", 1.0, 1.0),
            liquidation_clearance_fee_rate=0.0,
            liquidation_slippage_bps=0.0,
        )


# --------------------------------------------------------------------------
# v1.8.2 — 仕様の優先順位と H15
# --------------------------------------------------------------------------


def test_specification_precedence_is_declared():
    """§25: 後の凍結改訂節が同一フィールドの先行記述を supersede する。"""
    assert P.SPEC_PRECEDENCE.startswith("later_frozen_amendment_supersedes_earlier")
    rec = _record()
    assert "supersedes_earlier" in rec["spec_precedence"]
    text = (REPO / rec["prereg_doc"]["path"]).read_text(encoding="utf-8")
    assert "仕様の優先順位" in text
    assert "歴史的な監査証跡" in text


def test_h15_is_adopted_as_a_partial_proxy_and_says_so():
    """§27: 部分 proxy として採用。**厳密再現だと主張しない**。"""
    assert P.RATE_SOURCE_FIDELITY == "partial_proxy_not_exact_A2"
    assert P.RATE_MARKET_IDENTITY_STATUS == "adopted_partial_proxy"
    h15 = _record()["resolved_in_v1_8_3"]["H15"]
    assert "not an exact A2 reconstruction" in h15["resolution"]
    assert "V4" in h15["v4_excluded"]
    assert h15["sensitivity_retained"].startswith("Kenneth-French")
    # H15 は未解決リストから外れたが、H13/H14 は残っている
    assert "H15" not in _record()["unresolved_at_freeze"]


def test_h16_split_the_stale_constants():
    """§28: funding 用の 9h が金利入力に効いていた誤りの是正。"""
    assert P.FUNDING_MAX_STALE_SECONDS == 9 * 3600
    assert P.RATE_MAX_STALE_SECONDS == 24 * 3600
    assert not hasattr(P, "MAX_STALE_SECONDS")
    h16 = _record()["resolved_in_v1_8_3"]["H16"]
    assert "9h" in h16["defect"]
    assert "00:00 UTC" in h16["snapshot_age_semantics"]


def test_source_sensitivity_disposition_is_frozen():
    """§29: 符号が逆なら GO ではなく source_sensitive。"""
    assert P.SOURCE_SENSITIVE_DISPOSITION == "source_sensitive"
    rule = _record()["resolved_in_v1_8_3"]["source_sensitivity"]["rule"]
    assert "NOT GO" in rule and "kenneth" in rule.lower()


def test_v4_is_excluded_from_phase8():
    assert "aave_v4" in P.RATE_MARKET_EXCLUDED_VERSIONS
    assert P.RATE_MARKET_SPLICES[-1][0] == "aave_v3_core"
    assert P.RATE_MARKET_SPLICES[-1][2] is None


def test_remaining_blockers_gate_experiments():
    """H15 は解決したが H13 / H14 は残る。"""
    policy = _record()["post_freeze_policy"]
    assert policy["experiments_permitted"] is False
    assert set(policy["blocked_by"]) == {"H13", "H14a"}


# --------------------------------------------------------------------------
# v1.8.4(§30。D1 / H17 / O1)
# --------------------------------------------------------------------------


def test_all_predecessor_records_are_preserved_byte_for_byte():
    """v1.8 〜 v1.8.4 を**1バイトも変えていない**こと。"""
    frozen = _record()["preserved_predecessors"]
    assert len(frozen) == 5
    for path, ver in PREDECESSORS:
        assert path.exists(), f"{ver} の凍結記録が消えている"
        assert _sha256(path) == frozen[ver], f"{ver} の凍結記録が書き換えられている"


def test_d1_source_of_truth_and_transport_are_separated():
    """§30.1: 経済的な源は contract state。RPC 提供者は transport。"""
    assert P.RATE_SOURCE_OF_TRUTH == "aave_contract_state_on_ethereum_mainnet"
    assert P.RATE_ACCESS_ROUTE == "archive_rpc_eth_call"
    assert P.RATE_ACCESS_PROVIDER_ROLE == "transport_not_economic_source"
    assert P.RATE_CHAIN_ID == 1
    assert set(P.RATE_PROVENANCE_REQUIRED) == {
        "chain_id", "block_number", "block_timestamp", "block_hash"
    }
    rec = _record()["resolved_in_v1_8_4"]["D1"]
    assert rec["provider_is"] == "transport_not_economic_source"


def test_h17_completeness_is_reserve_list_membership():
    """§30.2: 完全性は**同一ブロックでの初期化済み reserve list membership**。"""
    assert P.RATE_COMPLETENESS_RULE == (
        "initialized_reserve_list_membership_at_observation_block"
    )
    assert P.RATE_MEMBERSHIP_BLOCK_RULE == "same_block_as_rate_read"
    assert dict(P.RATE_RESERVE_LIST_PRIMITIVE) == {
        "aave_v1": "getReserves()",
        "aave_v2": "getReservesList()",
        "aave_v3_core": "getReservesList()",
    }
    assert P.RATE_BASKET_REQUIRE_ALL is True


def test_h17_forbidden_repairs_are_all_declared_false():
    """0 代替 / 2資産 fallback / 世代延長 / splice 移動 / forward-fill を禁じる。"""
    assert P.RATE_ZERO_SUBSTITUTION_ALLOWED is False
    assert P.RATE_TWO_ASSET_FALLBACK_ALLOWED is False
    assert P.RATE_GENERATION_EXTENSION_ALLOWED is False
    assert P.RATE_SPLICE_DATES_MOVABLE is False
    assert P.RATE_FORWARD_FILL_ALLOWED is False
    assert P.RATE_INTERPOLATION == "none"
    assert P.RATE_MISSING_COMPONENT_ACTION == "null_mean_and_record_missing_components"


def test_h17_zero_struct_is_only_a_cross_check_and_disagreement_blocks():
    assert P.RATE_ZERO_STRUCT_DIAGNOSTIC == "independent_cross_check_only"
    assert P.RATE_INTEGRITY_DISAGREEMENT_ACTION == "emit_integrity_error_and_no_rate_value"


def test_h17_launch_gap_is_derived_not_hard_coded():
    """期待帰結を日付規則にしていないこと。"""
    assert P.RATE_LAUNCH_GAP_DERIVATION == "derived_from_historical_reserve_membership"
    adapter = (REPO / "src" / "mce" / "aave_rates.py").read_text(encoding="utf-8")
    # 欠測を作り出す日付リテラルが実装に無いこと
    for banned in ("2023-01-27", "2023-02-13", "2023-02-14"):
        assert banned not in adapter, f"欠測期間が日付でハードコードされている: {banned}"


def test_o1_valid_launch_era_values_are_not_altered():
    assert P.RATE_VALUE_TREATMENT == "no_filter_no_clip_no_smoothing_no_winsorization"


def test_v1_8_4_did_not_touch_the_hypothesis_or_the_seal():
    """§30 は入力データ源のみ。仮説・layer・封印・ブロッカーは不変。"""
    scope = json.loads(FREEZE_V1_8_4.read_text(encoding="utf-8"))["amendment_scope"]
    assert set(scope["changed"]) == {"D1", "H17", "O1"}
    assert splits.FINAL_OOS_START == datetime(2026, 1, 1, tzinfo=UTC)
    assert splits.PHASE8_PROSPECTIVE_START == datetime(2026, 9, 1, tzinfo=UTC)
    assert P.KAPPA == 1095.0
    assert P.A2_VARIANT == "long_spot_only"
    assert P.RATE_ASSETS == ("USDT", "USDC", "DAI")
    assert P.RATE_SOURCE == "aave_variable_borrow_apr"
    assert P.RATE_SENSITIVITY_SOURCE == "kenneth_french_daily_rf"
    assert P.SOURCE_SENSITIVE_DISPOSITION == "source_sensitive"


def test_h13_h14a_still_block_experiments_after_v1_8_5():
    policy = _record()["post_freeze_policy"]
    assert policy["experiments_permitted"] is False
    assert set(policy["blocked_by"]) == {"H13", "H14a"}
    assert P.COMMISSION_RATE_STATUS == "pending_authenticated_read"
    assert P.LIQUIDATION_FEE_STATUS == "pending_authoritative_read"
    # v1.8.4 は入力データ源のみを扱い、ブロッカーを解除していない
    assert _record()["resolved_in_v1_8_4"]["unblocks_experiments"] is False


# --------------------------------------------------------------------------
# v1.8.5(§31–§35。P3 / P4 / H14b / mark 経路 / gate 順序 / H14a / H13)
# --------------------------------------------------------------------------


def test_p3_preserves_m6a_m6b_and_adds_no_close_time_filter():
    p3 = _record()["resolved_in_v1_8_5"]["P3"]
    assert p3["decision"] == "preserve_m6a_m6b_semantics_for_spot_gaps"
    assert p3["special_filter_for_anomalous_close_time"] is False


def test_h14b_execution_model_is_frozen_with_exclusive_roles():
    rec = _record()["resolved_in_v1_8_5"]["P4_H14b"]
    assert rec["model"] == P.LIQUIDATION_EXECUTION_MODEL
    assert rec["trigger_field"] == P.LIQUIDATION_TRIGGER_FIELD == "mark_high"
    assert rec["execution_field"] == P.LIQUIDATION_ADVERSE_FIELD == "perp_high"
    assert rec["roles_are_exclusive"] is True
    assert rec["fixed_slippage_removed"] is True
    assert rec["fill_rule_binding"] == list(P.FILL_RULE_BINDINGS)
    assert rec["missing_execution_proxy"] == P.LIQUIDATION_STATE_UNKNOWN_DISPOSITION


def test_mark_path_observability_is_machine_readable_and_survives_joins():
    rec = _record()["resolved_in_v1_8_5"]["mark_path_observability"]
    assert rec["machine_readable"] is True and rec["survives_joins"] is True
    assert "retains missing bars" in rec["no_inner_join"]
    assert rec["statuses"] == list(P.MARK_PATH_STATUSES)
    assert rec["acceptable_while_open"] == list(P.MARK_PATH_ACCEPTABLE)
    assert rec["violation_disposition"] == P.LIQUIDATION_STATE_UNKNOWN_DISPOSITION
    assert rec["not_counted_as_zero_liquidations"] is True
    assert rec["carry_forward_is_not_evidence_of_intrabar_adverse_path"] is True


def test_current_p1_p2_are_unverified_not_unobservable():
    rec = _record()["resolved_in_v1_8_5"]["mark_path_observability"]
    assert rec["current_P1"] == P.MARK_PATH_CURRENT_P1_STATUS == "route_unverified"
    assert rec["current_P2"] == P.MARK_PATH_CURRENT_P2_STATUS == "stale_unverified"
    assert rec["P1_is_not_source_unobservable"] is True
    probe = _record()["mark_gap_probe"]
    assert "no interval classified source_unobservable" in probe["result"]


def test_gate_order_is_frozen_in_the_record():
    assert _record()["resolved_in_v1_8_5"]["gate_order"] == list(P.GATE_ORDER)
    assert P.GATE_ORDER[0] == "mark_path_observability"
    assert P.GATE_ORDER.index("liquidation_count") > P.GATE_ORDER.index(
        "mark_path_observability"
    )
    assert P.GATE_ORDER.index("h14a_fee_gate") < P.GATE_ORDER.index("economic_metrics")


def test_h14a_is_conditional_and_never_zero_substituted():
    rec = _record()["resolved_in_v1_8_5"]["H14a"]
    assert rec["conditional"] is True
    assert rec["no_liquidation"] == "non_binding"
    assert rec["liquidation_with_unresolved_fee"] == (
        P.LIQUIDATION_MODEL_BLOCKED_DISPOSITION
    )
    assert rec["zero_fee_substitution_allowed"] is False
    assert P.LIQUIDATION_FEE_ZERO_SUBSTITUTION_ALLOWED is False


def test_h13_is_recorded_as_the_sole_arm_r_hard_blocker():
    rec = _record()["resolved_in_v1_8_5"]["H13"]
    assert "sole hard blocker" in rec["status"]
    assert "rho_u(C)" in rec["reason"]
    assert P.ARM_R_SIGNAL_HARD_BLOCKER == "H13"


def test_v1_8_5_does_not_unblock_experiments():
    assert _record()["resolved_in_v1_8_5"]["unblocks_experiments"] is False
    permitted = _record()["permitted_after_this_freeze"]
    assert permitted["conformance_and_unit_tests"] is True
    for key in ("empirical_arm_r_signals", "rho_over_empirical_data", "returns_pnl",
                "layer_1_2_3", "final_oos_read"):
        assert permitted[key] is False, key


def test_v1_8_5_did_not_touch_the_hypothesis_or_the_seal():
    scope = _record()["amendment_scope"]
    assert set(scope["changed"]) == {
        "P3", "P4", "H14b", "H14a", "mark_path_observability", "gate_order"
    }
    for item in ("hypothesis", "family", "layer boundaries", "promotion rules",
                 "event order (§24.4)", "liquidation accounting (§24.5)",
                 "FINAL_OOS_START", "seals", "H13"):
        assert item in scope["unchanged"], item
    assert splits.FINAL_OOS_START == datetime(2026, 1, 1, tzinfo=UTC)
    assert P.KAPPA == 1095.0 and P.A2_VARIANT == "long_spot_only"
    assert P.MAINT_MARGIN_RATE_TIER1 == 0.004
    assert P.EVENT_ORDER == "funding_then_margin_then_topup_then_liquidation"

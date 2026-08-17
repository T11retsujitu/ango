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
FREEZE = REPO / "experiments" / "phase8" / "carry_freeze.json"

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
    for key in ("prereg_doc", "prereg_module", "prior_register", "a3_source_review", "splits_module"):
        entry = rec[key]
        path = REPO / entry["path"]
        assert path.exists(), f"{entry['path']} が存在しない"
        assert _sha256(path) == entry["sha256"], (
            f"{entry['path']} が凍結後に編集されている(凍結違反)"
        )


def test_protocol_declares_itself_frozen():
    rec = _record()
    assert rec["state"] == "FROZEN"
    assert rec["protocol_version"] == P.PROTOCOL_VERSION == "v1.8"
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
    assert P.MAX_STALE_SECONDS > P.DELTA_PUB_SECONDS


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

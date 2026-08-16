"""事前登録(文書)と凍結定数(コード)と実データの列の整合性。

事前登録は「後から書き換えない」ことに価値がある。文書とコードが別々に育って
ズレると凍結の意味が消えるので、両者を機械的に照合する。
"""

import ast
import re
from pathlib import Path

import pytest

from mce import features, features_tier0
from mce import tier0_prereg as prereg
from mce.backtest.splits import FINAL_OOS_START

REPO = Path(__file__).resolve().parents[1]
DOC = REPO / "docs" / "phase7" / "tier0_screening_preregistration_v1.md"
FEATURES = REPO / "data" / "features" / "binance_BTCUSDT_5m.parquet"


def test_family_is_exactly_enumerated():
    fam = prereg.family()
    assert len(fam) == prereg.FAMILY_SIZE == 27
    assert len(set(fam)) == len(fam)  # 重複が無い
    for set_id, horizon, target in fam:
        assert target in prereg.TARGETS
        assert horizon > 0


def test_doc_family_table_matches_constants():
    """文書の family 表と定数の列挙が一致すること。"""
    text = DOC.read_text(encoding="utf-8")
    rows = re.findall(r"^\| (T0-[A-C]\d?) \| ([\d, ]+) \| ([Y0-9, ]+) \| (\d+) \|$", text, re.M)
    assert rows, "family 表が文書から読み取れない"
    from_doc = {}
    for set_id, horizons, targets, count in rows:
        hs = tuple(int(h) for h in horizons.split(","))
        ts = tuple(t.strip() for t in targets.split(","))
        assert len(hs) * len(ts) == int(count), f"{set_id} の件数が表内で矛盾している"
        from_doc[set_id] = hs
    from_code = {s.id: s.horizons_bars for s in prereg.INFORMATION_SETS}
    assert from_doc == from_code
    assert sum(len(h) for h in from_doc.values()) * len(prereg.TARGETS) == prereg.FAMILY_SIZE


def test_baseline_columns_are_declared_observables():
    for column in prereg.A_BASE_COLUMNS:
        assert column in features.AVAILABILITY, f"{column} が data contract に未宣言"


def test_information_set_source_columns_are_declared_tier0_observables():
    for info_set in prereg.INFORMATION_SETS:
        for column in info_set.source_columns:
            assert column in features_tier0.TIER0_AVAILABILITY, f"{column} が Tier 0 observable に無い"


def test_excluded_columns_are_real_and_actually_unused():
    used = {c for s in prereg.INFORMATION_SETS for c in s.source_columns}
    for column, reason in prereg.EXCLUDED_COLUMNS.items():
        assert column in features_tier0.TIER0_AVAILABILITY, f"{column} は存在しない列"
        assert column not in used, f"{column} は除外理由({reason})があるのに使われている"


def test_windows_inherit_the_final_oos_seal():
    assert prereg.CONFIRMATION_END == FINAL_OOS_START
    assert prereg.DEV_END <= prereg.CONFIRMATION_START
    for info_set in prereg.INFORMATION_SETS:
        assert info_set.dev_start < prereg.DEV_END
        assert prereg.CONFIRMATION_END <= FINAL_OOS_START


def test_dev_window_is_long_enough_for_the_fold_scheme():
    need_months = prereg.FOLD["initial_train_months"] + prereg.FOLD["test_block_months"]
    for info_set in prereg.INFORMATION_SETS:
        months = (prereg.DEV_END.year - info_set.dev_start.year) * 12 + (
            prereg.DEV_END.month - info_set.dev_start.month
        )
        assert months >= need_months, f"{info_set.id} の dev 窓が fold 構成に足りない"


def test_placebo_resolution_can_reach_the_holm_threshold():
    """Holm の最小閾値 alpha/m を、全数 placebo の p 解像度が下回れること。

    巡回シフト群は有限(|S| = 窓の暦日数 - 13)なので、最短の dev 窓でも
    到達できることを事前に確認しておく必要がある。
    """
    holm_min = 0.05 / prereg.FAMILY_SIZE
    stage1_min_p = 1 / (1 + prereg.PLACEBO["k_stage1"])
    assert stage1_min_p > holm_min, "第1段階だけで有意判定できてしまう設計になっている"

    for info_set in prereg.INFORMATION_SETS:
        window_days = (prereg.DEV_END - info_set.dev_start).days
        exhaustive = prereg.placebo_shift_count(window_days)
        assert exhaustive > 0
        assert 1 / (1 + exhaustive) < holm_min, (
            f"{info_set.id} の dev 窓({window_days}日)では全数 placebo でも Holm 閾値へ届かない"
        )


def test_prereg_module_does_not_touch_labels():
    tree = ast.parse(Path(prereg.__file__).read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
    assert not any("labels" in name for name in imported)


def test_doc_declares_the_same_targets_and_cost_basis():
    text = DOC.read_text(encoding="utf-8")
    for target in prereg.TARGETS:
        assert f"**{target}**" in text, f"{target} が文書に無い"
    assert str(int(prereg.COST_BASIS_BPS["round_trip"])) + "bps" in text
    assert str(int(prereg.COST_BASIS_BPS["stress"])) + "bps" in text
    assert prereg.PROTOCOL in text or "tier0_prereg" in text


@pytest.mark.skipif(not FEATURES.exists(), reason="Tier 0 features がこの環境に無い")
def test_source_columns_exist_in_the_actual_features_file():
    import polars as pl

    columns = set(pl.read_parquet(FEATURES, n_rows=1).columns)
    for column in prereg.A_BASE_COLUMNS:
        assert column in columns
    for info_set in prereg.INFORMATION_SETS:
        for column in info_set.source_columns:
            assert column in columns


def test_family_cannot_grow_without_breaking_placebo_resolution():
    """target をもう1つ足すと最短 dev 窓の cell が原理的に有意になれない(§5 の記録)。

    「なぜ Y5 を入れなかったのか」を後から誰かが再検討するとき、この計算を
    もう一度手でやらずに済むように固定しておく。
    """
    shortest = min(
        prereg.placebo_shift_count((prereg.DEV_END - s.dev_start).days)
        for s in prereg.INFORMATION_SETS
    )
    min_p = 1 / (1 + shortest)
    set_horizon_pairs = sum(len(s.horizons_bars) for s in prereg.INFORMATION_SETS)
    assert min_p < 0.05 / (set_horizon_pairs * len(prereg.TARGETS))  # 現行 27 は到達可能
    assert min_p > 0.05 / (set_horizon_pairs * (len(prereg.TARGETS) + 1))  # 36 は不可能


def test_interaction_baseline_factors_are_in_the_baseline():
    """交互作用項の baseline 因子が A に入っていること(strict nesting)。

    入っていないと「B だけが baseline の非線形関数を表現できる」抜け穴になり、
    dR2 > 0 を X の情報だと誤読しうる(leakage 監査の指摘)。
    """
    interactions = {
        c for s in prereg.INFORMATION_SETS for c in s.model_columns if "_x_" in c
    }
    assert interactions, "交互作用項が1つも無い(設計と食い違っている)"
    required_factors = {
        "signed_imb_x_norm_move_1": "norm_move_1",
        "dlog_oi_12_x_z20d_return_1h": "z20d_return_1h",
    }
    assert set(required_factors) == interactions
    for interaction, baseline_factor in required_factors.items():
        assert baseline_factor in prereg.A_COLUMNS, (
            f"{interaction} の baseline 因子 {baseline_factor} が A に無い"
        )


DEV_ARTIFACT = REPO / "experiments" / "phase7" / "tier0_screening_dev_v1.json"


@pytest.mark.skipif(not DEV_ARTIFACT.exists(), reason="screening 未実行")
def test_frozen_spec_was_not_edited_after_the_run():
    """実行後に凍結仕様を書き換えたら落ちる(改竄の構造的検出)。

    artifact に記録された tier0_prereg.py の sha256 と、現在のファイルを照合する。
    """
    import hashlib
    import json

    recorded = json.loads(DEV_ARTIFACT.read_text(encoding="utf-8")).get("prereg_sha256")
    assert recorded, "artifact に prereg_sha256 が記録されていない"
    current = hashlib.sha256(Path(prereg.__file__).read_bytes()).hexdigest()
    assert recorded == current, (
        "screening 実行後に tier0_prereg.py が変更されている(凍結違反)。"
        "変更が必要なら v2 として別 module を作ること。"
    )

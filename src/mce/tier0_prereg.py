"""Phase 7 Tier 0 screening の**事前登録された定数**(機械可読な凍結仕様)。

文書は docs/phase7/tier0_screening_preregistration_v1.md。本 module はその
「変えてはいけない値」をコードとして固定し、次タスクの screening 実装が
ここだけを参照するようにする(文書とコードの二重管理でズレるのを防ぐ)。

**この module は計算をしない。** ラベル生成・モデル学習・効果量計算を含まない。
`mce.labels` を import しないこと(事前登録の前に効果量を覗かないための構造的措置)。

凍結の意味: screening 実行後にここの値を書き換えない。変更が必要になった場合は
v2 として別 module / 別文書を作り、その後に開く窓でのみ適用する。
"""

from dataclasses import dataclass
from datetime import datetime, timezone

UTC = timezone.utc
PROTOCOL = "phase7_tier0_screening_v1"

# --- baseline information set A(§3)-------------------------------------------------

# 既存 features からそのまま使う列
A_BASE_COLUMNS = (
    "return_5m",
    "return_1h",
    "volume_ratio_20",
    "drift_20d",
    "realized_vol_20d",
)
# 実行時に導出する列(定義は §3)。設計上の要点:
#  - 短期ボラ(rv_12 / rv_48 / hl_range_z20d)を入れて baseline を意図的に強くする
#  - norm_move_1 / z20d_return_1h は X 側の交互作用項の baseline 因子。A に入れることで
#    「B だけが baseline の非線形関数を表現できる」抜け穴を塞ぐ(strict nesting)
#  - 時刻ハーモニクスは3次まで。X の参加量が日内プロファイルを拾うのを防ぐ
A_DERIVED_COLUMNS = (
    "rv_12",
    "rv_48",
    "hl_range_z20d",
    "log_volume_z20d",
    "norm_move_1",
    "z20d_return_1h",
    "tod_sin_1",
    "tod_cos_1",
    "tod_sin_2",
    "tod_cos_2",
    "tod_sin_3",
    "tod_cos_3",
    "is_weekend",
    "is_quarter_hour",
)
A_COLUMNS = A_BASE_COLUMNS + A_DERIVED_COLUMNS

# Z 変換(左閉右開・現在バーを含まない)
ZSCORE_WINDOW = "20d"
ZSCORE_MIN_VALID_BARS = 5184  # 20d = 5,760 バーの 90%


@dataclass(frozen=True)
class InformationSet:
    id: str
    source_columns: tuple[str, ...]  # features parquet に実在する列
    model_columns: tuple[str, ...]  # 実行時に導出する入力列
    horizons_bars: tuple[int, ...]
    dev_start: datetime
    mechanism: str


# --- 候補 information set X(§4)+ horizon(§6)+ 窓(§7)-----------------------------

DEV_START_DEFAULT = datetime(2021, 1, 1, tzinfo=UTC)
DEV_START_T0B2 = datetime(2023, 1, 1, tzinfo=UTC)  # 2022 の欠測ブロックを期間として除外
DEV_END = datetime(2025, 1, 1, tzinfo=UTC)
CONFIRMATION_START = datetime(2025, 1, 1, tzinfo=UTC)
CONFIRMATION_END = datetime(2026, 1, 1, tzinfo=UTC)  # = splits.FINAL_OOS_START(封印の継承)

INFORMATION_SETS: tuple[InformationSet, ...] = (
    InformationSet(
        id="T0-A",
        source_columns=("taker_buy_ratio", "trade_count", "avg_trade_notional"),
        model_columns=(
            "signed_imb",  # 2 * taker_buy_ratio - 1
            "z20d_signed_imb",
            "z20d_log_trade_count",
            "z20d_log_avg_trade_notional",
            "signed_imb_x_norm_move_1",  # 吸収 vs 継続は交互作用でしか表現できない
        ),
        horizons_bars=(1, 3, 12),
        dev_start=DEV_START_DEFAULT,
        mechanism="aggressive-flow continuation vs absorption / participation structure",
    ),
    InformationSet(
        id="T0-B1",
        source_columns=("open_interest",),
        model_columns=(
            "dlog_oi_12",
            "z20d_dlog_oi_12",
            "z20d_log_open_interest",
            "dlog_oi_12_x_z20d_return_1h",  # 価格 x 建玉変化 = build-up / short cover
        ),
        horizons_bars=(12, 48),
        dev_start=DEV_START_DEFAULT,
        mechanism="open-interest build-up and crowded unwind",
    ),
    InformationSet(
        id="T0-B2",
        source_columns=(
            "top_trader_position_ls_ratio",
            "global_account_ls_ratio",
            "taker_ls_vol_ratio",
        ),
        model_columns=(
            "z20d_log_top_trader_position_ls_ratio",
            "z20d_log_global_account_ls_ratio",
            "z20d_log_taker_ls_vol_ratio",
        ),
        horizons_bars=(12, 48),
        dev_start=DEV_START_T0B2,
        mechanism="crowd positioning skew and taker direction",
    ),
    InformationSet(
        id="T0-C",
        source_columns=("premium_close",),
        model_columns=("premium_close", "z20d_premium_close", "dprem_12"),
        horizons_bars=(12, 48),
        dev_start=DEV_START_DEFAULT,
        mechanism="perp/index premium = leverage demand and squeeze pressure",
    ),
)

# 明示的に除外した列と理由(§4)。実行時に「やっぱり入れる」ことを禁止する。
EXCLUDED_COLUMNS = {
    "taker_buy_quote_ratio": "taker_buy_ratio とほぼ共線",
    "open_interest_value": "open_interest x price とほぼ共線",
    "avg_trade_size": "avg_trade_notional とほぼ共線",
    "top_trader_account_ls_ratio": "top_trader_position_ls_ratio と同族(position 版を採用)",
    "premium_open": "premium_close と同一系列の start_of_bar 版(片方のみ使う)",
}

# --- target(§5)---------------------------------------------------------------------

TARGETS = {
    # entry は open[t+1]。同一バー close 執行は構造的に不可能(data contract §2)
    "Y1": "open[t+1+h] / open[t+1] - 1",
    "Y2": "log(1 + sqrt(sum_{i=1..h} (log open[t+1+i] - log open[t+i])^2))",
    "Y3": "min_{i=1..h}( low[t+i] / open[t+1] ) - 1",  # long 目線の MAE
}

# --- 標本・fold・推論(§8–§13)--------------------------------------------------------

# n_eff は「pooled OOS 評価行数 / h」。学習行では数えない(§8)
MIN_SAMPLE = {
    "dev": {"n_eff": 2000, "utc_days": 200},
    "confirmation": {"n_eff": 500, "utc_days": 100},
}
MONTHLY_COVERAGE_MIN = 0.95  # 月次被覆がこれ未満の暦月はブロックごと除外して報告

FOLD = {
    "scheme": "expanding",
    "initial_train_months": 12,
    "test_block_months": 3,
    "embargo_bars": 288,  # 1 日。purge(target 窓の重なり)に追加で適用
}

ESTIMATOR = {
    "model": "ridge",
    "alpha_grid": (0.1, 1.0, 10.0, 100.0, 1000.0),
    "alpha_selection": "inner purged walk-forward CV (3 splits) on the training set only",
    "standardization": "training-set mean/std applied to test",
}

PLACEBO = {
    # 主帰無: A で説明できる成分を残し、残差の時刻対応だけを壊す。
    # 素朴な X 全体のシフトは corr(X, A) まで壊すので反保守的(統計監査の fatal)。
    "primary": "Bp: X_p = A @ Gamma_hat + circular_day_shift(E), E = X - A @ Gamma_hat, "
    "Gamma_hat fitted on TRAIN rows only",
    "secondary_reported": "Bt: circular day-shift of the whole X block (anti-conservative; report only)",
    "scheme": "circular day-shift (A is never shifted)",
    "min_shift_days": 7,
    # シフト群は有限: S = {7 .. W_days-7}。K を無限に増やすことはできない。
    "k_stage1": 200,
    "k_stage2": "exhaustive over S (|S| = window_days - 13)",
    "stage2_rule": "stage-1 rank only: #{placebo >= observed} <= 5 (never the effect size)",
    "p_rule": "(1 + #{placebo >= observed}) / (1 + K)",
    # シフト後 null になった行は埋めない。placebo ごとに有効行を取り直し、
    # その行集合で A と B を両方とも再評価する(埋めると placebo が甘くなる)。
    "null_rule": "recompute S_d = S ∩ {shifted X non-null}; re-evaluate BOTH A and B on S_d",
    "window_rule": "shifts are circular WITHIN the stage window (dev placebos never read confirmation X)",
}


def placebo_shift_count(window_days: int) -> int:
    """その窓で作れる独立な巡回シフトの総数 |S| = window_days - 13。"""
    return window_days - 2 * PLACEBO["min_shift_days"] + 1

BOOTSTRAP = {"scheme": "day-cluster", "reps": 20000}
# 副次 p 値(判定には使わない。capacity 差を統制しないため)
RANDOMIZATION = {"scheme": "day-cluster Rademacher sign flip on daily dSSE", "reps": 20000}
SEEDS = {"dev": 20260817, "confirmation": 20260818}

# --- family と判定(§14–§17)----------------------------------------------------------

PRIMARY_METRIC = "dR2 = R2_oos(B) - R2_oos(A), pooled over test blocks, SST from training mean"
SECONDARY_METRICS = ("dIC_pearson", "dIC_spearman", "per_fold_dR2", "x_coefficient_signs")

MULTIPLE_TESTING = {
    "primary": "Holm, family-wise alpha = 0.05",
    "secondary_reported": "Benjamini-Hochberg, q = 0.10",
    "insufficient_sample_rule": "p = 1 (family size is never reduced)",
}

STABILITY = {
    "coefficient_sign_agreement_min": 0.75,  # fold の 75% 以上で符号一致
    "positive_fold_fraction_min": 0.75,
    "positive_calendar_years_min": 2,
    "leave_one_block_out_all_positive": True,
    "dIC_sign_must_match_dR2": True,
    "drop_most_influential_day_still_positive": True,
}

CONFIRMATION_RULE = {
    "sign_must_match_dev": True,
    "magnitude_min_fraction_of_dev": 0.5,
}

COST_BASIS_BPS = {"round_trip": 10.0, "stress": 15.0}  # OKX taker 基準(恒久ルール5)


def family() -> tuple[tuple[str, int, str], ...]:
    """事前登録された multiple-testing family の完全列挙 (set_id, horizon, target)。"""
    return tuple(
        (s.id, h, target)
        for s in INFORMATION_SETS
        for h in s.horizons_bars
        for target in TARGETS
    )


FAMILY_SIZE = len(family())  # = 27

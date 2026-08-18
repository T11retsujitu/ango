"""Phase 8.1 carry replication — 凍結パラメータの機械可読定義。

正は docs/phase8/carry_replication_protocol_v1.md(v1.8.2)。本モジュールは
その数値を実行器が読める形へ書き写したものであり、**結果を見た後に変更しない**。

凍結の作法(Phase 7 と同じ):
    このモジュールと事前登録文書の sha256 を experiments/phase8/carry_freeze.json
    へ記録し(v1.8.1 は carry_freeze_v1_8_1.json)、実行時の artifact と照合する。
    不一致は凍結違反である。

参照:
    A2 = arXiv 2212.06888v6 (Fundamentals of Perpetual Futures) — 再現アンカー
    A3 = arXiv 2605.05089 — 設計参照(意味論のみ採用。閾値は採用しない)
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import MappingProxyType
from typing import Final

UTC = timezone.utc

PROTOCOL: Final = "phase8_carry_replication_v1"
PROTOCOL_VERSION: Final = "v1.8.2"
FROZEN_AT: Final = "2026-08-17"

# ---------------------------------------------------------------------------
# 1. layer 境界(§13.1。決定ログ 2026-08-17 で H5 承認)
# ---------------------------------------------------------------------------
# FINAL_OOS_START は変更しない。splits.py の定数をそのまま参照する。
LAYER1_START: Final = datetime(2020, 1, 1, tzinfo=UTC)
LAYER1_END: Final = datetime(2025, 6, 1, tzinfo=UTC)  # = layer 2 の開始
LAYER2_END: Final = datetime(2026, 1, 1, tzinfo=UTC)  # = FINAL_OOS_START
LAYER3_START: Final = datetime(2026, 9, 1, tzinfo=UTC)  # = PHASE8_PROSPECTIVE_START

# Phase 8 の結果評価で決して読まない区間 [start, end)
CONTAMINATED_BAND: Final = (LAYER2_END, LAYER3_START)

# ---------------------------------------------------------------------------
# 2. ρ と no-arbitrage 帯(§4.2 / §6.3。A2 eq.8 と Table 3 caption)
# ---------------------------------------------------------------------------
KAPPA: Final = 1095.0  # 年間の8時間区間数。A2 の年率化係数

# ρ の金利項 r(H12。決定ログ 2026-08-17 で確定)
RATE_SOURCE: Final = "aave_variable_borrow_apr"
RATE_ASSETS: Final = ("USDT", "USDC", "DAI")
RATE_AGGREGATION: Final = "equal_weight_mean"
RATE_POINT_IN_TIME: Final = True  # signal_time で利用可能な観測のみ
R_PRIME: Final = 0.0  # Arm R は spot をショートしないため supply rate は使わない

# 事前登録した感応度(primary ではない)
RATE_SENSITIVITY_SOURCE: Final = "kenneth_french_daily_rf"

# --- v1.8.2 H15: Aave 金利市場の同定(§26。**未解決。実験をブロックする**)-----
# A2 は Aave の version / network / market / データ提供元を**一切書いていない**
# (全文中の言及は7箇所。確定できるのは 3 ステーブルコインの等加重平均・日次・
#  perp > spot 側では borrowing rate、という点のみ)。
# 金利データの起点 2020-01-08 は Aave V1 の Ethereum mainnet ローンチ日と一致するが、
# A2 のサンプルは V1 → V2 → V3 の3世代をまたぎ、接合方法は記載が無い。
# **提案 proxy は調査記録に1つだけ記載してある。本モジュールは採用していない。**
RATE_MARKET_IDENTITY_STATUS: Final = "unresolved_source_fidelity_limitation"
RATE_MARKET_VERSION: Final = None  # V1/V2/V3/V4 のいずれとも確定していない
RATE_MARKET_NETWORK: Final = None  # Ethereum mainnet と推定されるが未確定
RATE_MARKET_INSTANCE: Final = None  # Core / Prime / EtherFi 等が未確定

# --- v1.8.2 §25: 仕様の優先順位 -------------------------------------------
# 同一フィールドについて複数の記述があるとき、**後の凍結改訂節が先の記述を
# supersede する**。先行する矛盾記述は削除せず歴史的監査証跡として残す。
SPEC_PRECEDENCE: Final = "later_frozen_amendment_supersedes_earlier_text_for_the_same_field"


def arb_bound_upper(round_trip_cost: float) -> float:
    """A2 Table 3 caption: rho_u = kappa * log(1 + C)。"""
    import math

    return KAPPA * math.log1p(round_trip_cost)


def arb_bound_lower(round_trip_cost: float) -> float:
    """A2 Table 3 caption: rho_l = kappa * log(1 - C)。"""
    import math

    return KAPPA * math.log1p(-round_trip_cost)


# ---------------------------------------------------------------------------
# 3. コスト(§7.2)
# ---------------------------------------------------------------------------
# spot 側は公式 fee ページの live 読み取りで確定(VIP-0 taker 0.100%)。
SPOT_TAKER_BPS: Final = 10.0

# perp 側は FAQ の worked example 由来(probable)。H13 が未解決。
PERP_TAKER_BPS: Final = 5.0
COMMISSION_RATE_STATUS: Final = "pending_authenticated_read"
COMMISSION_RATE_ENDPOINT: Final = "GET /fapi/v1/commissionRate"

# 送金コスト。Binance 内の spot <-> futures 振替は無料・ほぼ即時(Y42)。
# 仮定を可視化するために 0 を明示的に置く。
TRANSFER_COST_BPS: Final = 0.0

COST_SCENARIOS: Final = MappingProxyType(
    {
        # name: (spot 片道 bps, perp 片道 bps)
        "maker_low": (1.0, 1.0),  # 参考のみ。maker fill を仮定しない
        "base_taker": (SPOT_TAKER_BPS, PERP_TAKER_BPS),  # primary
        "stress": (15.0, 10.0),  # 昇格ゲート(§14.1 d)
    }
)
PRIMARY_COST_SCENARIO: Final = "base_taker"
PROMOTION_GATE_SCENARIO: Final = "stress"

# Arm R の family を張るコスト階層(§15.1)。**実行後に増やさない。**
COST_TIERS: Final = ("maker_low", "base_taker", "stress")

# ---------------------------------------------------------------------------
# 4. 資本・数量・証拠金(§11)
# ---------------------------------------------------------------------------
CAPITAL_BASE_USDT: Final = 10_000.0  # C

# --- v1.8.1 G1: 予備資金(§24.1)-------------------------------------------
# R = (C − R)/(L + 1) すなわち「初期証拠金1トランシェ分」として導出した:
#     R(L + 2) = C  ⇒  R = C/(L + 2) = 10000/5 = 2000
# **leverage 感応度をまたいで 2000 に固定する**(L を変えても動かさない)。
MARGIN_RESERVE_USDT: Final = 2_000.0
POSITION_CAPITAL_USDT: Final = CAPITAL_BASE_USDT - MARGIN_RESERVE_USDT  # = 8000

LEVERAGE: Final = 3.0  # L(primary)
LEVERAGE_SENSITIVITY: Final = (1.0, 2.0, 3.0, 5.0)

# 取引所仕様(2026-08-17 に exchangeInfo から実測)
PERP_LOT_STEP: Final = 0.001  # BTC
PERP_MIN_NOTIONAL_USDT: Final = 50.0
SPOT_LOT_STEP: Final = 0.00001  # BTC
MAINT_MARGIN_RATE_TIER1: Final = 0.004  # notional 0 .. 300,000 USDT
MAINT_MARGIN_TIER1_CAP_USDT: Final = 300_000.0

# 追証規則(§11.4)。凡庸な決め打ちを1組だけ凍結する。
# **A3 の REL_REBAL_THRESHOLD は採用していない**(a3_source_review_v1 N1)。
MARGIN_TOPUP_TRIGGER: Final = 0.010  # 維持証拠金率がこれを割ったら追加
MARGIN_TOPUP_TARGET: Final = 0.020  # ここまで戻す

# --- v1.8.1 G2 / G3 / G4(§24.2 / §24.3 / §24.4)---------------------------
# 正負いずれの funding も先物ウォレット残高を動かす。
FUNDING_COUNTS_TOWARD_MARGIN: Final = True

# 清算後は巻き戻す。再ヘッジは実装しない。
POST_LIQUIDATION_RULE: Final = "unwind"

# イベント順序。TOPUP_TRIGGER は維持証拠金より上にあるので追証が先に到達する。
EVENT_ORDER: Final = "funding_then_margin_then_topup_then_liquidation"

# --- v1.8.1 H14: 清算コスト(§24.6。**未解決。実験をブロックする**)---------
# Liquidation Clearance Fee の存在と算定方式(rate × 名目)は公式 FAQ で確認したが、
# rate の数値は取得できなかった(brackets payload に fee 項目なし / trading-rules は
# JS 描画 / leverageBracket は 401)。**ゼロを黙って維持しない。**
LIQUIDATION_CLEARANCE_FEE_RATE: Final = None
LIQUIDATION_FEE_STATUS: Final = "pending_authoritative_read"

# 無リスク金利ハードル(§14.1 a。Y39)
RISK_FREE_SOURCE: Final = "aave_variable_borrow_apr_usdt"  # decision-time observable

# ---------------------------------------------------------------------------
# 5. funding(§5.2 / §8.1)
# ---------------------------------------------------------------------------
# calc_time は決済時刻(X4 で確定)。公開遅延シフトで未来参照を防ぐ。
DELTA_PUB_SECONDS: Final = 60  # funding_key = settlement_time + DELTA_PUB
MAX_STALE_SECONDS: Final = 9 * 3600  # 陳腐化ガード(未来参照とは無関係)

# 決済 s が trade に帰属する条件: entry_fill_time < s <= exit_fill_time (Y6)
FUNDING_BOUNDARY: Final = "entry_exclusive_exit_inclusive"

# 8 をハードコードしない(X5: cap/floor 到達で恒久的に 1h へ切り替わる)
READ_FUNDING_INTERVAL_PER_ROW: Final = True

# funding 公開遅延に対する頑健性(§14.1 g。Y30)
FUNDING_LAG_ROBUSTNESS_INTERVALS: Final = 1
FUNDING_LAG_SENSITIVITY_INTERVALS: Final = 2

# ---------------------------------------------------------------------------
# 6. arm と horizon(§6.3 / §10)
# ---------------------------------------------------------------------------
PRIMARY_ARM: Final = "R"  # A2 の long-spot-only 変種(Y54)
A2_VARIANT: Final = "long_spot_only"

# Arm E(記述的 robustness のみ。promotion 対象外・family 外)
ARM_E_HORIZONS_HOURS: Final = (8, 24, 72, 168, 336, 720, 1440, 2160)
GRID_EPOCH: Final = datetime(1970, 1, 1, tzinfo=UTC)
GRID_PHASE_OFFSETS: Final = 4  # h/4 刻み。平均して1統計量へ畳む

BASIS_MA_WINDOWS_HOURS: Final = (24, 168)  # §14.2。探索しない

# ---------------------------------------------------------------------------
# 7. 統計(§15 / §16)
# ---------------------------------------------------------------------------
FWER_ALPHA: Final = 0.05
MULTIPLE_TESTING: Final = "holm_bonferroni"
FAMILY_SIZE: Final = len(COST_TIERS)  # Arm R × コスト階層のみ

# 帰無分布(§15.0)。B3 randomization。
K_RANDOM: Final = 1000
RANDOM_SEED_BASE: Final = 20260901
EXPOSURE_GUARD: Final = 0.7  # これ以上の exposure では B3 を判定に使わない

# 標本下限(§16.3)。暦の算術で事前評価する。
MIN_TRADES: Final = MappingProxyType({"layer1": 30, "layer2": 30, "layer3": 20})

# CI(§16.2)。**新規コードである**(Phase 7 の _bootstrap_ci は再利用できない)。
BOOTSTRAP_REPS: Final = 10_000
BOOTSTRAP_UNIT: Final = "nonoverlapping_trade"
BOOTSTRAP_METHOD: Final = "stationary"
BOOTSTRAP_SEEDS: Final = MappingProxyType(
    {"layer1": 20260817, "layer2": 20260818, "layer3": 20260901}
)
DAY_CLUSTER_BLOCK_DAYS: Final = (1, 7, 30)  # 参考として併記

# ---------------------------------------------------------------------------
# 8. layer 1 replication gate(§18.1。データを見る前に凍結する)
# ---------------------------------------------------------------------------
# A2 §4.2 自身が「funding はスプレッドを完全には追随しない」と述べているため
# RHO_MIN は緩い(Y48)。
RHO_MIN: Final = 0.30  # corr(f(s), 直前8時間の basis_rel 平均)
MAD_MIN: Final = 0.10  # mean(|rho|) の下限(A2 は年率 0.60〜0.90 と報告)
FUNDING_POSITIVE_FRACTION_FLOOR: Final = 0.50  # 下回れば符号が体系的に逆

# ---------------------------------------------------------------------------
# 9. GO / NO-GO(§18.3)
# ---------------------------------------------------------------------------
# 比ではなく絶対基準(Y4 / Y12)。layer 2 の値は汚染較正なので基準にしない。
GO_REQUIRES: Final = (
    "mean_r_layer3_gt_risk_free",
    "sign_agrees_with_layer2",
    "positive_under_stress",
    "n_trades_ge_min",
    "funding_lag_robust",
    "b3_null_rejected_if_exposure_below_guard",
)

__all__ = [name for name in dir() if not name.startswith("_")]

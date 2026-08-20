"""パス・対象銘柄などの設定。環境変数 MCE_DATA_DIR でデータ置き場を変更できる。"""

import os
from pathlib import Path

DATA_DIR = Path(os.environ.get("MCE_DATA_DIR", "data"))
RAW_DIR = DATA_DIR / "raw"
NORMALIZED_DIR = DATA_DIR / "normalized"
FEATURES_DIR = DATA_DIR / "features"
LABELS_DIR = DATA_DIR / "labels"
MANIFESTS_DIR = DATA_DIR / "manifests"

# PoC の対象は BTC の USDT 無期限スワップのみ
INST_ID = "BTC-USDT-SWAP"
BAR = "5m"
BAR_MS = 5 * 60 * 1000
SOURCE = "okx"
MARKET_TYPE = "perp_linear"


def ohlcv_parquet(inst_id: str = INST_ID, bar: str = BAR) -> Path:
    return NORMALIZED_DIR / "ohlcv" / f"{SOURCE}_{inst_id}_{bar}.parquet"


def funding_parquet(inst_id: str = INST_ID) -> Path:
    return NORMALIZED_DIR / "funding_rate" / f"{SOURCE}_{inst_id}.parquet"


def open_interest_parquet(inst_id: str = INST_ID, period: str = BAR) -> Path:
    return NORMALIZED_DIR / "open_interest" / f"{SOURCE}_{inst_id}_{period}.parquet"


def features_parquet(inst_id: str = INST_ID, bar: str = BAR) -> Path:
    return FEATURES_DIR / f"{SOURCE}_{inst_id}_{bar}.parquet"


def labels_parquet(inst_id: str = INST_ID, bar: str = BAR) -> Path:
    return LABELS_DIR / f"{SOURCE}_{inst_id}_{bar}.parquet"


# --- Phase 7 Tier 0(Binance Vision。別 venue なので path も source も分ける)---

BINANCE_SOURCE = "binance"
BINANCE_SYMBOL = "BTCUSDT"


def binance_klines_parquet(symbol: str = BINANCE_SYMBOL, bar: str = BAR) -> Path:
    return NORMALIZED_DIR / BINANCE_SOURCE / f"klines_{symbol}_{bar}.parquet"


def binance_metrics_parquet(symbol: str = BINANCE_SYMBOL, bar: str = BAR) -> Path:
    return NORMALIZED_DIR / BINANCE_SOURCE / f"metrics_{symbol}_{bar}.parquet"


def binance_premium_index_parquet(symbol: str = BINANCE_SYMBOL, bar: str = BAR) -> Path:
    return NORMALIZED_DIR / BINANCE_SOURCE / f"premium_index_{symbol}_{bar}.parquet"


# --- Phase 8 の入力(F1 / F2)。perp / mark / spot を**別ファイル**に分ける ---


def binance_mark_price_parquet(symbol: str = BINANCE_SYMBOL, bar: str = BAR) -> Path:
    """F1: USD-M perp の mark 価格。**清算トリガーの入力であって約定価格ではない。**"""
    return NORMALIZED_DIR / BINANCE_SOURCE / f"mark_price_{symbol}_{bar}.parquet"


def binance_spot_klines_parquet(symbol: str = BINANCE_SYMBOL, bar: str = BAR) -> Path:
    """F2: spot の klines。**perp の klines とは別 market・別ファイル。**"""
    return NORMALIZED_DIR / BINANCE_SOURCE / f"spot_klines_{symbol}_{bar}.parquet"


def binance_funding_rate_parquet(symbol: str = BINANCE_SYMBOL) -> Path:
    """F4: USD-M perp の funding **決済イベント**。

    **バー系列ではないので `bar` を取らない。** Phase 7 由来の
    `funding_rate/binance_BTCUSDT.parquet`(OKX 側と同じ棚に置かれた別資産)
    とは**別ファイル**であり、そちらを上書きしない。
    """
    return NORMALIZED_DIR / BINANCE_SOURCE / f"funding_rate_{symbol}.parquet"


def binance_funding_rate_rest_parquet(symbol: str = BINANCE_SYMBOL) -> Path:
    """公式 REST の funding 決済(markPrice 付き)。

    **Vision 系列とは別ファイル。** canonical は Vision 側であり、
    REST はそれを**置換せず照合するだけ**である。
    """
    return NORMALIZED_DIR / BINANCE_SOURCE / f"funding_rate_rest_{symbol}.parquet"


def binance_funding_reconciliation_parquet(symbol: str = BINANCE_SYMBOL) -> Path:
    """Vision(canonical)× REST の一対一照合表。"""
    return NORMALIZED_DIR / BINANCE_SOURCE / f"funding_reconciliation_{symbol}.parquet"


def binance_features_parquet(symbol: str = BINANCE_SYMBOL, bar: str = BAR) -> Path:
    return FEATURES_DIR / f"{BINANCE_SOURCE}_{symbol}_{bar}.parquet"

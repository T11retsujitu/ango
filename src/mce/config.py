"""パス・対象銘柄などの設定。環境変数 MCE_DATA_DIR でデータ置き場を変更できる。"""

import os
from pathlib import Path

DATA_DIR = Path(os.environ.get("MCE_DATA_DIR", "data"))
RAW_DIR = DATA_DIR / "raw"
NORMALIZED_DIR = DATA_DIR / "normalized"
FEATURES_DIR = DATA_DIR / "features"

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

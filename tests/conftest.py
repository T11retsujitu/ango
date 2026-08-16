"""テスト共通ヘルパー: 合成 OHLCV の生成と行の取り出し。"""

import polars as pl
import pytest

from mce.normalize import normalize_candles

MIN_MS = 60_000


def make_ohlcv(bars: list[tuple[int, float, float]]) -> pl.DataFrame:
    """bars: (分, close, volume) のリストから OHLCV を作る(open=high=low=close)。"""
    rows = [
        [str(m * MIN_MS), str(c), str(c), str(c), str(c), "0", str(v), "0", "1"]
        for m, c, v in bars
    ]
    return normalize_candles(rows, "BTC-USDT-SWAP")


def make_ohlcv_oc(bars: list[tuple[int, float, float]]) -> pl.DataFrame:
    """bars: (分, open, close) のリスト。open と close を別々に指定する(執行テスト用)。"""
    rows = [
        [str(m * MIN_MS), str(o), str(max(o, c)), str(min(o, c)), str(c), "0", "1", "0", "1"]
        for m, o, c in bars
    ]
    return normalize_candles(rows, "BTC-USDT-SWAP")


def by_minute(df: pl.DataFrame, minute: int) -> dict:
    ms = minute * MIN_MS
    return df.filter(pl.col("ts").dt.epoch("ms") == ms).row(0, named=True)


@pytest.fixture
def ohlcv_factory():
    return make_ohlcv

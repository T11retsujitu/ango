"""OKX 固有のレスポンス形式 → 共通スキーマへの変換。

すべての ts はミリ秒 UNIX time を UTC の Datetime に変換して保持する。
欠損の補間は行わない(欠損検出は report 側)。
"""

import polars as pl

from mce.config import MARKET_TYPE, SOURCE

_TS = pl.Datetime(time_unit="ms", time_zone="UTC")


def _with_provenance(df: pl.DataFrame, symbol: str) -> pl.DataFrame:
    return df.with_columns(
        pl.lit(symbol).alias("symbol"),
        pl.lit(SOURCE).alias("source"),
        pl.lit(MARKET_TYPE).alias("market_type"),
    )


def normalize_candles(rows: list[list[str]], symbol: str) -> pl.DataFrame:
    """OKX candle 行 [ts,o,h,l,c,vol,volCcy,volCcyQuote,confirm] を共通形式へ。

    confirm != "1"(未確定足)は捨てる。volume は base 通貨(BTC)建て、
    volume_quote は quote 通貨(USDT)建て。
    """
    confirmed = [r for r in rows if r[8] == "1"]
    if not confirmed:
        return pl.DataFrame()
    df = pl.DataFrame(
        {
            "ts": [int(r[0]) for r in confirmed],
            "open": [float(r[1]) for r in confirmed],
            "high": [float(r[2]) for r in confirmed],
            "low": [float(r[3]) for r in confirmed],
            "close": [float(r[4]) for r in confirmed],
            "volume": [float(r[6]) for r in confirmed],
            "volume_quote": [float(r[7]) for r in confirmed],
        }
    ).with_columns(pl.col("ts").cast(_TS))
    return _with_provenance(df, symbol)


def normalize_funding(rows: list[dict], symbol: str) -> pl.DataFrame:
    """OKX funding-rate-history のレコードを共通形式へ。"""
    if not rows:
        return pl.DataFrame()
    df = pl.DataFrame(
        {
            "ts": [int(r["fundingTime"]) for r in rows],
            "funding_rate": [float(r["fundingRate"]) for r in rows],
        }
    ).with_columns(pl.col("ts").cast(_TS))
    return _with_provenance(df, symbol)


def normalize_open_interest(rows: list[list[str]], symbol: str) -> pl.DataFrame:
    """OKX open-interest-history 行 [ts, oi, oiCcy, oiUsd] を共通形式へ。

    oi は base 通貨(BTC)建て、oi_usd は USD 建て。
    """
    if not rows:
        return pl.DataFrame()
    df = pl.DataFrame(
        {
            "ts": [int(r[0]) for r in rows],
            "oi": [float(r[2]) for r in rows],
            "oi_usd": [float(r[3]) for r in rows],
        }
    ).with_columns(pl.col("ts").cast(_TS))
    return _with_provenance(df, symbol)

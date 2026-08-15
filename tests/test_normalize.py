from datetime import datetime, timezone

from mce.normalize import normalize_candles, normalize_funding, normalize_open_interest

CANDLE = ["1609545300000", "29299.4", "29333.6", "29299.4", "29333.6", "1801", "18.01", "528058.816", "1"]
UNCONFIRMED = ["1609545600000", "29300", "29310", "29290", "29305", "100", "1.0", "29300.0", "0"]


def test_normalize_candles_schema_and_utc():
    df = normalize_candles([CANDLE], "BTC-USDT-SWAP")
    assert df.height == 1
    row = df.row(0, named=True)
    assert row["ts"] == datetime(2021, 1, 1, 23, 55, tzinfo=timezone.utc)
    assert row["open"] == 29299.4
    assert row["volume"] == 18.01  # base 通貨 (BTC) 建て
    assert row["symbol"] == "BTC-USDT-SWAP"
    assert row["source"] == "okx"
    assert row["market_type"] == "perp_linear"


def test_normalize_candles_drops_unconfirmed():
    df = normalize_candles([CANDLE, UNCONFIRMED], "BTC-USDT-SWAP")
    assert df.height == 1


def test_normalize_funding():
    rows = [{"fundingTime": "1786780800000", "fundingRate": "0.0000765914290582"}]
    df = normalize_funding(rows, "BTC-USDT-SWAP")
    assert df.height == 1
    assert abs(df.row(0, named=True)["funding_rate"] - 0.0000765914290582) < 1e-15


def test_normalize_open_interest():
    rows = [["1786801500000", "3393387.24", "33933.8724", "2137867895.07"]]
    df = normalize_open_interest(rows, "BTC-USDT-SWAP")
    assert df.row(0, named=True)["oi"] == 33933.8724  # BTC 建て


def test_empty_input():
    assert normalize_candles([], "X").is_empty()
    assert normalize_funding([], "X").is_empty()
    assert normalize_open_interest([], "X").is_empty()

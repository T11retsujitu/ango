import polars as pl

from mce.features import build_features
from mce.normalize import normalize_candles, normalize_funding

MIN_MS = 60_000


def make_ohlcv(bars: list[tuple[int, float, float]]) -> pl.DataFrame:
    """bars: (分, close, volume) のリストから OHLCV を作る。"""
    rows = [
        [str(m * MIN_MS), str(c), str(c), str(c), str(c), "0", str(v), "0", "1"]
        for m, c, v in bars
    ]
    return normalize_candles(rows, "BTC-USDT-SWAP")


def by_minute(df: pl.DataFrame, minute: int) -> dict:
    ms = minute * MIN_MS
    return df.filter(pl.col("ts").dt.epoch("ms") == ms).row(0, named=True)


def test_returns_and_gap_safety():
    # 15分のバーが欠損している
    df = build_features(make_ohlcv([(0, 100, 1), (5, 110, 1), (10, 121, 1), (20, 133.1, 1)]))
    assert abs(by_minute(df, 5)["return_5m"] - 0.10) < 1e-12
    assert by_minute(df, 20)["return_5m"] is None  # 15分が無いので null
    assert by_minute(df, 0)["return_5m"] is None  # 先頭も null
    # 前方リターン: 10分のバーの5分後(15分)は欠損 → null、5分バーの5分後は有効
    assert abs(by_minute(df, 5)["fwd_return_5m"] - 0.10) < 1e-12
    assert by_minute(df, 10)["fwd_return_5m"] is None


def test_return_1h():
    df = build_features(make_ohlcv([(0, 100, 1), (60, 105, 1)]))
    assert abs(by_minute(df, 60)["return_1h"] - 0.05) < 1e-12


def test_volume_ratio_20_requires_full_window():
    # 0..100分 = 21本連続。最後のバーだけが直近20本の完全な窓を持つ
    bars = [(5 * i, 100.0, 1.0) for i in range(20)] + [(100, 100.0, 3.0)]
    df = build_features(make_ohlcv(bars))
    assert abs(by_minute(df, 100)["volume_ratio_20"] - 3.0) < 1e-12
    assert by_minute(df, 95)["volume_ratio_20"] is None  # 窓が19本しかない


def test_funding_asof_with_tolerance():
    ohlcv = make_ohlcv([(5, 100, 1), (600, 100, 1)])  # 600分 = funding から10時間後
    funding = normalize_funding([{"fundingTime": "0", "fundingRate": "0.0001"}], "BTC-USDT-SWAP")
    df = build_features(ohlcv, funding=funding)
    assert abs(by_minute(df, 5)["funding_rate"] - 0.0001) < 1e-15
    assert by_minute(df, 600)["funding_rate"] is None  # 9時間超は無効


def test_missing_funding_and_oi_columns_exist():
    df = build_features(make_ohlcv([(0, 100, 1)]))
    assert df["funding_rate"].null_count() == 1
    assert df["oi"].null_count() == 1


def test_drift_20d():
    # 20日 = 28,800分。基準バーがあるときだけ値を持つ
    df = build_features(make_ohlcv([(0, 100, 1), (5, 100, 1), (28_800, 110, 1), (28_805, 121, 1)]))
    assert abs(by_minute(df, 28_800)["drift_20d"] - 0.10) < 1e-12
    assert abs(by_minute(df, 28_805)["drift_20d"] - 0.21) < 1e-12
    assert by_minute(df, 0)["drift_20d"] is None  # 20日前のバーが無い


def test_realized_vol_20d_requires_coverage():
    # 0..28,795分 = 5,760本連続 + 28,800分の1本。最後のバーの窓は有効 return_5m
    # 5,759本(先頭バーのみ null)で 90% 基準(5,184本)を満たす。
    closes = [100.0 if i % 2 == 0 else 101.0 for i in range(5760)]
    bars = [(5 * i, closes[i], 1.0) for i in range(5760)] + [(28_800, 100.0, 1.0)]
    df = build_features(make_ohlcv(bars))
    vol = by_minute(df, 28_800)["realized_vol_20d"]
    assert vol is not None
    assert 0.005 < vol < 0.015  # ±1%交互リターンの標準偏差 ≈ 0.01
    # 窓が 1,000 本しかないバーでは null
    assert by_minute(df, 5_000)["realized_vol_20d"] is None

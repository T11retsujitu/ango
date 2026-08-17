"""Tier 0 ラベル(Y1/Y2/Y3)の定義と入力ガード。"""

from datetime import datetime, timedelta, timezone

import numpy as np
import polars as pl
import pytest

from mce import labels_tier0 as L

UTC = timezone.utc
START = datetime(2024, 1, 1, tzinfo=UTC)


def _ohlcv(n: int = 60, gap_at: int | None = None) -> pl.DataFrame:
    minutes = [5 * i for i in range(n)]
    if gap_at is not None:
        minutes = [m for i, m in enumerate(minutes) if i != gap_at]
    ts = [START + timedelta(minutes=m) for m in minutes]
    rng = np.random.default_rng(0)
    opens = 100 + np.cumsum(rng.normal(0, 0.5, len(ts)))
    return pl.DataFrame(
        {
            "ts": ts,
            "open": opens,
            "high": opens + 1.0,
            "low": opens - 1.0,
            "close": opens + 0.1,
            "volume": np.full(len(ts), 10.0),
        }
    ).with_columns(
        pl.col("ts").dt.cast_time_unit("ms"),
        pl.lit("BTCUSDT").alias("symbol"),
        pl.lit("binance").alias("source"),
        pl.lit("perp_linear").alias("market_type"),
    )


def test_y1_y2_y3_match_their_definitions():
    df = _ohlcv()
    labels = L.build_labels(df, horizons=(3,))
    o = df["open"].to_numpy()
    low = df["low"].to_numpy()
    i = 10
    y1 = o[i + 1 + 3] / o[i + 1] - 1
    uo = np.log(o[i + 2 : i + 5]) - np.log(o[i + 1 : i + 4])
    y2 = np.log(1 + np.sqrt((uo**2).sum()))
    y3 = low[i + 1 : i + 4].min() / o[i + 1] - 1
    assert labels["fwd_y1_h3"][i] == pytest.approx(y1, rel=1e-12)
    assert labels["fwd_y2_h3"][i] == pytest.approx(y2, rel=1e-12)
    assert labels["fwd_y3_h3"][i] == pytest.approx(y3, rel=1e-12)


def test_all_targets_share_the_open_t_plus_1_anchor():
    """Y1/Y2/Y3 はすべて open[t+1] を起点とする同一区間(事前登録 §5)。"""
    df = _ohlcv()
    labels = L.build_labels(df, horizons=(1,))
    o = df["open"].to_numpy()
    i = 5
    # h=1 では Y2 の区間は open[t+1] -> open[t+2] の1本だけ
    expected = np.log(1 + abs(np.log(o[i + 2] / o[i + 1])))
    assert labels["fwd_y2_h1"][i] == pytest.approx(expected, rel=1e-12)


def test_gap_makes_labels_null_instead_of_spanning_it():
    """欠損バーを跨いだ値を作らない(ts 完全一致 join + 完全窓)。"""
    df = _ohlcv(gap_at=12)
    labels = L.build_labels(df, horizons=(3,)).sort("ts")
    # ギャップ直前の数行は h=3 の窓が埋まらないので null になる
    near_gap = labels.filter(
        (pl.col("ts") >= START + timedelta(minutes=5 * 9))
        & (pl.col("ts") < START + timedelta(minutes=5 * 12))
    )
    assert near_gap["fwd_y2_h3"].null_count() > 0
    assert near_gap["fwd_y3_h3"].null_count() > 0


def test_partial_window_is_null_at_the_tail():
    df = _ohlcv(n=20)
    labels = L.build_labels(df, horizons=(12,)).sort("ts")
    assert labels["fwd_y1_h12"].tail(12).null_count() == 12


def test_guard_rejects_forward_looking_input():
    df = _ohlcv().with_columns(pl.lit(0.0).alias("fwd_return_5m"))
    with pytest.raises(L.LabelGuardError, match="先読み列"):
        L.assert_input_is_sealed_and_observable(df, __import__("pathlib").Path("data/features/binance_x.parquet"))


def test_guard_rejects_sealed_rows():
    from pathlib import Path

    df = _ohlcv().with_columns(
        pl.when(pl.col("ts") == START)
        .then(pl.lit(datetime(2026, 1, 1, tzinfo=UTC)).dt.cast_time_unit("ms"))
        .otherwise(pl.col("ts"))
        .alias("ts")
    )
    with pytest.raises(L.LabelGuardError, match="封印期間"):
        L.assert_input_is_sealed_and_observable(df, Path("data/features/binance_x.parquet"))


def test_guard_rejects_unexpected_input_path():
    from pathlib import Path

    with pytest.raises(L.LabelGuardError, match="許可されていない入力"):
        L.assert_input_is_sealed_and_observable(_ohlcv(), Path("data/labels/whatever.parquet"))


def test_horizons_come_from_the_frozen_prereg():
    from mce import tier0_prereg as prereg

    assert set(L.HORIZONS) == {h for s in prereg.INFORMATION_SETS for h in s.horizons_bars}

"""DSL op 実装のテスト(欠損バー安全性・窓完全性・future mutation)。"""

import polars as pl
import pytest

from conftest import by_minute, make_ohlcv
from mce.dsl import ops
from mce.features import build_features
from mce.normalize import normalize_candles


def _ohlcv_rows(rows):
    """rows: (分, o, h, l, c, volume)"""
    raw = [[str(m * 60_000), str(o), str(h), str(l), str(c), "0", str(v), "0", "1"] for m, o, h, l, c, v in rows]
    return normalize_candles(raw, "X")


def test_return_gap_safety():
    df = make_ohlcv([(0, 100, 1), (5, 110, 1), (10, 121, 1), (20, 133.1, 1)])
    r = ops.op_return(df, 1)
    assert abs(r[1] - 0.10) < 1e-12
    assert r[3] is None  # 15分バー欠損 → null(行シフトなら誤値になる)
    r2 = ops.op_return(df, 2)
    assert abs(r2[2] - 0.21) < 1e-12


def test_trend_and_ma_slope():
    df = make_ohlcv([(0, 100, 1), (5, 110, 1), (10, 120, 1)])
    trend = ops.op_trend(df, 2)
    assert trend[0] is None  # 窓不足
    assert abs(trend[2] - (120 / 115 - 1)) < 1e-12
    slope = ops.op_ma_slope(df, 2)
    assert abs(slope[2] - (115 / 105 - 1)) < 1e-12
    assert slope[1] is None  # 前バーの SMA が無い


def test_volatility_requires_full_window():
    df = make_ohlcv([(0, 100, 1), (5, 110, 1), (10, 121, 1)])
    vol = ops.op_volatility(df, 2)
    assert vol[1] is None  # 有効リターンが1本しかない
    assert vol[2] == pytest.approx(0.0)  # リターン 0.10, 0.10 → std 0


def test_range():
    df = _ohlcv_rows([(0, 100, 105, 95, 100, 1), (5, 100, 110, 90, 100, 1)])
    rng = ops.op_range(df, 2)
    assert abs(rng[1] - (110 - 90) / 100) < 1e-12
    assert rng[0] is None


def test_volume_z_excludes_current_bar():
    df = make_ohlcv([(0, 100, 1.0), (5, 100, 2.0), (10, 100, 3.0), (15, 100, 6.0)])
    z = ops.op_volume_z(df, 3)
    assert abs(z[3] - (6 - 2) / 1.0) < 1e-12  # 直近3本(現在含まず)mean=2, std=1
    assert z[2] is None  # 窓不足


def test_volume_z_zero_std_is_null():
    df = make_ohlcv([(0, 100, 1.0), (5, 100, 1.0), (10, 100, 1.0), (15, 100, 5.0)])
    z = ops.op_volume_z(df, 3)
    assert z[3] is None


def test_zscore_includes_current():
    base = pl.Series([1.0, 2.0, 3.0])
    df = make_ohlcv([(0, 100, 1), (5, 100, 1), (10, 100, 1)])
    z = ops.op_zscore(df, base, 3)
    assert abs(z[2] - (3 - 2) / 1.0) < 1e-12
    assert z[1] is None


def test_clock_is():
    df = make_ohlcv([(0, 100, 1), (25, 100, 1), (30, 100, 1)])
    m15 = ops.op_clock_is(df, 15, 0)
    assert m15.to_list() == [True, False, True]
    m60 = ops.op_clock_is(df, 60, 25)
    assert m60.to_list() == [False, True, False]


def test_holds_for_requires_consecutive_bars():
    df = make_ohlcv([(0, 100, 1), (5, 100, 1), (15, 100, 1)])  # 10分欠損
    cond = pl.Series([True, True, True])
    h = ops.op_holds_for(df, cond, 2)
    assert h[1] == True  # noqa: E712 (0,5分の2本連続)
    assert h[2] is None  # 欠損バーを跨ぐ → 連続と見なさない


def test_all_ops_pass_future_mutation():
    """未来バーを書き換えても、過去行の全 op 出力が不変(observable保証)。"""
    past = [(5 * i, 100.0 + i, 1.0 + 0.1 * i) for i in range(30)]
    fut_a = [(5 * i, 100.0 + i, 1.0) for i in range(30, 50)]
    fut_b = [(5 * i, 300.0 - i, 7.0) for i in range(30, 50)]
    dfa = build_features(make_ohlcv(past + fut_a))
    dfb = build_features(make_ohlcv(past + fut_b))
    cutoff = 29 * 5

    for name, call in [
        ("return", lambda d: ops.op_return(d, 3)),
        ("trend", lambda d: ops.op_trend(d, 5)),
        ("volatility", lambda d: ops.op_volatility(d, 5)),
        ("range", lambda d: ops.op_range(d, 5)),
        ("volume_z", lambda d: ops.op_volume_z(d, 5)),
        ("ma_slope", lambda d: ops.op_ma_slope(d, 5)),
        ("zscore", lambda d: ops.op_zscore(d, ops.op_return(d, 1), 5)),
        ("holds_for", lambda d: ops.op_holds_for(d, ops.op_greater(ops.op_return(d, 1), 0), 3)),
    ]:
        a = pl.DataFrame({"ts": dfa["ts"], "v": call(dfa)}).filter(pl.col("ts").dt.epoch("ms") <= cutoff * 60_000)
        b = pl.DataFrame({"ts": dfb["ts"], "v": call(dfb)}).filter(pl.col("ts").dt.epoch("ms") <= cutoff * 60_000)
        assert a.equals(b), f"op {name} が未来バー改変の影響を受けた"

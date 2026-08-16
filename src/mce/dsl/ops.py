"""DSL の feature / transform / bool 演算の決定的実装。

凍結済み Data Contract に準拠する:
- 全 op は「バー t の close 時点までに確定した値」のみ使用(負の lag は存在しない)
- window は ts 基準で本数完全性を要求し、欠損バー・不足は null
- リターンは行シフトでなく ts 一致 join
仕様: docs/phase2/dsl_spec.md §3
"""

import polars as pl

BAR_MINUTES = 5


def _ts_shifted(df: pl.DataFrame, values: pl.Series, bars: int) -> pl.Series:
    """values(df の行に整列)の「bars 本前」の値。ts 一致 join(欠損は null)。"""
    src = pl.DataFrame({"ts": df["ts"], "_v": values}).select(
        (pl.col("ts") + pl.duration(minutes=BAR_MINUTES * bars)).alias("ts"),
        pl.col("_v"),
    )
    return df.select("ts").join(src, on="ts", how="left")["_v"]


def _rolling(df: pl.DataFrame, x: pl.Series, window: int, stat: str, closed: str = "right") -> pl.Series:
    """直近 window 本の rolling 統計。窓内の有効値がちょうど window 本のときのみ値を持つ。

    closed="right": (ts−5w, ts] = 現在バーを含む直近 w 本
    closed="left" : [ts−5w, ts) = 現在バーを含まない直近 w 本
    """
    tmp = pl.DataFrame({"ts": df["ts"], "_x": x}).with_columns(
        pl.col("_x").is_not_null().cast(pl.Int32).alias("_ok")
    )
    win = f"{BAR_MINUTES * window}m"
    stat_expr = getattr(pl.col("_x"), f"rolling_{stat}_by")("ts", window_size=win, closed=closed)
    out = tmp.with_columns(
        stat_expr.alias("_s"),
        pl.col("_ok").rolling_sum_by("ts", window_size=win, closed=closed).alias("_n"),
    )
    return out.select(pl.when(pl.col("_n") == window).then(pl.col("_s")).alias("_r"))["_r"]


def _safe_div(num: pl.Series, den: pl.Series) -> pl.Series:
    tmp = pl.DataFrame({"n": num, "d": den})
    return tmp.select(pl.when(pl.col("d") > 0).then(pl.col("n") / pl.col("d")).alias("_r"))["_r"]


# ---- feature ops(num の葉)----

def op_return(df: pl.DataFrame, window: int) -> pl.Series:
    prev = _ts_shifted(df, df["close"], window)
    return df["close"] / prev - 1


def op_trend(df: pl.DataFrame, window: int) -> pl.Series:
    sma = _rolling(df, df["close"], window, "mean")
    return df["close"] / sma - 1


def op_volatility(df: pl.DataFrame, window: int) -> pl.Series:
    return _rolling(df, op_return(df, 1), window, "std")


def op_range(df: pl.DataFrame, window: int) -> pl.Series:
    hi = _rolling(df, df["high"], window, "max")
    lo = _rolling(df, df["low"], window, "min")
    return (hi - lo) / df["close"]


def op_volume_z(df: pl.DataFrame, window: int) -> pl.Series:
    m = _rolling(df, df["volume"], window, "mean", closed="left")
    s = _rolling(df, df["volume"], window, "std", closed="left")
    return _safe_div(df["volume"] - m, s)


def op_ma_slope(df: pl.DataFrame, window: int) -> pl.Series:
    sma = _rolling(df, df["close"], window, "mean")
    prev = _ts_shifted(df, sma, 1)
    return sma / prev - 1


# ---- transform ops(num → num)----

def op_rolling_mean(df: pl.DataFrame, x: pl.Series, window: int) -> pl.Series:
    return _rolling(df, x, window, "mean")


def op_rolling_std(df: pl.DataFrame, x: pl.Series, window: int) -> pl.Series:
    return _rolling(df, x, window, "std")


def op_zscore(df: pl.DataFrame, x: pl.Series, window: int) -> pl.Series:
    m = _rolling(df, x, window, "mean")
    s = _rolling(df, x, window, "std")
    return _safe_div(x - m, s)


# ---- bool ops ----

def op_greater(x: pl.Series, threshold: float) -> pl.Series:
    return x > threshold


def op_less(x: pl.Series, threshold: float) -> pl.Series:
    return x < threshold


def op_and(a: pl.Series, b: pl.Series) -> pl.Series:
    return a & b


def op_or(a: pl.Series, b: pl.Series) -> pl.Series:
    return a | b


def op_not(a: pl.Series) -> pl.Series:
    return ~a


def op_clock_is(df: pl.DataFrame, period: int, phase: int) -> pl.Series:
    return df["ts"].dt.minute() % period == phase


def op_holds_for(df: pl.DataFrame, a: pl.Series, bars: int) -> pl.Series:
    """条件が直近 bars 本連続で成立(null や欠損バーを跨ぐ場合は null → flat 扱い)。"""
    x = a.cast(pl.Int32)
    s = _rolling(df, x, bars, "sum")
    return s == bars

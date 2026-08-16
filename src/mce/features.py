"""normalized Parquet から observable features Parquet を生成する。

    python -m mce.features

このモジュールが生成する列は「バー t の close 時点までに観測可能な値」だけである。
先読みラベル(fwd_return_*)は mce.labels が data/labels/ へ別生成し、backtest
loader は fwd_ 列を拒否する。時刻規約・availability の定義は
docs/data_contract.md を参照。

normalized からの決定的な全再生成なので、何度実行しても結果は同じ(冪等)。

ギャップの扱い:
- リターン系は行シフトではなく「ts の完全一致 join」で計算する。
  基準となる過去のバーが欠損している場合、その特徴量は null になる
  (欠損をまたいで誤った期間のリターンを作らない)。
- volume_ratio_20 は直近20本(現在バーを含まない)が揃っているときのみ値を持つ。
- funding_rate は as-of join(その時点で確定している直近値)。9時間より古い値は
  使わない(= funding データが無い期間は null)。
"""

import polars as pl

from mce import config

# メタ列(availability 宣言の対象外)
META_COLUMNS = {"ts", "symbol", "source", "market_type"}

# observable 列 → availability 種別(docs/data_contract.md §3)。
# start_of_bar: バー開始時刻 ts で確定 / close_of_bar: ts + 5m(close)で確定。
# 未宣言の列は backtest loader が拒否する。
AVAILABILITY: dict[str, str] = {
    "open": "start_of_bar",
    "high": "close_of_bar",
    "low": "close_of_bar",
    "close": "close_of_bar",
    "volume": "close_of_bar",
    "volume_quote": "close_of_bar",
    "return_5m": "close_of_bar",
    "return_1h": "close_of_bar",
    "volume_ratio_20": "close_of_bar",
    "drift_20d": "close_of_bar",
    "realized_vol_20d": "start_of_bar",  # 窓 [ts-20d, ts) は現在バーを含まない
    "funding_rate": "start_of_bar",  # as-of backward: ts 以前に決済確定した値のみ
    "oi": "start_of_bar",
    "oi_usd": "start_of_bar",
    "minute_mod_15": "start_of_bar",
    "minute_mod_60": "start_of_bar",
    "hour_utc": "start_of_bar",
    "weekday_utc": "start_of_bar",
}


def close_at_offset(df: pl.DataFrame, offset_min: int, name: str) -> pl.DataFrame:
    """ts + offset の位置に close を並べたテーブル。join すると
    「offset_min 分前(負なら後)の close」列になる。"""
    return df.select(
        (pl.col("ts") + pl.duration(minutes=offset_min)).alias("ts"),
        pl.col("close").alias(name),
    )


def build_features(
    ohlcv: pl.DataFrame,
    funding: pl.DataFrame | None = None,
    oi: pl.DataFrame | None = None,
) -> pl.DataFrame:
    df = ohlcv.sort("ts")

    # 過去の close を ts 一致 join で取り付ける(ギャップは自動的に null)
    for offset_min, name in [(5, "close_5m_ago"), (60, "close_1h_ago")]:
        df = df.join(close_at_offset(ohlcv, offset_min, name), on="ts", how="left")

    # 直近20本(現在バー含まず)の出来高平均。window [ts-100m, ts) に
    # ちょうど20本あるときだけ有効。
    df = df.with_columns(pl.lit(1, dtype=pl.Int32).alias("_one")).with_columns(
        pl.col("volume").rolling_sum_by("ts", window_size="100m", closed="left").alias("_vol_sum"),
        pl.col("_one").rolling_sum_by("ts", window_size="100m", closed="left").alias("_vol_n"),
    )

    df = df.with_columns(
        (pl.col("close") / pl.col("close_5m_ago") - 1).alias("return_5m"),
        (pl.col("close") / pl.col("close_1h_ago") - 1).alias("return_1h"),
        pl.when(pl.col("_vol_n") == 20)
        .then(pl.col("volume") / (pl.col("_vol_sum") / 20))
        .alias("volume_ratio_20"),
    ).drop("_one", "_vol_sum", "_vol_n", "close_5m_ago", "close_1h_ago")

    # 20日ローリング特徴量(レジーム分類用)。drift_20d は ts 一致 join なので
    # 基準バー欠損なら null。realized_vol_20d は窓 [ts-20d, ts) の return_5m
    # 標準偏差で、有効本数が窓の90%(= 5,184本)未満なら null。
    df = (
        df.join(close_at_offset(ohlcv, 20 * 24 * 60, "close_20d_ago"), on="ts", how="left")
        .with_columns(pl.col("return_5m").is_not_null().cast(pl.Int32).alias("_ret_ok"))
        .with_columns(
            pl.col("return_5m").rolling_std_by("ts", window_size="20d", closed="left").alias("_vol20"),
            pl.col("_ret_ok").rolling_sum_by("ts", window_size="20d", closed="left").alias("_vol20_n"),
        )
        .with_columns(
            (pl.col("close") / pl.col("close_20d_ago") - 1).alias("drift_20d"),
            pl.when(pl.col("_vol20_n") >= 5184).then(pl.col("_vol20")).alias("realized_vol_20d"),
        )
        .drop("close_20d_ago", "_ret_ok", "_vol20", "_vol20_n")
    )

    # clock 系列(バー開始時刻の壁時計 UTC。docs/data_contract.md §7)
    df = df.with_columns(
        (pl.col("ts").dt.minute() % 15).cast(pl.Int8).alias("minute_mod_15"),
        pl.col("ts").dt.minute().cast(pl.Int8).alias("minute_mod_60"),
        pl.col("ts").dt.hour().cast(pl.Int8).alias("hour_utc"),
        (pl.col("ts").dt.weekday() - 1).cast(pl.Int8).alias("weekday_utc"),
    )

    if funding is not None and not funding.is_empty():
        df = df.join_asof(
            funding.sort("ts").select("ts", "funding_rate"),
            on="ts",
            strategy="backward",
            tolerance="9h",
        )
    else:
        df = df.with_columns(pl.lit(None, dtype=pl.Float64).alias("funding_rate"))

    if oi is not None and not oi.is_empty():
        df = df.join(oi.select("ts", "oi", "oi_usd"), on="ts", how="left")
    else:
        df = df.with_columns(
            pl.lit(None, dtype=pl.Float64).alias("oi"),
            pl.lit(None, dtype=pl.Float64).alias("oi_usd"),
        )

    _assert_observable(df)
    return df


def _assert_observable(df: pl.DataFrame) -> None:
    """出力が契約に適合しているか(fwd_ 無し・全列 availability 宣言済み)。"""
    for c in df.columns:
        if c.startswith("fwd_"):
            raise AssertionError(f"label 列 {c} が observable features に混入している")
        if c not in META_COLUMNS and c not in AVAILABILITY:
            raise AssertionError(f"列 {c} の availability が未宣言(mce.features.AVAILABILITY へ追加すること)")


def main() -> None:
    ohlcv_path = config.ohlcv_parquet()
    if not ohlcv_path.exists():
        raise SystemExit("normalized の OHLCV がありません。先に `python -m mce.ingest ohlcv` を実行してください。")
    ohlcv = pl.read_parquet(ohlcv_path)
    funding_path = config.funding_parquet()
    oi_path = config.open_interest_parquet()
    funding = pl.read_parquet(funding_path) if funding_path.exists() else None
    oi = pl.read_parquet(oi_path) if oi_path.exists() else None

    df = build_features(ohlcv, funding, oi)

    out = config.features_parquet()
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".parquet.tmp")
    df.write_parquet(tmp)
    tmp.replace(out)

    print(f"features: {df.height} 行 -> {out}")
    for c in ["return_5m", "return_1h", "volume_ratio_20", "drift_20d", "realized_vol_20d", "funding_rate", "oi"]:
        n = df.height - df[c].null_count()
        print(f"  {c:<16}: 有効 {n} / {df.height}")


if __name__ == "__main__":
    main()

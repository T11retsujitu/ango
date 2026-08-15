"""normalized Parquet から features Parquet を生成する。

    python -m mce.features

normalized からの決定的な全再生成なので、何度実行しても結果は同じ(冪等)。
PoC のデータ量(数年分の5分足でも数十万行)では全再生成で十分速い。

ギャップの扱い:
- リターン系は行シフトではなく「ts の完全一致 join」で計算する。
  基準となる過去/未来のバーが欠損している場合、その特徴量は null になる
  (欠損をまたいで誤った期間のリターンを作らない)。
- volume_ratio_20 は直近20本(現在バーを含まない)が揃っているときのみ値を持つ。
- funding_rate は as-of join(その時点で確定している直近値)。9時間より古い値は
  使わない(= funding データが無い期間は null)。
"""

import polars as pl

from mce import config


def _close_at_offset(df: pl.DataFrame, offset_min: int, name: str) -> pl.DataFrame:
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

    # 過去/未来の close を ts 一致 join で取り付ける(ギャップは自動的に null)
    for offset_min, name in [
        (5, "close_5m_ago"),
        (60, "close_1h_ago"),
        (-5, "close_5m_later"),
        (-60, "close_1h_later"),
        (-240, "close_4h_later"),
    ]:
        df = df.join(_close_at_offset(ohlcv, offset_min, name), on="ts", how="left")

    # 直近20本(現在バー含まず)の出来高平均。window [ts-100m, ts) に
    # ちょうど20本あるときだけ有効。
    df = df.with_columns(pl.lit(1, dtype=pl.Int32).alias("_one")).with_columns(
        pl.col("volume").rolling_sum_by("ts", window_size="100m", closed="left").alias("_vol_sum"),
        pl.col("_one").rolling_sum_by("ts", window_size="100m", closed="left").alias("_vol_n"),
    )

    df = df.with_columns(
        (pl.col("close") / pl.col("close_5m_ago") - 1).alias("return_5m"),
        (pl.col("close") / pl.col("close_1h_ago") - 1).alias("return_1h"),
        (pl.col("close_5m_later") / pl.col("close") - 1).alias("fwd_return_5m"),
        (pl.col("close_1h_later") / pl.col("close") - 1).alias("fwd_return_1h"),
        (pl.col("close_4h_later") / pl.col("close") - 1).alias("fwd_return_4h"),
        pl.when(pl.col("_vol_n") == 20)
        .then(pl.col("volume") / (pl.col("_vol_sum") / 20))
        .alias("volume_ratio_20"),
    ).drop("_one", "_vol_sum", "_vol_n", "close_5m_ago", "close_1h_ago", "close_5m_later", "close_1h_later", "close_4h_later")

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

    return df


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

    out = config.FEATURES_DIR / f"{config.SOURCE}_{config.INST_ID}_{config.BAR}.parquet"
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".parquet.tmp")
    df.write_parquet(tmp)
    tmp.replace(out)

    print(f"features: {df.height} 行 -> {out}")
    for c in ["return_5m", "return_1h", "volume_ratio_20", "fwd_return_5m", "fwd_return_1h", "fwd_return_4h", "funding_rate", "oi"]:
        n = df.height - df[c].null_count()
        print(f"  {c:<16}: 有効 {n} / {df.height}")


if __name__ == "__main__":
    main()

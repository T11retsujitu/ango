"""normalized Parquet から評価専用ラベル(fwd_return_*)を生成する。

    python -m mce.labels

ラベルは「バー t の close から見た未来のリターン」であり、strategy feature として
参照してはならない(backtest loader が fwd_ 接頭辞を拒否する)。出力は
data/labels/ のみ。条件検索・評価では features ⋈ labels を明示 join して使う。
規約は docs/data_contract.md §4 を参照。
"""

import polars as pl

from mce import config
from mce.features import close_at_offset

LABEL_PREFIX = "fwd_"

# (offset_min, 列名)。offset は負 = 未来の close を参照する
_HORIZONS = [(-5, "fwd_return_5m"), (-60, "fwd_return_1h"), (-240, "fwd_return_4h")]


def build_labels(ohlcv: pl.DataFrame) -> pl.DataFrame:
    """fwd_return_* を ts 完全一致 join で計算する(未来バー欠損なら null)。"""
    df = ohlcv.sort("ts").select("ts", "symbol", "source", "market_type", "close")
    for offset_min, name in _HORIZONS:
        df = df.join(close_at_offset(ohlcv, offset_min, f"_c_{name}"), on="ts", how="left")
    df = df.with_columns(
        [(pl.col(f"_c_{name}") / pl.col("close") - 1).alias(name) for _, name in _HORIZONS]
    ).drop("close", *[f"_c_{name}" for _, name in _HORIZONS])
    return df


def main() -> None:
    ohlcv_path = config.ohlcv_parquet()
    if not ohlcv_path.exists():
        raise SystemExit("normalized の OHLCV がありません。先に `python -m mce.ingest ohlcv` を実行してください。")
    df = build_labels(pl.read_parquet(ohlcv_path))

    out = config.labels_parquet()
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".parquet.tmp")
    df.write_parquet(tmp)
    tmp.replace(out)

    print(f"labels: {df.height} 行 -> {out}")
    for _, name in _HORIZONS:
        n = df.height - df[name].null_count()
        print(f"  {name:<14}: 有効 {n} / {df.height}")


if __name__ == "__main__":
    main()

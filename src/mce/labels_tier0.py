"""Phase 7 Tier 0 の評価専用ラベル Y1 / Y2 / Y3(事前登録 §5)。

    python -m mce.labels_tier0

**このコマンドの実行が事前登録の凍結を確定させる。** 実行時に
`docs/phase7/tier0_screening_preregistration_v1.md` と `mce.tier0_prereg` の指紋を
`experiments/phase7/tier0_freeze.json` へ記録し、以後の変更は v2 扱いとなる。

定義(すべて執行整合。signal はバー t の close 後、entry は `open[t+1]`):

```text
Y1_h(t) = open[t+1+h] / open[t+1] - 1
Y2_h(t) = log(1 + sqrt( sum_{i=1..h} ( log open[t+1+i] - log open[t+i] )^2 ))
Y3_h(t) = min_{i=1..h}( low[t+i] ) / open[t+1] - 1        # long 目線の MAE
```

- 参照は行シフトではなく **ts 完全一致 join**。欠損バーを跨いだ値を作らない。
- h 個の項が完全に揃わない行は null(部分窓を許さない)。
- 出力は `data/labels/` のみ。features へ `fwd_` 列を書かない(data contract §4)。
"""

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import polars as pl

from mce import config, experiments, tier0_prereg
from mce.backtest.splits import FINAL_OOS_START
from mce.binance_vision import DEFAULT_SYMBOL

BAR_MINUTES = 5
LABEL_PREFIX = "fwd_"
FREEZE_ARTIFACT = Path("experiments") / "phase7" / "tier0_freeze.json"

# 事前登録 §6 に現れる全 horizon(バー)
HORIZONS = tuple(sorted({h for s in tier0_prereg.INFORMATION_SETS for h in s.horizons_bars}))


class LabelGuardError(RuntimeError):
    """入力が事前登録の前提(封印・observable 分離)を満たしていない。"""


def assert_input_is_sealed_and_observable(df: pl.DataFrame, source: Path) -> None:
    """事前登録 §18-2 の load-time assert。"""
    allowed = ("data/features/binance_", "data/normalized/binance/")
    if not any(source.as_posix().startswith(a) for a in allowed):
        raise LabelGuardError(f"許可されていない入力: {source}(許可: {allowed})")
    forward = [c for c in df.columns if c.startswith(LABEL_PREFIX)]
    if forward:
        raise LabelGuardError(f"入力に先読み列が含まれている: {forward}")
    ts_max = df["ts"].max()
    if ts_max >= FINAL_OOS_START:
        raise LabelGuardError(f"入力に封印期間の行がある: max(ts)={ts_max} >= {FINAL_OOS_START}")


def _at_offset(df: pl.DataFrame, column: str, offset_minutes: int, name: str) -> pl.DataFrame:
    """`ts + offset` の位置に `column` を並べた表。join すると「offset 分ずれた値」になる。

    offset が負なら未来の値を参照する(ラベル専用。features では禁止)。
    """
    return df.select(
        (pl.col("ts") + pl.duration(minutes=offset_minutes)).alias("ts"),
        pl.col(column).alias(name),
    )


def build_labels(ohlcv: pl.DataFrame, horizons: tuple[int, ...] = HORIZONS) -> pl.DataFrame:
    """Y1 / Y2 / Y3 を全 horizon について作る(ts 完全一致 join・部分窓は null)。"""
    df = ohlcv.sort("ts").select("ts", "open", "low", "symbol", "source", "market_type")

    # bar j の open->次 open リターン(bar j の始点で確定しない未来値なのでラベル側で扱う)
    base = df.join(_at_offset(df, "open", -BAR_MINUTES, "_open_next"), on="ts", how="left")
    base = base.with_columns(
        (pl.col("_open_next").log() - pl.col("open").log()).alias("_uo")
    ).with_columns(pl.col("_uo").pow(2).alias("_uo2"))

    out = df.select("ts", "symbol", "source", "market_type")
    # entry 価格 open[t+1]
    out = out.join(_at_offset(df, "open", -BAR_MINUTES, "_entry"), on="ts", how="left")

    for h in horizons:
        # --- Y1: open[t+1+h] / open[t+1] - 1
        out = out.join(
            _at_offset(df, "open", -BAR_MINUTES * (1 + h), f"_exit_{h}"), on="ts", how="left"
        )
        # --- Y2: 保有区間 [t+1, t+1+h) の open-to-open 実現ボラ
        #   R(tau) = sum_{j=tau-h+1..tau} uo[j]^2 を tau = t+h で評価 -> offset -5h
        window = f"{BAR_MINUTES * h}m"
        rolled = base.with_columns(
            pl.col("_uo2").rolling_sum_by("ts", window_size=window, closed="right").alias("_rs"),
            pl.col("_uo2").is_not_null().cast(pl.Int32)
            .rolling_sum_by("ts", window_size=window, closed="right")
            .alias("_rn"),
        ).with_columns(
            pl.when(pl.col("_rn") == h).then(pl.col("_rs")).alias("_rs_full")
        )
        out = out.join(
            _at_offset(rolled, "_rs_full", -BAR_MINUTES * h, f"_rv2_{h}"), on="ts", how="left"
        )
        # --- Y3: min low over [t+1, t+h] -> tau = t+h の過去 h 本 min を offset -5h
        rolled_low = base.with_columns(
            pl.col("low").rolling_min_by("ts", window_size=window, closed="right").alias("_lmin"),
            pl.col("low").is_not_null().cast(pl.Int32)
            .rolling_sum_by("ts", window_size=window, closed="right")
            .alias("_ln"),
        ).with_columns(pl.when(pl.col("_ln") == h).then(pl.col("_lmin")).alias("_lmin_full"))
        out = out.join(
            _at_offset(rolled_low, "_lmin_full", -BAR_MINUTES * h, f"_low_{h}"), on="ts", how="left"
        )
        out = out.with_columns(
            (pl.col(f"_exit_{h}") / pl.col("_entry") - 1).alias(f"{LABEL_PREFIX}y1_h{h}"),
            (1 + pl.col(f"_rv2_{h}").sqrt()).log().alias(f"{LABEL_PREFIX}y2_h{h}"),
            (pl.col(f"_low_{h}") / pl.col("_entry") - 1).alias(f"{LABEL_PREFIX}y3_h{h}"),
        ).drop(f"_exit_{h}", f"_rv2_{h}", f"_low_{h}")

    return out.drop("_entry")


def freeze_record(source: Path, labels_path: Path, rows: int) -> dict:
    """凍結確定の記録(事前登録の改訂規則)。"""
    doc = Path("docs/phase7/tier0_screening_preregistration_v1.md")
    return {
        "record": "phase7_tier0_freeze_v1",
        "protocol": tier0_prereg.PROTOCOL,
        "frozen_at": datetime.now(timezone.utc).isoformat(),
        "prereg_sha256": hashlib.sha256(Path(tier0_prereg.__file__).read_bytes()).hexdigest(),
        "prereg_doc_sha256": hashlib.sha256(doc.read_bytes()).hexdigest() if doc.exists() else None,
        "source_commit": experiments.git_commit_hash(),
        "features_input": source.as_posix(),
        "labels_output": labels_path.as_posix(),
        "label_rows": rows,
        "horizons_bars": list(HORIZONS),
        "targets": dict(tier0_prereg.TARGETS),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Phase 7 Tier 0 labels (Y1/Y2/Y3)")
    parser.add_argument("--symbol", default=DEFAULT_SYMBOL)
    args = parser.parse_args()

    source = config.binance_features_parquet(args.symbol)
    if not source.exists():
        raise SystemExit(f"features がありません: {source}")
    df = pl.read_parquet(source)
    assert_input_is_sealed_and_observable(df, source)

    labels = build_labels(df)
    out = config.LABELS_DIR / f"{config.BINANCE_SOURCE}_{args.symbol}_{config.BAR}.parquet"
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".parquet.tmp")
    labels.write_parquet(tmp)
    tmp.replace(out)

    record = freeze_record(source, out, labels.height)
    FREEZE_ARTIFACT.parent.mkdir(parents=True, exist_ok=True)
    if not FREEZE_ARTIFACT.exists():  # 凍結時刻は一度きり(上書きしない)
        FREEZE_ARTIFACT.write_text(
            json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    print(f"labels: {labels.height} 行 -> {out}")
    for h in HORIZONS:
        for target in ("y1", "y2", "y3"):
            column = f"{LABEL_PREFIX}{target}_h{h}"
            print(f"  {column:<12}: 有効 {labels.height - labels[column].null_count()} / {labels.height}")
    print(f"\n凍結確定記録: {FREEZE_ARTIFACT}")


if __name__ == "__main__":
    main()

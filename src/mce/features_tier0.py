"""Phase 7 Tier 0 の observable features(Binance venue)。

    python -m mce.features_tier0

`data/normalized/binance/` の3テーブルから
`data/features/binance_BTCUSDT_5m.parquet` を決定的に全再生成する。

構成:

```text
baseline (A)  : mce.features.build_features と同一定義の OHLCV 由来 observable
Tier 0 (X)    : 集約 aggressive flow / derivatives state / basis
```

baseline を **OKX と同じコードで作る**ことが重要で、入れ子比較 A vs A+X の
「A 側だけ venue 差で有利/不利になる」ことを避ける(OKX 固有の funding / oi 列は
Binance では供給されないので落とす)。

availability(docs/data_contract.md §3):

- **close_of_bar**: バー区間 `[ts, ts+5m)` を集約した値(taker buy 比率・約定件数など)。
  バー close 後に signal を作るので利用可能。
- **start_of_bar**: バー開始時刻 `ts` 時点のスナップショット(OI・long/short ratio・
  premium open)。signal 時刻(bar close)から見て5分以上前の情報しか使わない。

**この module は labels を一切読まない。** fwd_ 列の生成・参照は禁止で、
出力に fwd_ 列が混入していないことを毎回検査する。
"""

import argparse

import polars as pl

from mce import config, features
from mce.binance_vision import DEFAULT_SYMBOL

# Tier 0 の X 列 → availability。baseline 側は features.AVAILABILITY を継承する。
TIER0_AVAILABILITY: dict[str, str] = {
    # T0-A 集約 aggressive flow(バー区間の集約 → close_of_bar)
    "taker_buy_ratio": "close_of_bar",
    "taker_buy_quote_ratio": "close_of_bar",
    "trade_count": "close_of_bar",
    "avg_trade_size": "close_of_bar",
    "avg_trade_notional": "close_of_bar",
    # T0-B derivatives state(ts 時点のスナップショット → start_of_bar)
    "open_interest": "start_of_bar",
    "open_interest_value": "start_of_bar",
    "top_trader_account_ls_ratio": "start_of_bar",
    "top_trader_position_ls_ratio": "start_of_bar",
    "global_account_ls_ratio": "start_of_bar",
    "taker_ls_vol_ratio": "start_of_bar",
    # T0-C basis
    "premium_open": "start_of_bar",
    "premium_close": "close_of_bar",
}

# OKX 固有で Binance Tier 0 には供給元が無い列(baseline から落とす)
_OKX_ONLY = ("funding_rate", "oi", "oi_usd")

_OHLCV_COLUMNS = ("ts", "open", "high", "low", "close", "volume", "volume_quote", "symbol", "source", "market_type")

_METRIC_COLUMNS = (
    "open_interest",
    "open_interest_value",
    "top_trader_account_ls_ratio",
    "top_trader_position_ls_ratio",
    "global_account_ls_ratio",
    "taker_ls_vol_ratio",
)


# normalized には source どおりの値を残すが、observable では物理的に不可能な値を
# 欠測として扱う(OI=0 の穴が 485 行実在する。log/z-score を壊さないため)。
# **これは事前(ラベル閲覧前)に決めた固定規則であり、結果を見て変えない。**
_POSITIVE_ONLY = _METRIC_COLUMNS


def _positive_only() -> list[pl.Expr]:
    return [
        pl.when(pl.col(c) > 0).then(pl.col(c)).otherwise(None).alias(c) for c in _POSITIVE_ONLY
    ]


def _safe_ratio(numerator: str, denominator: str, name: str) -> pl.Expr:
    """分母が 0 / null のときは null(0除算で無限大を作らない)。"""
    return (
        pl.when(pl.col(denominator) > 0)
        .then(pl.col(numerator) / pl.col(denominator))
        .otherwise(None)
        .alias(name)
    )


def build_tier0_features(
    klines: pl.DataFrame,
    metrics: pl.DataFrame | None = None,
    premium: pl.DataFrame | None = None,
) -> pl.DataFrame:
    """normalized 3テーブル → observable features。join は ts 完全一致(補間なし)。"""
    if klines.is_empty():
        raise ValueError("klines が空")
    base = features.build_features(klines.select(_OHLCV_COLUMNS).sort("ts")).drop(_OKX_ONLY)

    flow = klines.select(
        "ts",
        pl.col("trades").alias("trade_count"),
        _safe_ratio("taker_buy_volume", "volume", "taker_buy_ratio"),
        _safe_ratio("taker_buy_quote", "volume_quote", "taker_buy_quote_ratio"),
        pl.when(pl.col("trades") > 0)
        .then(pl.col("volume") / pl.col("trades"))
        .alias("avg_trade_size"),
        pl.when(pl.col("trades") > 0)
        .then(pl.col("volume_quote") / pl.col("trades"))
        .alias("avg_trade_notional"),
    )
    df = base.join(flow, on="ts", how="left")

    if metrics is not None and not metrics.is_empty():
        snapshot = metrics.select("ts", *_METRIC_COLUMNS).with_columns(_positive_only())
        df = df.join(snapshot, on="ts", how="left")
    else:
        df = df.with_columns(
            [pl.lit(None, dtype=pl.Float64).alias(c) for c in _METRIC_COLUMNS]
        )

    if premium is not None and not premium.is_empty():
        df = df.join(premium.select("ts", "premium_open", "premium_close"), on="ts", how="left")
    else:
        df = df.with_columns(
            pl.lit(None, dtype=pl.Float64).alias("premium_open"),
            pl.lit(None, dtype=pl.Float64).alias("premium_close"),
        )

    assert_observable(df)
    return df.sort("ts")


def assert_observable(df: pl.DataFrame) -> None:
    """契約適合(fwd_ 無し・全列 availability 宣言済み)。"""
    declared = {**features.AVAILABILITY, **TIER0_AVAILABILITY}
    for c in df.columns:
        if c.startswith("fwd_"):
            raise AssertionError(f"label 列 {c} が observable features に混入している")
        if c in features.META_COLUMNS:
            continue
        if c not in declared:
            raise AssertionError(f"列 {c} の availability が未宣言(TIER0_AVAILABILITY へ追加すること)")


def main() -> None:
    parser = argparse.ArgumentParser(description="Phase 7 Tier 0 observable features")
    parser.add_argument("--symbol", default=DEFAULT_SYMBOL)
    args = parser.parse_args()

    klines_path = config.binance_klines_parquet(args.symbol)
    if not klines_path.exists():
        raise SystemExit(
            "normalized の Binance klines がありません。"
            "先に `python -m mce.binance_vision` と `python -m mce.normalize_binance` を実行してください。"
        )
    metrics_path = config.binance_metrics_parquet(args.symbol)
    premium_path = config.binance_premium_index_parquet(args.symbol)
    df = build_tier0_features(
        pl.read_parquet(klines_path),
        pl.read_parquet(metrics_path) if metrics_path.exists() else None,
        pl.read_parquet(premium_path) if premium_path.exists() else None,
    )

    out = config.binance_features_parquet(args.symbol)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".parquet.tmp")
    df.write_parquet(tmp)
    tmp.replace(out)

    print(f"tier0 features: {df.height} 行 -> {out}")
    for c in TIER0_AVAILABILITY:
        print(f"  {c:<28}: 有効 {df.height - df[c].null_count()} / {df.height}")


if __name__ == "__main__":
    main()

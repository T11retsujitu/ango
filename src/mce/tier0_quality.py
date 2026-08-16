"""Phase 7 Tier 0 の品質レポート(**ラベルを一切読まない**)。

    python -m mce.tier0_quality --json experiments/phase7/tier0_quality_v1.json

検査するもの:

1. **会計** — raw ファイル数・raw 行数・封印(ts >= 2026-01-01)で落とした行数・
   重複行数・値が食い違う重複(= 契約違反)
2. **時刻境界** — 5分グリッド整合、単調増加、UTC、`ts < FINAL_OOS_START`
3. **欠損** — 5分グリッドに対する欠測数・最長ギャップ・大きいギャップ上位
4. **単位・値域** — taker buy ≤ 総量、high/low の整合、OI・比率の正値性、null 数
5. **既存 OKX OHLCV との整合** — 重なり本数、close 価格の相対差、5分リターン相関

すべて**同時刻の記述統計**であり、将来リターン・ラベル・target を計算しない。
この module は `mce.labels` を import しないし、`data/labels/` を開かない
(pre-registration 前に効果量を覗かないための構造的措置)。
"""

import argparse
import json
from pathlib import Path

import polars as pl

from mce import binance_vision, config, normalize_binance
from mce.backtest.splits import FINAL_OOS_START
from mce.binance_vision import DEFAULT_SYMBOL

BAR_MS = 5 * 60 * 1000
MAX_GAPS_REPORTED = 10

# バー(区間 [ts, ts+5m))を表すデータセット。snapshot 系(metrics)と扱いを分ける
BAR_SPINE_DATASETS = ("klines_5m", "premium_index_5m")

# null を許さない列(それ以外の列の null 数も必ず報告はする)。
# metrics の long/short ratio は実 dump に空欄が存在するので key に入れない。
KEY_COLUMNS = {
    "klines_5m": (
        "open",
        "high",
        "low",
        "close",
        "volume",
        "volume_quote",
        "trades",
        "taker_buy_volume",
        "taker_buy_quote",
    ),
    "metrics_5m": ("open_interest", "open_interest_value"),
    "premium_index_5m": ("premium_open", "premium_high", "premium_low", "premium_close"),
}


def _round(value, digits: int = 6):
    return None if value is None else round(float(value), digits)


def timestamp_report(df: pl.DataFrame) -> dict:
    """5分グリッド整合・欠測・封印遵守。"""
    ts = df["ts"].sort()
    epoch_ms = ts.dt.epoch("ms")
    diffs = epoch_ms.diff().drop_nulls()
    gaps = (
        pl.DataFrame({"ts": ts[1:], "gap_ms": diffs})
        .filter(pl.col("gap_ms") > BAR_MS)
        .sort("gap_ms", descending=True)
    )
    ts_min, ts_max = ts.min(), ts.max()
    expected = (int(ts_max.timestamp() * 1000) - int(ts_min.timestamp() * 1000)) // BAR_MS + 1
    return {
        "rows": df.height,
        "ts_min": ts_min.isoformat(),
        "ts_max": ts_max.isoformat(),
        "time_zone": str(df.schema["ts"].time_zone),
        "off_grid_rows": int((epoch_ms % BAR_MS != 0).sum()),
        "strictly_increasing": bool((diffs > 0).all()),
        "expected_rows_on_grid": expected,
        "missing_rows": expected - df.height,
        "missing_ratio": _round((expected - df.height) / expected),
        "gap_count": gaps.height,
        "longest_gap_minutes": _round(gaps["gap_ms"].max() / 60000) if gaps.height else 0,
        "largest_gaps": [
            {"gap_start": row["ts"].isoformat(), "gap_minutes": _round(row["gap_ms"] / 60000)}
            for row in gaps.head(MAX_GAPS_REPORTED).iter_rows(named=True)
        ],
        "sealed_rows_present": int((ts >= FINAL_OOS_START).sum()),
    }


def _null_counts(df: pl.DataFrame) -> dict:
    return {c: int(df[c].null_count()) for c in df.columns if df[c].null_count() > 0}


def klines_value_report(df: pl.DataFrame) -> dict:
    """単位と値域(taker buy は総量の部分集合であるべき、など)。"""
    eps = 1e-9
    violations = df.select(
        (pl.col("taker_buy_volume") > pl.col("volume") + eps).sum().alias("taker_buy_gt_volume"),
        (pl.col("taker_buy_quote") > pl.col("volume_quote") + eps)
        .sum()
        .alias("taker_buy_quote_gt_volume_quote"),
        (pl.col("high") < pl.max_horizontal("open", "close") - eps)
        .sum()
        .alias("high_lt_max_open_close"),
        (pl.col("low") > pl.min_horizontal("open", "close") + eps)
        .sum()
        .alias("low_gt_min_open_close"),
    ).row(0, named=True)
    return {
        "taker_buy_gt_volume": int(violations["taker_buy_gt_volume"]),
        "taker_buy_quote_gt_volume_quote": int(violations["taker_buy_quote_gt_volume_quote"]),
        "negative_volume": int((df["volume"] < 0).sum()),
        "negative_trades": int((df["trades"] < 0).sum()),
        "zero_volume_bars": int((df["volume"] == 0).sum()),
        "zero_trade_bars": int((df["trades"] == 0).sum()),
        "high_lt_max_open_close": int(violations["high_lt_max_open_close"]),
        "low_gt_min_open_close": int(violations["low_gt_min_open_close"]),
        "nonpositive_close": int((df["close"] <= 0).sum()),
        "taker_buy_ratio_median": _round(
            df.filter(pl.col("volume") > 0)
            .select(pl.col("taker_buy_volume") / pl.col("volume"))
            .to_series()
            .median()
        ),
        "trades_median": _round(df["trades"].median()),
        "null_counts": _null_counts(df),
    }


def metrics_value_report(df: pl.DataFrame) -> dict:
    ratios = (
        "top_trader_account_ls_ratio",
        "top_trader_position_ls_ratio",
        "global_account_ls_ratio",
        "taker_ls_vol_ratio",
    )
    return {
        "nonpositive_open_interest": int((df["open_interest"] <= 0).sum()),
        "nonpositive_open_interest_value": int((df["open_interest_value"] <= 0).sum()),
        "negative_ratios": {c: int((df[c] < 0).sum()) for c in ratios},
        "zero_ratios": {c: int((df[c] == 0).sum()) for c in ratios},
        "open_interest_median": _round(df["open_interest"].median(), 3),
        "open_interest_value_median": _round(df["open_interest_value"].median(), 1),
        "implied_price_median": _round(
            (df["open_interest_value"] / df["open_interest"]).median(), 2
        ),
        "null_counts": _null_counts(df),
    }


def premium_value_report(df: pl.DataFrame) -> dict:
    return {
        "abs_premium_gt_10pct": int((df["premium_close"].abs() > 0.10).sum()),
        "premium_close_median": _round(df["premium_close"].median()),
        "premium_close_p01": _round(df["premium_close"].quantile(0.01)),
        "premium_close_p99": _round(df["premium_close"].quantile(0.99)),
        "premium_samples_median": _round(df["premium_samples"].median(), 1),
        "null_counts": _null_counts(df),
    }


def okx_consistency_report(binance_klines: pl.DataFrame, okx_ohlcv: pl.DataFrame) -> dict:
    """既存 OKX OHLCV との**同時刻**比較(ラベル・将来値を使わない記述統計)。"""
    bn = binance_klines.select("ts", pl.col("close").alias("bn_close"), pl.col("volume").alias("bn_volume"))
    okx = okx_ohlcv.select("ts", pl.col("close").alias("okx_close"), pl.col("volume").alias("okx_volume"))
    joined = bn.join(okx, on="ts", how="inner").sort("ts")
    if joined.is_empty():
        return {"overlap_rows": 0}
    diff_bps = (joined["bn_close"] / joined["okx_close"] - 1).abs() * 1e4
    returns = joined.select(
        (pl.col("bn_close") / pl.col("bn_close").shift(1) - 1).alias("bn_ret"),
        (pl.col("okx_close") / pl.col("okx_close").shift(1) - 1).alias("okx_ret"),
        (pl.col("ts").dt.epoch("ms").diff() == BAR_MS).alias("contiguous"),
    ).filter(pl.col("contiguous"))
    return {
        "overlap_rows": joined.height,
        "binance_only_rows": bn.join(okx, on="ts", how="anti").height,
        "okx_only_rows": okx.join(bn, on="ts", how="anti").height,
        "close_abs_diff_bps_median": _round(diff_bps.median(), 3),
        "close_abs_diff_bps_p99": _round(diff_bps.quantile(0.99), 3),
        "close_abs_diff_bps_max": _round(diff_bps.max(), 3),
        "return_correlation": _round(
            returns.select(pl.corr("bn_ret", "okx_ret")).item(), 6
        ),
        "volume_ratio_median": _round(
            joined.filter(pl.col("okx_volume") > 0)
            .select(pl.col("bn_volume") / pl.col("okx_volume"))
            .to_series()
            .median(),
            3,
        ),
    }


def features_report(df: pl.DataFrame, tier0_columns) -> dict:
    """observable features の被覆(ラベルは含まない)。"""
    coverage = {
        c: {
            "non_null": int(df.height - df[c].null_count()),
            "coverage": _round((df.height - df[c].null_count()) / df.height, 4),
        }
        for c in tier0_columns
        if c in df.columns
    }
    by_year = (
        df.group_by(pl.col("ts").dt.year().alias("year"))
        .agg(pl.len().alias("rows"))
        .sort("year")
    )
    return {
        "rows": df.height,
        "ts_min": df["ts"].min().isoformat(),
        "ts_max": df["ts"].max().isoformat(),
        "forward_looking_columns": [c for c in df.columns if c.startswith("fwd_")],
        "tier0_coverage": coverage,
        "rows_by_year": {str(r["year"]): r["rows"] for r in by_year.iter_rows(named=True)},
    }


def build_report(symbol: str = DEFAULT_SYMBOL, raw_scan: bool = True) -> dict:
    report: dict = {
        "report": "phase7_tier0_quality_v1",
        "venue": "binance_um_perp",
        "symbol": symbol,
        "screening_cutoff": FINAL_OOS_START.isoformat(),
        "labels_read": False,
        "datasets": {},
    }
    paths = {
        "klines_5m": config.binance_klines_parquet(symbol),
        "metrics_5m": config.binance_metrics_parquet(symbol),
        "premium_index_5m": config.binance_premium_index_parquet(symbol),
    }
    value_reports = {
        "klines_5m": klines_value_report,
        "metrics_5m": metrics_value_report,
        "premium_index_5m": premium_value_report,
    }
    frames: dict[str, pl.DataFrame] = {}
    for name, path in paths.items():
        if not path.exists():
            report["datasets"][name] = {"present": False, "path": path.as_posix()}
            continue
        df = pl.read_parquet(path).sort("ts")
        frames[name] = df
        entry = {
            "present": True,
            "path": path.as_posix(),
            "timestamps": timestamp_report(df),
            "values": value_reports[name](df),
        }
        entry["source"] = binance_vision.source_digest(name, symbol)
        if raw_scan:
            _, accounting = normalize_binance.scan_dataset(name, symbol)
            entry["raw_accounting"] = accounting
        report["datasets"][name] = entry

    okx_path = config.ohlcv_parquet()
    if "klines_5m" in frames and okx_path.exists():
        # OKX 側は封印期間を含みうるので、比較前に必ず落とす(件数としても数えない)
        okx = pl.read_parquet(okx_path).filter(pl.col("ts") < FINAL_OOS_START)
        report["okx_consistency"] = okx_consistency_report(frames["klines_5m"], okx)
    else:
        report["okx_consistency"] = {"okx_ohlcv_present": okx_path.exists()}

    features_path = config.binance_features_parquet(symbol)
    if features_path.exists():
        from mce.features_tier0 import TIER0_AVAILABILITY

        report["features"] = features_report(
            pl.read_parquet(features_path), tuple(TIER0_AVAILABILITY)
        )
        if "metrics_5m" in frames and "klines_5m" in frames:
            # bar 開始時刻と完全一致し、かつ observable の正値規則
            # (mce.features_tier0 の `_positive_only`)を満たす snapshot だけが
            # features へ入るはず。grid 外や 0 埋めが紛れていないかを本数で照合する。
            joinable = (
                frames["metrics_5m"]
                .filter((pl.col("ts").dt.epoch("ms") % BAR_MS) == 0)
                .filter(pl.col("open_interest") > 0)
                .join(frames["klines_5m"].select("ts"), on="ts", how="semi")
                .height
            )
            report["features"]["metrics_joinable_rows"] = joinable
    else:
        report["features"] = {"present": False, "path": features_path.as_posix()}

    report["gates"] = evaluate_gates(report)
    return report


def evaluate_gates(report: dict) -> dict:
    """事前に書き下せる機械的ゲート(結果を見てから緩めない)。"""
    gates: dict[str, bool] = {}
    for name, entry in report["datasets"].items():
        if not entry.get("present"):
            gates[f"{name}:present"] = False
            continue
        ts = entry["timestamps"]
        values = entry["values"]
        raw = entry.get("raw_accounting", {})
        if name in BAR_SPINE_DATASETS:
            # バーは [ts, ts+5m) を表すので、grid 外の行があれば契約違反。
            # snapshot 系(metrics)は上流が数秒ずれることが実在するため、
            # 件数を報告した上で features への混入を別ゲートで確認する。
            gates[f"{name}:on_5m_grid"] = ts["off_grid_rows"] == 0
        gates[f"{name}:strictly_increasing"] = ts["strictly_increasing"]
        gates[f"{name}:utc"] = ts["time_zone"] == "UTC"
        gates[f"{name}:no_sealed_rows"] = ts["sealed_rows_present"] == 0
        # 値が食い違う重複そのものは raw に実在する(日付境界)。ゲートは
        # 「所有ファイル規則で決定的に解決できたか」を見る。
        gates[f"{name}:no_unresolved_conflicts"] = raw.get("unresolved_conflicts", 0) == 0
        gates[f"{name}:no_null_key_columns"] = not [
            c for c in values["null_counts"] if c in KEY_COLUMNS.get(name, ())
        ]
    if "klines_5m" in report["datasets"] and report["datasets"]["klines_5m"].get("present"):
        v = report["datasets"]["klines_5m"]["values"]
        gates["klines_5m:taker_buy_within_volume"] = (
            v["taker_buy_gt_volume"] == 0 and v["taker_buy_quote_gt_volume_quote"] == 0
        )
        gates["klines_5m:ohlc_consistent"] = (
            v["high_lt_max_open_close"] == 0 and v["low_gt_min_open_close"] == 0
        )
    features = report.get("features", {})
    if features.get("rows"):
        gates["features:no_forward_looking_columns"] = not features["forward_looking_columns"]
        if "metrics_joinable_rows" in features:
            # grid 外 snapshot が exact join をすり抜けていないことの本数照合
            gates["features:metrics_join_is_exact"] = (
                features["tier0_coverage"]["open_interest"]["non_null"]
                == features["metrics_joinable_rows"]
            )
    consistency = report.get("okx_consistency", {})
    if consistency.get("overlap_rows"):
        # 別 venue でも同じ資産なので、同時刻 close はほぼ一致し、5分リターンは高相関のはず。
        gates["okx_consistency:close_median_diff_under_10bps"] = (
            consistency["close_abs_diff_bps_median"] < 10
        )
        gates["okx_consistency:return_correlation_over_0.9"] = (
            consistency["return_correlation"] > 0.9
        )
    gates["all_passed"] = all(gates.values())
    return gates


def render(report: dict) -> str:
    lines = [
        f"# Phase 7 Tier 0 quality report ({report['venue']} / {report['symbol']})",
        "",
        f"- screening cutoff: `ts < {report['screening_cutoff']}`",
        f"- labels read: {report['labels_read']}",
        "",
        "## datasets",
        "",
        "| dataset | rows | ts_min | ts_max | missing | longest gap (min) | dup dropped | conflicting |",
        "|---|---:|---|---|---:|---:|---:|---:|",
    ]
    for name, entry in report["datasets"].items():
        if not entry.get("present"):
            lines.append(f"| {name} | (なし) | - | - | - | - | - | - |")
            continue
        t = entry["timestamps"]
        raw = entry.get("raw_accounting", {})
        lines.append(
            f"| {name} | {t['rows']} | {t['ts_min']} | {t['ts_max']} | {t['missing_rows']} | "
            f"{t['longest_gap_minutes']} | {raw.get('duplicate_rows_dropped', '-')} | "
            f"{raw.get('conflicting_duplicates', '-')} |"
        )
    consistency = report.get("okx_consistency", {})
    if consistency.get("overlap_rows"):
        lines += [
            "",
            "## OKX OHLCV との整合(同時刻・記述統計のみ)",
            "",
            f"- overlap: {consistency['overlap_rows']} 本 "
            f"(binance のみ {consistency['binance_only_rows']} / okx のみ {consistency['okx_only_rows']})",
            f"- close 相対差 |Δ|: median {consistency['close_abs_diff_bps_median']} bps / "
            f"p99 {consistency['close_abs_diff_bps_p99']} bps / max {consistency['close_abs_diff_bps_max']} bps",
            f"- 5分リターン相関: {consistency['return_correlation']}",
            f"- volume 比(binance/okx)中央値: {consistency['volume_ratio_median']}",
        ]
    lines += ["", "## gates", ""]
    for gate, passed in report["gates"].items():
        lines.append(f"- {'PASS' if passed else 'FAIL'} `{gate}`")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Phase 7 Tier 0 quality report")
    parser.add_argument("--symbol", default=DEFAULT_SYMBOL)
    parser.add_argument("--json", type=Path, default=None)
    parser.add_argument("--no-raw-scan", action="store_true", help="raw zip の再走査を省く")
    args = parser.parse_args()

    report = build_report(args.symbol, raw_scan=not args.no_raw_scan)
    print(render(report))
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(
            json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        print(f"\nwrote {args.json}")
    if not report["gates"]["all_passed"]:
        raise SystemExit("quality gate 不通過(レポートの FAIL 項目を参照)")


if __name__ == "__main__":
    main()

"""凍結済み first-touch v1 を一度だけ実行する。

仕様: docs/findings/2026-08-16-first-touch-v1-protocol.md
出力: data/analysis/first_touch_v1/ (manifest.json が再実行ロック)
"""

from __future__ import annotations

import argparse
import bisect
import hashlib
import json
import math
import random
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import mean, median

import polars as pl

from mce.first_touch import BarrierSpec, OhlcBar, simulate_short


UTC = timezone.utc
FEATURES = Path("data/features/okx_BTC-USDT-SWAP_5m.parquet")
OHLCV = Path("data/normalized/ohlcv/okx_BTC-USDT-SWAP_5m.parquet")
OUT = Path("data/analysis/first_touch_v1")
SEED = 20260816
COOLDOWN = timedelta(hours=4)
COSTS = (10.0, 12.0, 15.0)
SPECS = (
    BarrierSpec("B1", 15, 30, 60),
    BarrierSpec("B2", 20, 30, 60),
    BarrierSpec("B3", 20, 40, 120),
    BarrierSpec("B4", 30, 50, 120),
    BarrierSpec("B5", 30, 60, 240),
    BarrierSpec("B6", 40, 80, 240),
)
SPLITS = (
    ("development", None, datetime(2025, 1, 1, tzinfo=UTC)),
    ("validation", datetime(2025, 1, 1, tzinfo=UTC), datetime(2026, 1, 1, tzinfo=UTC)),
    ("final", datetime(2026, 1, 1, tzinfo=UTC), None),
)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def load_frame() -> pl.DataFrame:
    features = pl.read_parquet(
        FEATURES,
        columns=["ts", "return_5m", "return_1h", "volume_ratio_20", "realized_vol_20d"],
    )
    ohlcv = pl.read_parquet(OHLCV, columns=["ts", "open", "high", "low", "close"])
    df = ohlcv.join(features, on="ts", how="inner").sort("ts")
    df = df.with_columns(
        (pl.col("high") - pl.col("low")).alias("bar_range"),
        pl.col("return_5m").abs().alias("abs_return_5m"),
        pl.lit(1, dtype=pl.Int32).alias("_one"),
    ).with_columns(
        pl.col("bar_range").rolling_mean_by("ts", window_size="100m", closed="left").alias("prior_range20"),
        pl.col("_one").rolling_sum_by("ts", window_size="100m", closed="left").alias("prior_range_n"),
        pl.col("abs_return_5m").rolling_sum_by("ts", window_size="1h", closed="right").alias("trail_abs_1h"),
        pl.col("_one").rolling_sum_by("ts", window_size="1h", closed="right").alias("trail_abs_n"),
    )
    return df.drop("_one")


def h6_events(df: pl.DataFrame) -> tuple[list[dict], list[datetime]]:
    raw = df.filter(
        (pl.col("return_5m") > 0)
        & (pl.col("volume_ratio_20") >= 3)
        & (pl.col("prior_range_n") == 20)
        & (pl.col("bar_range") >= 2 * pl.col("prior_range20"))
    )
    raw_rows = raw.select(
        "ts", "return_1h", "realized_vol_20d", "trail_abs_1h", "trail_abs_n"
    ).to_dicts()
    return raw_rows, raw["ts"].to_list()


def split_bounds(df: pl.DataFrame, name: str) -> tuple[datetime, datetime]:
    data_start = df["ts"].min()
    # max ts is the start of the last available 5m bar; its close is max+5m.
    data_end = df["ts"].max() + timedelta(minutes=5)
    for split_name, start, end in SPLITS:
        if split_name == name:
            return (max(start or data_start, data_start), min(end or data_end, data_end))
    raise KeyError(name)


def valid_common_events(df: pl.DataFrame, raw_events: list[dict], split: str) -> list[dict]:
    start, end = split_bounds(df, split)
    ts_set = set(df["ts"].to_list())
    eligible: list[dict] = []
    for event in raw_events:
        if (
            event["realized_vol_20d"] is None
            or event["trail_abs_1h"] is None
            or event["trail_abs_n"] != 12
        ):
            continue
        event_ts = event["ts"]
        entry_ts = event_ts + timedelta(minutes=5)
        timeout_ts = entry_ts + timedelta(hours=4)
        if event_ts < start or timeout_ts >= end:
            continue
        required = [entry_ts + timedelta(minutes=5 * i) for i in range(49)]
        if all(ts in ts_set for ts in required):
            eligible.append(event)
    accepted: list[dict] = []
    last_entry: datetime | None = None
    for event in eligible:
        entry_ts = event["ts"] + timedelta(minutes=5)
        if last_entry is None or entry_ts - last_entry >= COOLDOWN:
            accepted.append(event)
            last_entry = entry_ts
    return accepted


def simulate_events(
    df: pl.DataFrame,
    events: list[dict],
    spec: BarrierSpec,
    *,
    policy: str = "stop",
    source: str = "h6",
) -> list[dict]:
    rows = df.select("ts", "open", "high", "low", "close").to_dicts()
    by_ts = {row["ts"]: row for row in rows}
    results: list[dict] = []
    for event in events:
        event_ts = event["ts"]
        entry_ts = event_ts + timedelta(minutes=5)
        timeout_ts = entry_ts + timedelta(minutes=spec.horizon_minutes)
        entry = by_ts.get(entry_ts)
        timeout = by_ts.get(timeout_ts)
        path_rows = [by_ts.get(entry_ts + timedelta(minutes=5 * i)) for i in range(spec.horizon_bars)]
        if entry is None or timeout is None or any(row is None for row in path_rows):
            continue
        bars = [OhlcBar(row["ts"], row["open"], row["high"], row["low"], row["close"]) for row in path_rows]
        result = simulate_short(
            entry_ts=entry_ts,
            entry_price=entry["open"],
            bars=bars,
            timeout_ts=timeout_ts,
            timeout_open=timeout["open"],
            spec=spec,
            ambiguous_policy=policy,
        )
        results.append(
            {
                "source": source,
                "barrier_id": spec.barrier_id,
                "ambiguous_policy": policy,
                "event_ts": event_ts,
                "entry_ts": entry_ts,
                "exit_ts": result.exit_ts,
                "entry_price": entry["open"],
                "exit_price": result.exit_price,
                "status": result.status,
                "gross_return": result.gross_return,
                "holding_minutes": result.holding_minutes,
                "ambiguous": result.ambiguous,
                "fixed_horizon_favorable_bps": result.fixed_horizon_favorable_bps,
                "fixed_horizon_adverse_bps": result.fixed_horizon_adverse_bps,
            }
        )
    return results


def iso_week(ts: datetime) -> str:
    year, week, _ = ts.isocalendar()
    return f"{year:04d}-W{week:02d}"


def cluster_bootstrap_ci(values: list[float], clusters: list[str], *, reps: int = 20_000) -> list[float]:
    grouped: dict[str, list[float]] = defaultdict(list)
    for value, cluster in zip(values, clusters):
        grouped[cluster].append(value)
    keys = sorted(grouped)
    if len(keys) < 2:
        return [float("nan"), float("nan")]
    sums = [sum(grouped[key]) for key in keys]
    counts = [len(grouped[key]) for key in keys]
    rng = random.Random(SEED)
    samples: list[float] = []
    for _ in range(reps):
        total = 0.0
        n = 0
        for _ in keys:
            i = rng.randrange(len(keys))
            total += sums[i]
            n += counts[i]
        samples.append(total / n)
    samples.sort()
    return [samples[int(0.025 * reps)], samples[int(0.975 * reps)]]


def moving_block_ci(values: list[float], clusters: list[str], *, reps: int = 20_000, block: int = 4) -> list[float]:
    grouped: dict[str, list[float]] = defaultdict(list)
    for value, cluster in zip(values, clusters):
        grouped[cluster].append(value)
    keys = sorted(grouped)
    if len(keys) < block:
        return [float("nan"), float("nan")]
    sums = [sum(grouped[key]) for key in keys]
    counts = [len(grouped[key]) for key in keys]
    blocks_needed = math.ceil(len(keys) / block)
    rng = random.Random(SEED)
    samples: list[float] = []
    for _ in range(reps):
        total = 0.0
        n = 0
        for _ in range(blocks_needed):
            start = rng.randrange(len(keys))
            for offset in range(block):
                i = (start + offset) % len(keys)
                total += sums[i]
                n += counts[i]
        samples.append(total / n)
    samples.sort()
    return [samples[int(0.025 * reps)], samples[int(0.975 * reps)]]


def summary(trades: list[dict], cost_bps: float, *, block_ci: bool = False) -> dict:
    usable = [trade for trade in trades if trade["gross_return"] is not None]
    values = [trade["gross_return"] - cost_bps / 10_000 for trade in usable]
    clusters = [iso_week(trade["entry_ts"]) for trade in usable]
    months = [trade["entry_ts"].strftime("%Y-%m") for trade in usable]
    statuses = defaultdict(int)
    for trade in usable:
        statuses[trade["status"]] += 1
    positive = sum(value for value in values if value > 0)
    negative = -sum(value for value in values if value < 0)
    equity = peak = 1.0
    max_dd = 0.0
    for value in values:
        equity *= 1 + value
        peak = max(peak, equity)
        max_dd = min(max_dd, equity / peak - 1)
    lomo: dict[str, float] = {}
    for month in sorted(set(months)):
        remaining = [value for value, item_month in zip(values, months) if item_month != month]
        lomo[month] = mean(remaining) * 10_000 if remaining else float("nan")
    ci = moving_block_ci(values, clusters) if block_ci else cluster_bootstrap_ci(values, clusters)
    return {
        "n": len(usable),
        "week_clusters": len(set(clusters)),
        "cost_bps": cost_bps,
        "mean_net_bps": mean(values) * 10_000 if values else float("nan"),
        "median_net_bps": median(values) * 10_000 if values else float("nan"),
        "win_rate": mean(value > 0 for value in values) if values else float("nan"),
        "tp_rate": statuses["take_profit"] / len(usable) if usable else float("nan"),
        "sl_rate": statuses["stop"] / len(usable) if usable else float("nan"),
        "timeout_rate": statuses["timeout"] / len(usable) if usable else float("nan"),
        "ambiguous_rate": mean(bool(trade["ambiguous"]) for trade in usable) if usable else float("nan"),
        "mean_holding_minutes": mean(trade["holding_minutes"] for trade in usable) if usable else float("nan"),
        "profit_factor": positive / negative if negative else float("inf"),
        "max_sequential_drawdown": max_dd,
        "bootstrap_95_bps": [bound * 10_000 for bound in ci],
        "lomo_mean_net_bps": lomo,
        "lomo_min_bps": min(lomo.values()) if lomo else float("nan"),
    }


def _near_raw_event(ts: datetime, sorted_raw: list[datetime]) -> bool:
    i = bisect.bisect_left(sorted_raw, ts)
    neighbors = sorted_raw[max(0, i - 1) : min(len(sorted_raw), i + 2)]
    return any(abs(ts - event_ts) < COOLDOWN for event_ts in neighbors)


def development_quartiles(df: pl.DataFrame) -> dict[str, list[float]]:
    start, end = split_bounds(df, "development")
    base = df.filter(
        (pl.col("ts") >= start)
        & (pl.col("ts") < end)
        & pl.col("realized_vol_20d").is_not_null()
        & pl.col("trail_abs_1h").is_not_null()
        & (pl.col("trail_abs_n") == 12)
    )
    return {
        column: [float(base[column].quantile(q, interpolation="linear")) for q in (0.25, 0.5, 0.75)]
        for column in ("realized_vol_20d", "trail_abs_1h")
    }


def quartile(value: float, boundaries: list[float]) -> int:
    return bisect.bisect_right(boundaries, value)


def matched_controls(
    df: pl.DataFrame,
    events: list[dict],
    raw_event_ts: list[datetime],
    split: str,
    quartiles: dict[str, list[float]],
) -> list[dict]:
    start, end = split_bounds(df, split)
    raw_sorted = sorted(raw_event_ts)
    pool = df.filter(
        (pl.col("ts") >= start)
        & (pl.col("ts") < end - timedelta(hours=4, minutes=5))
        & pl.col("realized_vol_20d").is_not_null()
        & pl.col("return_1h").is_not_null()
        & pl.col("trail_abs_1h").is_not_null()
        & (pl.col("trail_abs_n") == 12)
    ).select("ts", "return_1h", "realized_vol_20d", "trail_abs_1h")
    strata: dict[tuple[int, int, int, int], list[dict]] = defaultdict(list)
    for row in pool.to_dicts():
        ts = row["ts"]
        if not _near_raw_event(ts, raw_sorted):
            key = (
                ts.weekday(),
                ts.hour,
                quartile(row["realized_vol_20d"], quartiles["realized_vol_20d"]),
                quartile(row["trail_abs_1h"], quartiles["trail_abs_1h"]),
            )
            strata[key].append(row)

    used: list[datetime] = []
    controls: list[dict] = []
    for event in events:
        ets = event["ts"]
        key = (
            ets.weekday(),
            ets.hour,
            quartile(event["realized_vol_20d"], quartiles["realized_vol_20d"]),
            quartile(event["trail_abs_1h"], quartiles["trail_abs_1h"]),
        )
        candidates = []
        for row in strata[key]:
            if any(abs(row["ts"] - selected) < COOLDOWN for selected in used):
                continue
            time_distance = abs(row["ts"] - ets)
            future_tie = row["ts"] > ets
            candidates.append((time_distance, future_tie, row["ts"], row))
        if candidates:
            _, _, _, chosen = min(candidates, key=lambda item: (item[0], item[1], item[2]))
            controls.append(
                {
                    **chosen,
                    "matched_event_ts": ets,
                }
            )
            bisect.insort(used, chosen["ts"])
    return controls


def paired_control_summary(event_trades: list[dict], control_trades: list[dict], controls: list[dict]) -> dict:
    event_by_ts = {trade["event_ts"]: trade for trade in event_trades if trade["gross_return"] is not None}
    control_by_ts = {trade["event_ts"]: trade for trade in control_trades if trade["gross_return"] is not None}
    differences: list[float] = []
    clusters: list[str] = []
    for control in controls:
        event = event_by_ts.get(control["matched_event_ts"])
        control_trade = control_by_ts.get(control["ts"])
        if event is None or control_trade is None:
            continue
        # Equal round-trip costs cancel in the paired difference.
        differences.append(event["gross_return"] - control_trade["gross_return"])
        clusters.append(iso_week(event["entry_ts"]))
    ci = cluster_bootstrap_ci(differences, clusters) if differences else [float("nan"), float("nan")]
    return {
        "matched_n": len(differences),
        "coverage": len(differences) / len(event_trades) if event_trades else 0.0,
        "mean_event_minus_control_bps": mean(differences) * 10_000 if differences else float("nan"),
        "bootstrap_95_bps": [bound * 10_000 for bound in ci],
    }


def json_dump(path: Path, value) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n")


def evaluate_splits(
    df: pl.DataFrame,
    raw_events: list[dict],
    raw_event_ts: list[datetime],
    split_names: tuple[str, ...],
    quartiles: dict[str, list[float]],
) -> tuple[dict, list[dict], list[dict]]:
    report: dict[str, object] = {"splits": {}}
    all_trade_rows: list[dict] = []
    controls_all: list[dict] = []
    primary_by_split: dict[str, list[dict]] = {}
    common_by_split: dict[str, list[dict]] = {}
    b3 = next(spec for spec in SPECS if spec.barrier_id == "B3")

    for split in split_names:
        common = valid_common_events(df, raw_events, split)
        common_by_split[split] = common
        split_report: dict[str, object] = {"common_event_n": len(common), "barriers": {}}
        for spec in SPECS:
            trades = simulate_events(df, common, spec)
            all_trade_rows.extend({**trade, "split": split} for trade in trades)
            barrier_report = {f"cost_{cost:g}bps": summary(trades, cost) for cost in COSTS}
            if spec.barrier_id == "B3":
                primary_by_split[split] = trades
                for policy in ("take_profit", "exclude"):
                    bound_trades = simulate_events(df, common, spec, policy=policy)
                    all_trade_rows.extend({**trade, "split": split} for trade in bound_trades)
                    barrier_report[f"ambiguous_{policy}_cost_15bps"] = summary(bound_trades, 15)
            split_report["barriers"][spec.barrier_id] = barrier_report
        report["splits"][split] = split_report

    for split in split_names:
        controls = matched_controls(df, common_by_split[split], raw_event_ts, split, quartiles)
        controls_all.extend({**control, "split": split} for control in controls)
        control_trades = simulate_events(df, controls, b3, source="control")
        all_trade_rows.extend({**trade, "split": split} for trade in control_trades)
        report["splits"][split]["matched_control"] = paired_control_summary(
            primary_by_split[split], control_trades, controls
        )
    return report, all_trade_rows, controls_all


def write_phase_outputs(
    out: Path,
    phase: str,
    report: dict,
    trades: list[dict],
    controls: list[dict],
    manifest: dict,
) -> None:
    report_path = out / f"{phase}_report.json"
    pl.DataFrame(trades).write_parquet(out / f"{phase}_trades.parquet")
    pl.DataFrame(controls).write_parquet(out / f"{phase}_matched_controls.parquet")
    json_dump(report_path, report)
    manifest["output_report_sha256"] = sha256(report_path)
    json_dump(out / f"{phase}_manifest.json", manifest)


def execute_validation(out: Path) -> None:
    manifest_path = out / "validation_manifest.json"
    if manifest_path.exists():
        raise SystemExit(f"Validationは実行済みです。再実行しません: {manifest_path}")
    out.mkdir(parents=True, exist_ok=True)
    df = load_frame()
    raw_events, raw_event_ts = h6_events(df)
    quartiles = development_quartiles(df)
    report, trades, controls = evaluate_splits(
        df, raw_events, raw_event_ts, ("development", "validation"), quartiles
    )
    report.update(
        {
            "protocol": "docs/findings/2026-08-16-first-touch-v1-protocol.md",
            "raw_h6_n": len(raw_event_ts),
            "development_quartiles": quartiles,
        }
    )
    val = report["splits"]["validation"]
    primary = val["barriers"]["B3"]["cost_15bps"]
    control = val["matched_control"]
    positive_barriers = sum(
        val["barriers"][spec.barrier_id]["cost_15bps"]["mean_net_bps"] > 0 for spec in SPECS
    )
    checks = {
        "sample_size": primary["n"] >= 150 and primary["week_clusters"] >= 24,
        "primary_positive": primary["mean_net_bps"] > 0,
        "matched_control": control["coverage"] >= 0.8
        and control["mean_event_minus_control_bps"] > 0,
        "lomo_all_positive": primary["lomo_min_bps"] > 0,
        "barrier_plateau": positive_barriers >= 4,
        "pessimistic_ambiguity": True,
    }
    report["validation_decision"] = {
        "checks": checks,
        "positive_barriers": positive_barriers,
        "pass_to_final": all(checks.values()),
        "outcome": "open_final_once" if all(checks.values()) else "rejected_end_ohlcv_direction_search",
    }
    manifest = {
        "executed_at": datetime.now(UTC),
        "features_sha256": sha256(FEATURES),
        "ohlcv_sha256": sha256(OHLCV),
        "protocol_sha256": sha256(Path(report["protocol"])),
        "raw_h6_n": len(raw_event_ts),
        "split_event_n": {
            split: report["splits"][split]["common_event_n"] for split in ("development", "validation")
        },
        "validation_decision": report["validation_decision"],
    }
    write_phase_outputs(out, "validation", report, trades, controls, manifest)
    print(json.dumps(report["validation_decision"], ensure_ascii=False, indent=2))


def execute_final(out: Path) -> None:
    final_manifest = out / "final_manifest.json"
    if final_manifest.exists():
        raise SystemExit(f"Finalは実行済みです。再実行しません: {final_manifest}")
    validation_manifest_path = out / "validation_manifest.json"
    validation_report_path = out / "validation_report.json"
    if not validation_manifest_path.exists() or not validation_report_path.exists():
        raise SystemExit("先にValidationを実行してください。")
    validation_manifest = json.loads(validation_manifest_path.read_text())
    validation_report = json.loads(validation_report_path.read_text())
    if not validation_manifest["validation_decision"]["pass_to_final"]:
        raise SystemExit("Validation落ちのためFinalは開きません。")

    df = load_frame()
    raw_events, raw_event_ts = h6_events(df)
    quartiles = validation_report["development_quartiles"]
    report, trades, controls = evaluate_splits(df, raw_events, raw_event_ts, ("final",), quartiles)
    report.update(
        {
            "protocol": validation_report["protocol"],
            "raw_h6_n": len(raw_event_ts),
            "validation_report_sha256": sha256(validation_report_path),
        }
    )
    val = validation_report["splits"]["validation"]
    final = report["splits"]["final"]
    val_primary = val["barriers"]["B3"]["cost_15bps"]
    final_primary = final["barriers"]["B3"]["cost_15bps"]
    val_control = val["matched_control"]
    final_control = final["matched_control"]
    positive_barriers = sum(
        val["barriers"][spec.barrier_id]["cost_15bps"]["mean_net_bps"] > 0
        and final["barriers"][spec.barrier_id]["cost_15bps"]["mean_net_bps"] > 0
        for spec in SPECS
    )
    checks = {
        "sample_size": all(
            item["n"] >= 150 and item["week_clusters"] >= 24
            for item in (val_primary, final_primary)
        ),
        "validation_final_positive": val_primary["mean_net_bps"] > 0
        and final_primary["mean_net_bps"] > 0,
        "final_ci_lower_positive": final_primary["bootstrap_95_bps"][0] > 0,
        "matched_control": val_control["coverage"] >= 0.8
        and final_control["coverage"] >= 0.8
        and val_control["mean_event_minus_control_bps"] > 0
        and final_control["mean_event_minus_control_bps"] > 0
        and final_control["bootstrap_95_bps"][0] > 0,
        "lomo_all_positive": val_primary["lomo_min_bps"] > 0
        and final_primary["lomo_min_bps"] > 0,
        "barrier_plateau": positive_barriers >= 4,
        "pessimistic_ambiguity": True,
    }
    report["final_decision"] = {
        "checks": checks,
        "positive_barriers_validation_and_final": positive_barriers,
        "provisional_pass": all(checks.values()),
        "outcome": "provisional_pass" if all(checks.values()) else "rejected_end_ohlcv_direction_search",
    }
    manifest = {
        "executed_at": datetime.now(UTC),
        "features_sha256": sha256(FEATURES),
        "ohlcv_sha256": sha256(OHLCV),
        "protocol_sha256": sha256(Path(report["protocol"])),
        "raw_h6_n": len(raw_event_ts),
        "split_event_n": {"final": final["common_event_n"]},
        "final_decision": report["final_decision"],
    }
    write_phase_outputs(out, "final", report, trades, controls, manifest)
    print(json.dumps(report["final_decision"], ensure_ascii=False, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description="凍結済みfirst-touch v1を一度だけ実行")
    parser.add_argument("phase", choices=("validation", "final"))
    parser.add_argument("--output", type=Path, default=OUT)
    args = parser.parse_args()
    if args.phase == "validation":
        execute_validation(args.output)
    else:
        execute_final(args.output)


if __name__ == "__main__":
    main()

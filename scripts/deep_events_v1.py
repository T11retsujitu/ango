"""Deep-event exploration with a hard train/holdout boundary.

The exploration phase never evaluates rows on or after 2025-07-01.  It writes
all candidate results plus, at most, the five candidates that pass the frozen
gates in docs/findings/2026-08-16-deep-events-v1-protocol.md.

Usage:
    uv run python scripts/deep_events_v1.py explore
    uv run python scripts/deep_events_v1.py holdout --frozen <json>
"""

from __future__ import annotations

import argparse
import csv
import json
import math
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import mean, stdev

import polars as pl


CUTOFF = datetime(2025, 7, 1, tzinfo=timezone.utc)
FEATURES = Path("data/features/okx_BTC-USDT-SWAP_5m.parquet")
OUT_DIR = Path("data/analysis/deep_events_v1")
HORIZONS = {
    "5m": ("fwd_return_5m", 1, 5),
    "1h": ("fwd_return_1h", 12, 60),
    "4h": ("fwd_return_4h", 48, 240),
}


@dataclass(frozen=True)
class Candidate:
    candidate_id: str
    family: str
    condition: str
    side: str
    horizon: str


def _base_frame(start: datetime | None, end: datetime) -> pl.DataFrame:
    scan = pl.scan_parquet(FEATURES).filter(pl.col("ts") < end)
    if start is not None:
        # Keep enough pre-window bars to derive the longest local feature, then
        # trim them only after all trailing features have been calculated.
        scan = scan.filter(pl.col("ts") >= start - timedelta(hours=4))
    df = scan.collect().sort("ts")
    if df.is_empty() or df["ts"].max() >= end:
        raise RuntimeError("time-boundary guard failed")

    close_4h_ago = df.select(
        (pl.col("ts") + pl.duration(hours=4)).alias("ts"),
        pl.col("close").alias("close_4h_ago"),
    )
    df = df.join(close_4h_ago, on="ts", how="left")
    df = df.with_columns(
        (pl.col("close") / pl.col("close_4h_ago") - 1).alias("return_4h"),
        (pl.col("high") - pl.col("low")).alias("bar_range"),
        pl.when(pl.col("high") > pl.col("low"))
        .then((pl.col("close") - pl.col("low")) / (pl.col("high") - pl.col("low")))
        .alias("clv"),
        pl.when(pl.col("high") > pl.col("low"))
        .then((pl.col("high") - pl.max_horizontal("open", "close")) / (pl.col("high") - pl.col("low")))
        .alias("upper_wick_frac"),
        pl.when(pl.col("high") > pl.col("low"))
        .then((pl.min_horizontal("open", "close") - pl.col("low")) / (pl.col("high") - pl.col("low")))
        .alias("lower_wick_frac"),
        (pl.col("return_5m") > 0).cast(pl.Float64).alias("positive_bar"),
    ).with_columns(
        pl.col("bar_range").rolling_mean_by("ts", window_size="100m", closed="left").alias("prior_range20"),
        pl.col("high").rolling_max_by("ts", window_size="1h", closed="left").alias("prior_high_1h"),
        pl.col("low").rolling_min_by("ts", window_size="1h", closed="left").alias("prior_low_1h"),
        pl.col("high").rolling_max_by("ts", window_size="4h", closed="left").alias("prior_high_4h"),
        pl.col("low").rolling_min_by("ts", window_size="4h", closed="left").alias("prior_low_4h"),
        pl.col("positive_bar").rolling_mean_by("ts", window_size="1h", closed="right").alias("positive_frac_1h"),
    ).with_columns(
        (pl.col("return_5m") / pl.col("realized_vol_20d")).alias("z5"),
        (pl.col("return_1h") / (pl.col("realized_vol_20d") * math.sqrt(12))).alias("z1"),
        (pl.col("return_4h") / (pl.col("realized_vol_20d") * math.sqrt(48))).alias("z4"),
        (pl.col("bar_range") / pl.col("prior_range20")).alias("range_ratio20"),
    )
    df = df.drop("close_4h_ago")
    return df if start is None else df.filter(pl.col("ts") >= start)


def _candidate_space() -> list[Candidate]:
    candidates: list[Candidate] = []
    serial = 0

    def add(family: str, condition: str, side: str, horizon: str) -> None:
        nonlocal serial
        serial += 1
        candidates.append(Candidate(f"D{serial:04d}", family, condition, side, horizon))

    # Multi-timeframe displacement, normalized by trailing 20-day 5m volatility.
    for zcol in ("z1", "z4"):
        for z in (1.5, 2.0, 2.5, 3.0):
            for vr in (1.0, 2.0, 3.0, 5.0):
                for rr in (1.0, 2.0):
                    cond = f"abs({zcol}) >= {z} and volume_ratio_20 >= {vr} and range_ratio20 >= {rr}"
                    for mode in ("continuation", "fade"):
                        for horizon in ("1h", "4h"):
                            add("normalized_impulse", cond, f"{mode}:{zcol}", horizon)

    # A single liquidation-like bar.
    for z in (2.0, 3.0, 4.0, 5.0):
        for vr in (2.0, 3.0, 5.0):
            for rr in (2.0, 3.0):
                cond = f"abs(z5) >= {z} and volume_ratio_20 >= {vr} and range_ratio20 >= {rr}"
                for mode in ("continuation", "fade"):
                    for horizon in ("1h", "4h"):
                        add("single_bar_shock", cond, f"{mode}:z5", horizon)

    # Rejection after a normalized hourly impulse. Direction is fixed in each condition.
    for z in (1.5, 2.0, 2.5, 3.0):
        for vr in (2.0, 3.0, 5.0):
            for wick in (0.35, 0.50):
                up = (
                    f"z1 >= {z} and volume_ratio_20 >= {vr} and range_ratio20 >= 2 "
                    f"and upper_wick_frac >= {wick} and clv <= 0.5"
                )
                down = (
                    f"z1 <= -{z} and volume_ratio_20 >= {vr} and range_ratio20 >= 2 "
                    f"and lower_wick_frac >= {wick} and clv >= 0.5"
                )
                for horizon in ("1h", "4h"):
                    add("wick_rejection", up, "short", horizon)
                    add("wick_rejection", up, "long", horizon)
                    add("wick_rejection", down, "long", horizon)
                    add("wick_rejection", down, "short", horizon)

    # Breaks of the previous 1h/4h range; impulse sign determines trade direction.
    for window in ("1h", "4h"):
        for z in (0.5, 1.0, 1.5, 2.0):
            for vr in (1.0, 2.0, 3.0):
                for rr in (1.0, 2.0):
                    cond = (
                        f"((close > prior_high_{window} and z1 >= {z}) or "
                        f"(close < prior_low_{window} and z1 <= -{z})) "
                        f"and volume_ratio_20 >= {vr} and range_ratio20 >= {rr}"
                    )
                    for mode in ("continuation", "fade"):
                        for horizon in ("1h", "4h"):
                            add("range_break", cond, f"{mode}:z1", horizon)

    # Persistent one-hour tape combined with a normalized displacement.
    for frac in (0.75, 10 / 12, 11 / 12):
        for z in (1.0, 1.5, 2.0):
            for vr in (1.0, 2.0, 3.0):
                up = f"positive_frac_1h >= {frac} and z1 >= {z} and volume_ratio_20 >= {vr}"
                down = f"positive_frac_1h <= {1-frac} and z1 <= -{z} and volume_ratio_20 >= {vr}"
                for condition, sign in ((up, "long"), (down, "short")):
                    for mode in ("continuation", "fade"):
                        side = sign if mode == "continuation" else ("short" if sign == "long" else "long")
                        for horizon in ("1h", "4h"):
                            add("persistent_tape", condition, side, horizon)
    return candidates


def _mask(df: pl.DataFrame, condition: str) -> pl.Series:
    # Conditions come only from _candidate_space; SQL parsing keeps their
    # persisted text directly executable without accepting external input.
    return df.select(pl.sql_expr(condition).fill_null(False).alias("m"))["m"]


def _sides(df: pl.DataFrame, side: str) -> pl.Series:
    if side == "long":
        return pl.Series("side", [1.0] * df.height)
    if side == "short":
        return pl.Series("side", [-1.0] * df.height)
    mode, zcol = side.split(":")
    sign = df[zcol].sign()
    return sign if mode == "continuation" else -sign


def _episodes(timestamps: list[datetime]) -> int:
    if not timestamps:
        return 0
    count, previous = 1, timestamps[0]
    for current in timestamps[1:]:
        if current - previous >= timedelta(hours=1):
            count += 1
        previous = current
    return count


def _evaluate(df: pl.DataFrame, candidate: Candidate, thirds: list[datetime]) -> dict[str, object]:
    fwd_col, overlap, horizon_minutes = HORIZONS[candidate.horizon]
    # fwd_* was generated from the full source file. Explicitly require the
    # target timestamp to remain inside this phase's frame, otherwise a label
    # immediately before the cutoff would leak the next partition.
    last_ts = df["ts"].max()
    valid = df.filter(
        pl.col(fwd_col).is_not_null()
        & (pl.col("ts") + pl.duration(minutes=horizon_minutes) <= last_ts)
    )
    selected_base = valid.filter(_mask(valid, candidate.condition))
    selected = selected_base.with_columns(_sides(selected_base, candidate.side).alias("side"))
    n = selected.height
    episodes = _episodes(selected["ts"].to_list())
    if n:
        unconditional = valid[fwd_col].mean()
        outcomes = (selected["side"] * (selected[fwd_col] - unconditional)).to_list()
        effect = mean(outcomes) * 10_000
        effective_n = min(n / overlap, episodes)
        t_value = mean(outcomes) / stdev(outcomes) * math.sqrt(effective_n) if n > 1 and stdev(outcomes) else 0.0
    else:
        effect = 0.0
        effective_n = 0.0
        t_value = 0.0

    sub_effects: list[float | None] = []
    bounds = [valid["ts"].min(), *thirds, valid["ts"].max() + timedelta(minutes=5)]
    for lo, hi in zip(bounds, bounds[1:]):
        sub_base = valid.filter((pl.col("ts") >= lo) & (pl.col("ts") < hi))
        sub = sub_base.filter(_mask(sub_base, candidate.condition))
        if sub.is_empty():
            sub_effects.append(None)
            continue
        sides = _sides(sub, candidate.side)
        sub_effects.append(mean((sides * (sub[fwd_col] - sub_base[fwd_col].mean())).to_list()) * 10_000)
    sign_consistency = sum(value is not None and value > 0 for value in sub_effects)
    passed = n >= 150 and episodes >= 60 and effect >= 15 and t_value >= 2 and sign_consistency >= 2
    return {
        **asdict(candidate),
        "n": n,
        "episodes": episodes,
        "effective_n": effective_n,
        "effect_bps": effect,
        "t": t_value,
        "sub1_bps": sub_effects[0],
        "sub2_bps": sub_effects[1],
        "sub3_bps": sub_effects[2],
        "positive_thirds": sign_consistency,
        "passed": passed,
    }


def explore() -> None:
    df = _base_frame(None, CUTOFF)
    start, end = df["ts"].min(), df["ts"].max()
    span = end - start
    thirds = [start + span / 3, start + span * 2 / 3]
    candidates = _candidate_space()
    results = [_evaluate(df, candidate, thirds) for candidate in candidates]
    results.sort(key=lambda row: (bool(row["passed"]), float(row["t"]), float(row["effect_bps"])), reverse=True)
    frozen = [row for row in results if row["passed"]][:5]

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    with (OUT_DIR / "exploration_all.csv").open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(results[0]))
        writer.writeheader()
        writer.writerows(results)
    with (OUT_DIR / "frozen_candidates.json").open("w") as f:
        json.dump(frozen, f, ensure_ascii=False, indent=2, default=str)
    print(json.dumps({"rows": df.height, "start": start, "end": end, "candidates": len(results), "passed": sum(bool(r["passed"]) for r in results), "frozen": frozen}, ensure_ascii=False, indent=2, default=str))


def holdout(frozen_path: Path) -> None:
    with frozen_path.open() as f:
        frozen_rows = json.load(f)
    if not frozen_rows:
        raise SystemExit("凍結候補が0件のため、ホールドアウトは開きません。")
    candidates = [Candidate(*(row[key] for key in ("candidate_id", "family", "condition", "side", "horizon"))) for row in frozen_rows]
    df = _base_frame(CUTOFF, datetime.max.replace(tzinfo=timezone.utc))
    start, end = df["ts"].min(), df["ts"].max()
    span = end - start
    thirds = [start + span / 3, start + span * 2 / 3]
    results = [_evaluate(df, candidate, thirds) for candidate in candidates]
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    with (OUT_DIR / "holdout_results.json").open("w") as f:
        json.dump(results, f, ensure_ascii=False, indent=2, default=str)
    print(json.dumps({"rows": df.height, "start": start, "end": end, "results": results}, ensure_ascii=False, indent=2, default=str))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("phase", choices=("explore", "holdout"))
    parser.add_argument("--frozen", type=Path)
    args = parser.parse_args()
    if args.phase == "explore":
        explore()
    elif args.frozen is None:
        parser.error("holdout requires --frozen")
    else:
        holdout(args.frozen)


if __name__ == "__main__":
    main()

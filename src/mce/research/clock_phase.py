"""Phase 1B — Clock Phase / Quarter-Hour スクリーニング(凍結プロトコル:
docs/phase1/phase1b_protocol.md)。

    python -m mce.research.clock_phase

research 区間の 5 分足について、phase family(minute_mod_15 / minute_mod_60)ごとに
- directional: mean fwd_return_5m(境界バー close → 次バーの執行可能な向き)
- activity:   mean |return_5m| / mean volume
を全 phase 候補について選別なしで計算し、permutation(max統計・FWER 制御)と
凍結判定 D1 で「clock-anchored な方向性構造」の有無を機械判定する。

placebo は (1) 全シフト候補の同時報告、(2) random phase permutation の両方。
"""

import argparse
import json
import math
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import numpy as np
import polars as pl

from mce import config, experiments
from mce.backtest import data as btdata
from mce.backtest import splits
from mce.manifest import sha256_file

PROTOCOL = "phase1b_v1"
DIRECTIONAL_LABEL = "fwd_return_5m"
EXPECTANCY_LABEL = "fwd_open_return_1h"

# 凍結判定閾値(docs/phase1/phase1b_protocol.md §5)
T_DIRECTIONAL = 3.0
P_DIRECTIONAL = 0.01
MIN_EFFECT_BPS = 1.0
T_ACTIVITY = 5.0

# family 名 → 真の境界 phase
FAMILIES = {"minute_mod_15": 0, "minute_mod_60": 0}
DESCRIPTIVE_FAMILIES = ("hour_utc", "weekday_utc")


@dataclass(frozen=True)
class ClockPhaseConfig:
    n_permutations: int = 500
    seed: int = 20260817
    start: datetime = splits.RESEARCH_START
    end: datetime = splits.VALIDATION_START
    features_path: Path | None = None
    labels_path: Path | None = None


def _welch_t(a: np.ndarray, b: np.ndarray) -> float:
    va, vb = a.var(ddof=1), b.var(ddof=1)
    denom = math.sqrt(va / len(a) + vb / len(b))
    return float((a.mean() - b.mean()) / denom) if denom > 0 else 0.0


def _perm_null_max(y: np.ndarray, labels: np.ndarray, n_perm: int, seed: int) -> np.ndarray:
    """random-phase permutation の帰無分布(全候補の効果量の max)。決定的(seed 固定)。"""
    uniq = np.unique(labels)
    idx = np.searchsorted(uniq, labels)
    counts = np.bincount(idx, minlength=len(uniq)).astype(np.float64)
    total, n = y.sum(), float(len(y))
    rng = np.random.default_rng(seed)
    out = np.empty(n_perm)
    for b in range(n_perm):
        yp = rng.permutation(y)
        sums = np.bincount(idx, weights=yp, minlength=len(uniq))
        diffs = sums / counts - (total - sums) / (n - counts)
        out[b] = np.max(np.abs(diffs))
    return out


def _phase_stats(df: pl.DataFrame, family: str, mid_ts: datetime, null_max: np.ndarray | None) -> list[dict]:
    """phase 候補ごとの directional / activity 統計(選別なしで全候補)。"""
    rows = []
    dir_df = df.drop_nulls(subset=[DIRECTIONAL_LABEL])
    act_df = df.drop_nulls(subset=["return_5m"])
    y_dir = dir_df[DIRECTIONAL_LABEL].to_numpy().astype(np.float64)
    lab_dir = dir_df[family].to_numpy()
    y_act = np.abs(act_df["return_5m"].to_numpy().astype(np.float64))
    lab_act = act_df[family].to_numpy()

    for phase in sorted(np.unique(lab_dir).tolist()):
        in_d = lab_dir == phase
        in_a = lab_act == phase
        a_dir, rest_dir = y_dir[in_d], y_dir[~in_d]
        effect = abs(a_dir.mean() - rest_dir.mean())
        # 前半/後半の符号一致
        halves = []
        for cond in [pl.col("ts") < mid_ts, pl.col("ts") >= mid_ts]:
            h = dir_df.filter(cond)
            hy, hl = h[DIRECTIONAL_LABEL].to_numpy(), h[family].to_numpy()
            m_in, m_out = hy[hl == phase], hy[hl != phase]
            halves.append(float(m_in.mean() - m_out.mean()) if len(m_in) and len(m_out) else 0.0)
        sign_consistent = bool(halves[0] * halves[1] > 0)

        row = {
            "phase": int(phase),
            "n": int(in_d.sum()),
            "fwd5m_mean_bps": round(float(a_dir.mean()) * 1e4, 4),
            "fwd5m_effect_bps": round(effect * 1e4, 4),
            "fwd5m_t": round(_welch_t(a_dir, rest_dir), 3),
            "half_effects_bps": [round(h * 1e4, 4) for h in halves],
            "sign_consistent": sign_consistent,
            "abs_ret_mean_bps": round(float(y_act[in_a].mean()) * 1e4, 4),
            "abs_ret_t": round(_welch_t(y_act[in_a], y_act[~in_a]), 3),
            "volume_mean": round(float(act_df.filter(pl.col(family) == phase)["volume"].mean()), 4),
            "fwd1h_open_mean_bps": _mean_bps(df, phase, family, EXPECTANCY_LABEL),
        }
        if null_max is not None:
            row["perm_p"] = round(float((1 + (null_max >= effect).sum()) / (len(null_max) + 1)), 5)
            row["D1_pass"] = bool(
                abs(row["fwd5m_t"]) >= T_DIRECTIONAL
                and row["perm_p"] < P_DIRECTIONAL
                and row["fwd5m_effect_bps"] >= MIN_EFFECT_BPS
                and sign_consistent
            )
            row["activity_confirmed"] = bool(abs(row["abs_ret_t"]) >= T_ACTIVITY)
        rows.append(row)
    return rows


def _mean_bps(df: pl.DataFrame, phase: int, family: str, col: str) -> float | None:
    s = df.filter(pl.col(family) == phase)[col]
    m = s.mean()
    return round(float(m) * 1e4, 4) if m is not None else None


def _family_judgment(rows: list[dict], true_boundary: int) -> dict:
    """効果量最大の phase(argmax)が真の境界であり、かつ D1 を通過することを要求する。

    vs-rest 対比は補集合効果を持つ(ある phase が持ち上がると他 phase の
    「対 rest」差も自動的に生じる)ため、「他候補が通らないこと」ではなく
    「最大効果の位置が境界であること」で clock-anchored を判定する。"""
    top = max(rows, key=lambda r: r["fwd5m_effect_bps"])
    passing = [r["phase"] for r in rows if r.get("D1_pass")]
    if top.get("D1_pass") and top["phase"] == true_boundary:
        verdict = "clock_anchored_directional"
    elif top.get("D1_pass"):
        verdict = "directional_at_shifted_phase"
    else:
        verdict = "no_directional_structure"
    return {
        "true_boundary": true_boundary,
        "top_phase": top["phase"],
        "top_effect_bps": top["fwd5m_effect_bps"],
        "D1_passing_phases": passing,
        "activity_confirmed_phases": [r["phase"] for r in rows if r.get("activity_confirmed")],
        "verdict": verdict,
    }


def run(cfg: ClockPhaseConfig) -> dict:
    features = btdata.load_features("research", path=cfg.features_path)
    features = features.filter((pl.col("ts") >= cfg.start) & (pl.col("ts") < cfg.end))
    labels_path = cfg.labels_path if cfg.labels_path is not None else config.labels_parquet()
    labels = pl.read_parquet(labels_path).select("ts", DIRECTIONAL_LABEL, EXPECTANCY_LABEL)
    df = features.join(labels, on="ts", how="left")  # 評価専用 join(strategy には渡さない)
    if df.height == 0:
        raise ValueError("research 区間にデータがない")
    mid_ts = cfg.start + (cfg.end - cfg.start) / 2

    families = {}
    for family, boundary in FAMILIES.items():
        dir_df = df.drop_nulls(subset=[DIRECTIONAL_LABEL])
        null_max = _perm_null_max(
            dir_df[DIRECTIONAL_LABEL].to_numpy().astype(np.float64),
            dir_df[family].to_numpy(),
            cfg.n_permutations,
            cfg.seed,
        )
        rows = _phase_stats(df, family, mid_ts, null_max)
        families[family] = {"phases": rows, "judgment": _family_judgment(rows, boundary)}

    for family in DESCRIPTIVE_FAMILIES:
        families[family] = {"phases": _phase_stats(df, family, mid_ts, None), "judgment": None}

    families["m15_x_vol_regime"] = _vol_regime_interaction(df)

    verdicts = {f: families[f]["judgment"]["verdict"] for f in FAMILIES}
    report = {
        "protocol": PROTOCOL,
        "replication_class": "cross_exchange_validation",
        "reference": "The Quarter-Hour Effect (arXiv:2607.09426; 原論文はBinance perp・1分足を含む)",
        "config": {
            "n_permutations": cfg.n_permutations,
            "seed": cfg.seed,
            "start": cfg.start.isoformat(),
            "end": cfg.end.isoformat(),
            "thresholds": {
                "t_directional": T_DIRECTIONAL,
                "p_directional": P_DIRECTIONAL,
                "min_effect_bps": MIN_EFFECT_BPS,
                "t_activity": T_ACTIVITY,
            },
        },
        "source_commit": experiments.git_commit_hash(),
        "manifest_sha256": _hashes(cfg),
        "families": families,
        "judgment": {
            "family_verdicts": verdicts,
            "proceed_to_aggtrades_on_directional": any(v == "clock_anchored_directional" for v in verdicts.values()),
            "economic_note": "D1通過効果もmaker往復2bps/taker往復10bpsと必ず比較すること(恒久ルール5)",
        },
    }
    return report


def _vol_regime_interaction(df: pl.DataFrame) -> dict:
    """m15 × volatility regime の記述統計(in-sample 中央値2分。判定なし)。"""
    base = df.drop_nulls(subset=[DIRECTIONAL_LABEL, "realized_vol_20d"])
    if base.height == 0:
        return {"note": "realized_vol_20d が全て null", "cells": []}
    med = base["realized_vol_20d"].median()
    cells = []
    for regime, cond in [("high_vol", pl.col("realized_vol_20d") >= med), ("low_vol", pl.col("realized_vol_20d") < med)]:
        sub = base.filter(cond)
        for phase in [0, 5, 10]:
            g = sub.filter(pl.col("minute_mod_15") == phase)
            if g.height == 0:
                continue
            cells.append(
                {
                    "regime": regime,
                    "phase": phase,
                    "n": g.height,
                    "fwd5m_mean_bps": round(float(g[DIRECTIONAL_LABEL].mean()) * 1e4, 4),
                    "abs_ret_mean_bps": round(float(g["return_5m"].abs().mean()) * 1e4, 4),
                }
            )
    return {"note": "in-sample中央値2分の記述統計(walk-forwardではない)", "vol_median": float(med), "cells": cells}


def _hashes(cfg: ClockPhaseConfig) -> dict:
    out = {}
    fp = cfg.features_path if cfg.features_path is not None else config.features_parquet()
    lp = cfg.labels_path if cfg.labels_path is not None else config.labels_parquet()
    for name, p in [("features", fp), ("labels", lp)]:
        if p is not None and Path(p).exists():
            out[name] = sha256_file(Path(p))
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description="Phase 1B clock-phase スクリーニング(凍結プロトコル)")
    parser.add_argument("--permutations", type=int, default=500)
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()

    report = run(ClockPhaseConfig(n_permutations=args.permutations))

    out = args.out or (Path("experiments") / "phase1b" / "clock_phase.json")
    if out.exists():
        raise SystemExit(f"{out} は既に存在する(実験記録は追記専用。再実行するなら別名を --out で指定)")
    out.parent.mkdir(parents=True, exist_ok=True)
    record = {"created_at": datetime.now().astimezone().isoformat(), **report}
    out.write_text(json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    for family in FAMILIES:
        fam = report["families"][family]
        print(f"== {family} (真の境界=0) ==")
        for r in fam["phases"]:
            print(
                f"  phase {r['phase']:>2}: n={r['n']:>7} fwd5m {r['fwd5m_mean_bps']:+7.3f}bps "
                f"(effect {r['fwd5m_effect_bps']:.3f}, t={r['fwd5m_t']:+6.2f}, p={r.get('perm_p')}) "
                f"|ret| {r['abs_ret_mean_bps']:7.2f}bps (t={r['abs_ret_t']:+7.2f}) "
                f"D1={'✓' if r.get('D1_pass') else '-'}"
            )
        print(f"  verdict: {fam['judgment']['verdict']}")
    print("judgment:", json.dumps(report["judgment"], ensure_ascii=False))
    print(f"report -> {out}")


if __name__ == "__main__":
    main()

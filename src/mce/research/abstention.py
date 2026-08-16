"""Phase 1A — Cost-Aware Abstention 実験(凍結プロトコル: docs/phase1/phase1a_protocol.md)。

    python -m mce.research.abstention --cost base_taker
    python -m mce.research.abstention --cost maker_low

walk-forward の各 fold で:
  train 窓(embargo 付き)で LogReg を学習
    → test 窓の observable features から予測エッジ(bps)を算出
    → 4 arm(model_sign / abstention / random_abstention / buy_and_hold)を
      同一の Phase 0 Judge(execution + cost + metrics)で評価
し、凍結済み判定基準(J1–J3)で abstention の価値を機械判定する。

leakage 防御:
- features は backtest loader(fwd_ 列拒否)経由でしか読まない
- ラベルは train 窓の学習にのみ join し、モデルの predict 入力は observable のみ
- 標準化統計・エッジ換算係数は train 窓のみから計算
- train 窓末尾はラベル horizon ぶん embargo(ラベルが test 窓を覗かない)
"""

import argparse
import json
import random
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import polars as pl

from mce import config, experiments
from mce.backtest import data as btdata
from mce.backtest import splits
from mce.backtest.baselines import buy_and_hold, cost_aware_target
from mce.backtest.costs import SCENARIOS
from mce.backtest.engine import StrategySpec, run_backtest
from mce.manifest import sha256_file
from mce.research.logistic import fit_logistic, predict_proba

PROTOCOL = "phase1a_v1"
LABEL = "fwd_open_return_1h"
HORIZON_BARS = 12
EMBARGO_MINUTES = 5 * (HORIZON_BARS + 2)  # ラベルが train_end を跨がないための余白(70分)

# 凍結: モデル入力(observable のみ。変更は v2 プロトコルとして再凍結)
MODEL_FEATURES = ("return_5m", "return_1h", "volume_ratio_20", "drift_20d", "realized_vol_20d")

# arm ごとに記録する metrics
_KEEP_METRICS = (
    "total_return", "gross_total_return", "turnover_total", "trade_count",
    "sharpe", "exposure", "hit_rate", "break_even_cost_bps",
)


@dataclass(frozen=True)
class AbstentionConfig:
    cost_scenario: str = "base_taker"
    train_days: int = 120
    test_days: int = 30
    l2: float = 1e-3
    seed: int = 20260816
    start: datetime = splits.RESEARCH_START
    end: datetime = splits.VALIDATION_START  # Phase 1A は research 区間のみ(validation は温存)
    model_features: tuple = MODEL_FEATURES
    features_path: Path | None = None
    labels_path: Path | None = None


def _train_frame(df: pl.DataFrame, fold: splits.Fold, model_features: tuple) -> pl.DataFrame:
    cutoff = fold.train_end - timedelta(minutes=EMBARGO_MINUTES)
    return df.filter((pl.col("ts") >= fold.train_start) & (pl.col("ts") < cutoff)).drop_nulls(
        subset=list(model_features) + [LABEL]
    )


def _fit(train_df: pl.DataFrame, model_features: tuple, l2: float) -> dict:
    X = train_df.select(model_features).to_numpy().astype(np.float64)
    mu = X.mean(axis=0)
    sd = X.std(axis=0)
    sd[sd == 0] = 1.0
    label = train_df[LABEL].to_numpy().astype(np.float64)
    y = (label > 0).astype(np.float64)
    w = fit_logistic((X - mu) / sd, y, l2=l2)
    return {
        "w": w,
        "mu": mu,
        "sd": sd,
        "mean_abs_bps": float(np.abs(label).mean() * 1e4),
        "n_train": int(len(y)),
        "share_up": float(y.mean()),
    }


def _edge_bps(model: dict, feats: pl.DataFrame, model_features: tuple) -> pl.Series:
    """test 窓の observable features から予測エッジ(bps)。feature 欠損行は NaN(= abstain)。"""
    arr = feats.select(model_features).to_numpy().astype(np.float64)
    edge = np.full(len(arr), np.nan)
    valid = ~np.isnan(arr).any(axis=1)
    if valid.any():
        p = predict_proba(model["w"], (arr[valid] - model["mu"]) / model["sd"])
        edge[valid] = (2.0 * p - 1.0) * model["mean_abs_bps"]
    return pl.Series("edge_bps", edge)


def _random_abstention(target: list[int], keep_prob: float, seed: int) -> list[int]:
    """model_sign の連続シグナル区間を確率 keep_prob で残す(exposure を揃えた対照)。"""
    rng = random.Random(seed)
    out: list[int] = []
    i, n = 0, len(target)
    while i < n:
        if target[i] == 0:
            out.append(0)
            i += 1
            continue
        j = i
        while j < n and target[j] == target[i]:
            j += 1
        keep = rng.random() < keep_prob
        out.extend(target[i:j] if keep else [0] * (j - i))
        i = j
    return out


def _fixed(name: str, target: pl.Series, seed: int | None = None, params: dict | None = None) -> StrategySpec:
    def fn(df: pl.DataFrame) -> pl.Series:
        if df.height != len(target):
            raise ValueError("target と test 窓の長さが一致しない")
        return target

    return StrategySpec(name, fn, params=params or {}, seed=seed)


def _arm_metrics(feats: pl.DataFrame, strategy: StrategySpec, cost) -> dict:
    m = run_backtest(feats, strategy, cost).metrics
    return {k: m[k] for k in _KEEP_METRICS}


def run(cfg: AbstentionConfig) -> dict:
    cost = SCENARIOS[cfg.cost_scenario]
    threshold_bps = cost.roundtrip_bps  # 凍結: abstention 閾値 = 往復コスト

    features = btdata.load_features("research", path=cfg.features_path)
    features = features.filter((pl.col("ts") >= cfg.start) & (pl.col("ts") < cfg.end))
    labels_path = cfg.labels_path if cfg.labels_path is not None else config.labels_parquet()
    labels = pl.read_parquet(labels_path).select("ts", LABEL)
    joined = features.join(labels, on="ts", how="left")  # train 用のみ。predict には渡さない

    folds = splits.walk_forward_folds(cfg.train_days, cfg.test_days, start=cfg.start, end=cfg.end)
    if not folds:
        raise ValueError("fold が 0 件(train/test 日数と期間を確認)")

    fold_rows: list[dict] = []
    for k, fold in enumerate(folds):
        train_df = _train_frame(joined, fold, cfg.model_features)
        if train_df.height < 100:
            fold_rows.append({"fold": k, "skipped": "train_rows_lt_100", "n_train": train_df.height})
            continue
        model = _fit(train_df, cfg.model_features, cfg.l2)

        test_feats = features.filter((pl.col("ts") >= fold.test_start) & (pl.col("ts") < fold.test_end))
        if test_feats.height == 0:
            fold_rows.append({"fold": k, "skipped": "empty_test_window"})
            continue
        edge = _edge_bps(model, test_feats, cfg.model_features)

        sign_target = cost_aware_target(edge, 0.0)  # 閾値なし = abstention しない対照
        abst_target = cost_aware_target(edge, threshold_bps)

        sign_active = int((sign_target != 0).sum())
        abst_active = int((abst_target != 0).sum())
        keep_prob = (abst_active / sign_active) if sign_active > 0 else 0.0
        rand_target = pl.Series(_random_abstention(sign_target.to_list(), keep_prob, cfg.seed + k), dtype=pl.Int8)

        row = {
            "fold": k,
            "train": [fold.train_start.isoformat(), fold.train_end.isoformat()],
            "test": [fold.test_start.isoformat(), fold.test_end.isoformat()],
            "n_train": model["n_train"],
            "share_up": round(model["share_up"], 4),
            "mean_abs_move_bps": round(model["mean_abs_bps"], 3),
            "keep_prob": round(keep_prob, 4),
            "arms": {
                "model_sign": _arm_metrics(test_feats, _fixed("phase1a_model_sign", sign_target), cost),
                "abstention": _arm_metrics(
                    test_feats,
                    _fixed("phase1a_abstention", abst_target, params={"threshold_bps": threshold_bps}),
                    cost,
                ),
                "random_abstention": _arm_metrics(
                    test_feats, _fixed("phase1a_random_abstention", rand_target, seed=cfg.seed + k), cost
                ),
                "buy_and_hold": _arm_metrics(test_feats, buy_and_hold(), cost),
            },
        }
        fold_rows.append(row)

    judgment = _judge([r for r in fold_rows if "arms" in r])

    report = {
        "protocol": PROTOCOL,
        "replication_class": "method_transfer",
        "reference": "Machine Learning-Based Bitcoin Trading Under Transaction Costs (arXiv:2606.00060; 原論文は1時間足)",
        "config": {
            "cost_scenario": cfg.cost_scenario,
            "threshold_bps": threshold_bps,
            "train_days": cfg.train_days,
            "test_days": cfg.test_days,
            "horizon_bars": HORIZON_BARS,
            "embargo_minutes": EMBARGO_MINUTES,
            "label": LABEL,
            "model_features": list(cfg.model_features),
            "l2": cfg.l2,
            "seed": cfg.seed,
            "start": cfg.start.isoformat(),
            "end": cfg.end.isoformat(),
        },
        "source_commit": experiments.git_commit_hash(),
        "manifest_sha256": _hashes(cfg),
        "folds": fold_rows,
        "judgment": judgment,
    }
    return report


def _hashes(cfg: AbstentionConfig) -> dict:
    out = {}
    fp = cfg.features_path if cfg.features_path is not None else config.features_parquet()
    lp = cfg.labels_path if cfg.labels_path is not None else config.labels_parquet()
    for name, p in [("features", fp), ("labels", lp)]:
        if p is not None and Path(p).exists():
            out[name] = sha256_file(Path(p))
    return out


def _judge(rows: list[dict]) -> dict:
    """凍結判定(docs/phase1/phase1a_protocol.md §6)。実行前に定義、結果での変更禁止。"""
    if not rows:
        return {"verdict": "no_valid_folds"}

    def frac(pred) -> float:
        return sum(1 for r in rows if pred(r)) / len(rows)

    net = lambda r, a: r["arms"][a]["total_return"]
    turn = lambda r, a: r["arms"][a]["turnover_total"]

    j1_frac = frac(lambda r: turn(r, "abstention") < turn(r, "model_sign"))
    turn_ratios = [turn(r, "abstention") / turn(r, "model_sign") for r in rows if turn(r, "model_sign") > 0]
    j2_frac = frac(lambda r: net(r, "abstention") > net(r, "model_sign"))
    j2_med = statistics.median(net(r, "abstention") - net(r, "model_sign") for r in rows)
    j3_frac = frac(lambda r: net(r, "abstention") > net(r, "random_abstention"))
    total_abst_trades = sum(r["arms"]["abstention"]["trade_count"] for r in rows)
    abst_net_positive_frac = frac(lambda r: net(r, "abstention") > 0)

    j1 = j1_frac >= 0.8
    j2 = j2_frac >= 2 / 3 and j2_med > 0
    j3 = j3_frac >= 2 / 3
    if total_abst_trades < 30:
        verdict = "insufficient_trades"
    elif j1 and j2 and j3:
        verdict = "abstention_supported"
    else:
        verdict = "abstention_rejected"

    return {
        "folds_used": len(rows),
        "J1_turnover_reduced_frac": round(j1_frac, 4),
        "J1_median_turnover_ratio": round(statistics.median(turn_ratios), 4) if turn_ratios else None,
        "J2_net_improved_frac": round(j2_frac, 4),
        "J2_median_net_improvement": j2_med,
        "J3_beats_random_frac": round(j3_frac, 4),
        "abstention_total_trades": total_abst_trades,
        "abstention_net_positive_frac": round(abst_net_positive_frac, 4),
        "J1_pass": j1,
        "J2_pass": j2,
        "J3_pass": j3,
        "verdict": verdict,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Phase 1A cost-aware abstention 実験(凍結プロトコル)")
    parser.add_argument("--cost", default="base_taker", choices=sorted(SCENARIOS))
    parser.add_argument("--out", type=Path, default=None, help="出力 JSON(既定: experiments/phase1a/phase1a_<cost>.json)")
    args = parser.parse_args()

    cfg = AbstentionConfig(cost_scenario=args.cost)
    report = run(cfg)

    out = args.out or (Path("experiments") / "phase1a" / f"phase1a_{args.cost}.json")
    if out.exists():
        raise SystemExit(f"{out} は既に存在する(実験記録は追記専用。再実行するなら別名を --out で指定)")
    out.parent.mkdir(parents=True, exist_ok=True)
    record = {"created_at": datetime.now().astimezone().isoformat(), **report}
    out.write_text(json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    print(f"folds={report['judgment'].get('folds_used', 0)} cost={args.cost} threshold={report['config']['threshold_bps']}bps")
    for r in report["folds"]:
        if "arms" not in r:
            print(f"  fold {r['fold']}: skipped ({r.get('skipped')})")
            continue
        a, s, rd = r["arms"]["abstention"], r["arms"]["model_sign"], r["arms"]["random_abstention"]
        print(
            f"  fold {r['fold']:>2} {r['test'][0][:10]}: "
            f"net abst {a['total_return']:+.4f} / sign {s['total_return']:+.4f} / rand {rd['total_return']:+.4f}  "
            f"turnover {a['turnover_total']:.0f}/{s['turnover_total']:.0f}  trades {a['trade_count']}"
        )
    print("judgment:", json.dumps(report["judgment"], ensure_ascii=False))
    print(f"report -> {out}")


if __name__ == "__main__":
    main()

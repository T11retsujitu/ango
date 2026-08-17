"""Phase 3 bakeoff の cross-arm 集計(決定論的・artifact 読み取り専用)。

    python -m mce.phase3_summary                          # markdown 表を stdout へ
    python -m mce.phase3_summary --json <path>            # 機械可読 summary も書き出す

入力は凍結済み run artifact(`experiments/phase3/<arm>/summary.json` と
`candidates.jsonl`)だけで、backtest の再実行・再評価は一切行わない。
主指標(validation 生存 / evaluations)と副次指標(valid rate・duplicate rate・
コスト後 net 分布など)を分離して出す。「一番良かった Sharpe だけ報告する」を
避けるため、全 counter と分布統計を機械的に含める。
"""

import argparse
import json
import statistics
from pathlib import Path

PHASE3_DIR = Path("experiments") / "phase3"
PRIMARY_COST = "base_taker"
SECONDARY_COST = "maker_low"

# 表示順(bakeoff の論理順)。ここに無い arm は名前順で後ろへ付ける
ARM_ORDER = ("random", "genetic", "llm", "baselines")

# LLM の提案文が「OHLCV に集約される前の実体」に言及しているかの機械的スキャン。
# 事後観察であり事前登録された指標ではない(findings に明記する)。語彙は固定。
MECHANISM_KEYWORDS = (
    "absorb",
    "absorption",
    "aggressive",
    "book",
    "depth",
    "forced",
    "funding",
    "impact",
    "inventory",
    "liquidation",
    "maker",
    "market maker",
    "open interest",
    "order book",
    "order flow",
    "order-flow",
    "orderflow",
    "passive",
    "queue",
    "spread",
    "stop",
    "taker",
)

COUNTER_KEYS = (
    "candidate_count",
    "rejected_candidate_count",
    "duplicate_count",
    "runtime_failure_count",
    "evaluated_count",
    "research_pass_count",
    "validation_count",
    "survivor_count",
)


def _round(value: float | None, digits: int = 4) -> float | None:
    return None if value is None else round(float(value), digits)


def _median(values: list[float]) -> float | None:
    return _round(statistics.median(values)) if values else None


def _side(ast: dict | None) -> str:
    if not ast:
        return "unknown"
    has_long = ast.get("long_if") is not None
    has_short = ast.get("short_if") is not None
    if has_long and has_short:
        return "both"
    if has_long:
        return "long"
    if has_short:
        return "short"
    return "none"


def _metrics(record: dict, cost: str) -> dict:
    return (record.get("research") or {}).get(cost) or {}


def arm_stats(run_dir: Path) -> dict:
    """1 arm の counters + 副次指標。artifact の値をそのまま集計する(再評価しない)。"""
    summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    records = [
        json.loads(line)
        for line in (run_dir / "candidates.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    evaluated = [r for r in records if r.get("status") == "evaluated"]

    counters = summary["counters"]
    draws = counters["candidate_count"]
    primary_net = [_metrics(r, PRIMARY_COST).get("total_return") for r in evaluated]
    primary_net = [x for x in primary_net if x is not None]
    secondary_net = [_metrics(r, SECONDARY_COST).get("total_return") for r in evaluated]
    secondary_net = [x for x in secondary_net if x is not None]
    trades = [_metrics(r, PRIMARY_COST).get("trade_count") for r in evaluated]
    trades = [x for x in trades if x is not None]
    turnover = [_metrics(r, PRIMARY_COST).get("turnover_total") for r in evaluated]
    turnover = [x for x in turnover if x is not None]
    exposure = [_metrics(r, PRIMARY_COST).get("exposure") for r in evaluated]
    exposure = [x for x in exposure if x is not None]
    # 実効N(min_trades 30)を満たす候補だけを見た net(no-trade 候補の net=0 を除く)
    active_net = [
        _metrics(r, PRIMARY_COST)["total_return"]
        for r in evaluated
        if (_metrics(r, PRIMARY_COST).get("trade_count") or 0) >= 30
    ]

    sides: dict[str, int] = {}
    for record in evaluated:
        key = _side(record.get("ast"))
        sides[key] = sides.get(key, 0) + 1

    stats = {
        "arm": summary.get("arm_meta", {}).get("arm", summary["method"]),
        "method": summary["method"],
        "protocol": summary["protocol"],
        "run_dir": run_dir.as_posix(),
        "seed": summary["config"]["seed"],
        "budget": summary["config"]["budget"],
        "primary_cost": summary["config"]["primary_cost"],
        "source_commit": summary.get("source_commit"),
        "features_sha256": (summary.get("manifest_sha256") or {}).get("features"),
        "counters": {key: counters[key] for key in COUNTER_KEYS},
        "search_quality": {
            # valid rate = validator を通った draw の割合(duplicate も「有効な提案」に数える)
            "valid_rate": _round(1 - counters["rejected_candidate_count"] / draws) if draws else None,
            "duplicate_rate": _round(counters["duplicate_count"] / draws) if draws else None,
            "unique_per_draw": _round(counters["unique_candidate_count"] / draws) if draws else None,
            "draws_per_evaluation": _round(draws / counters["evaluated_count"])
            if counters["evaluated_count"]
            else None,
        },
        "research_distribution": {
            "n_evaluated": len(evaluated),
            "primary_net_median": _median(primary_net),
            "primary_net_max": _round(max(primary_net)) if primary_net else None,
            "primary_net_min": _round(min(primary_net)) if primary_net else None,
            "primary_net_positive": sum(1 for x in primary_net if x > 0),
            "secondary_net_positive": sum(1 for x in secondary_net if x > 0),
            "active_candidates": len(active_net),  # trade_count >= 30
            "active_net_max": _round(max(active_net)) if active_net else None,
            "active_net_positive": sum(1 for x in active_net if x > 0),
            "trade_count_median": _median(trades),
            "turnover_median": _median(turnover),
            "exposure_median": _median(exposure),
            "sides": dict(sorted(sides.items())),
        },
    }
    transcript = llm_transcript_stats(run_dir)
    if transcript is not None:
        stats["llm_transcript"] = transcript
    return stats


def llm_transcript_stats(run_dir: Path) -> dict | None:
    """LLM arm の transcript(あれば)から提案側の統計を出す。決定論的。"""
    path = run_dir / "llm_transcript.jsonl"
    if not path.exists():
        return None
    calls = [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    proposals = [h for c in calls for h in (c.get("response") or {}).get("hypotheses", [])]
    families = [(h.get("dsl_plan") or {}).get("signal_family") for h in proposals]
    mechanism_hits = sum(
        1
        for h in proposals
        if any(k in (h.get("hypothesis") or "").lower() for k in MECHANISM_KEYWORDS)
    )
    return {
        "api_calls": len(calls),
        "models": sorted({c.get("model") for c in calls if c.get("model")}),
        "proposals": len(proposals),
        "unique_signal_families": len({f for f in families if f}),
        # 提案文が板・約定フロー・OI/liquidation 等へ言及した件数(事後スキャン)
        "mechanism_keyword_hits": mechanism_hits,
        "non_ok_calls": sum(1 for c in calls if c.get("status") != "ok"),
    }


def _order_key(arm: dict) -> tuple[int, str]:
    method = arm["method"]
    rank = ARM_ORDER.index(method) if method in ARM_ORDER else len(ARM_ORDER)
    return rank, arm["run_dir"]


def collect(root: Path = PHASE3_DIR) -> dict:
    """Phase 3 の全 arm を集計する。run ディレクトリの有無だけで決まる決定論的関数。"""
    run_dirs = sorted(p for p in root.iterdir() if (p / "summary.json").exists())
    arms = sorted((arm_stats(p) for p in run_dirs), key=_order_key)
    fingerprints = {a["features_sha256"] for a in arms}
    totals = {key: sum(a["counters"][key] for a in arms) for key in COUNTER_KEYS}
    return {
        "report": "phase3_bakeoff_cross_arm_v1",
        "source": root.as_posix(),
        "arms": arms,
        "totals": totals,
        "consistency": {
            # 全 arm が同一 features manifest で評価されたか(データ差の排除)
            "same_features_manifest": len(fingerprints) == 1,
            "features_sha256": sorted(x for x in fingerprints if x),
            "protocols": sorted({a["protocol"] for a in arms}),
            "source_commits": sorted({a["source_commit"] for a in arms if a["source_commit"]}),
        },
    }


def markdown_table(report: dict) -> str:
    """主指標表(findings に貼る形)。副次指標は別表にする。"""
    lines = [
        "| arm | draw | rejected | duplicate | evaluated | research_pass | validation_survivor |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for arm in report["arms"]:
        c = arm["counters"]
        lines.append(
            f"| {arm['method']} | {c['candidate_count']} | {c['rejected_candidate_count']} | "
            f"{c['duplicate_count']} | {c['evaluated_count']} | {c['research_pass_count']} | "
            f"{c['survivor_count']} |"
        )
    t = report["totals"]
    lines.append(
        f"| **total** | {t['candidate_count']} | {t['rejected_candidate_count']} | "
        f"{t['duplicate_count']} | {t['evaluated_count']} | {t['research_pass_count']} | "
        f"{t['survivor_count']} |"
    )
    lines.append("")
    lines.append(
        "| arm | valid_rate | duplicate_rate | unique/draw | net>0 (taker) | "
        "net>0 (maker_low) | net median | trades median | exposure median |"
    )
    lines.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|")
    for arm in report["arms"]:
        q = arm["search_quality"]
        d = arm["research_distribution"]
        lines.append(
            f"| {arm['method']} | {q['valid_rate']} | {q['duplicate_rate']} | "
            f"{q['unique_per_draw']} | {d['primary_net_positive']}/{d['n_evaluated']} | "
            f"{d['secondary_net_positive']}/{d['n_evaluated']} | {d['primary_net_median']} | "
            f"{d['trade_count_median']} | {d['exposure_median']} |"
        )
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Phase 3 bakeoff cross-arm summary")
    parser.add_argument("--root", type=Path, default=PHASE3_DIR)
    parser.add_argument("--json", type=Path, default=None, help="機械可読 summary の出力先")
    args = parser.parse_args()

    report = collect(args.root)
    print(markdown_table(report))
    if not report["consistency"]["same_features_manifest"]:
        print("\n注意: arm 間で features manifest が一致していない(比較不能の可能性)")
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(
            json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        print(f"\nwrote {args.json}")


if __name__ == "__main__":
    main()

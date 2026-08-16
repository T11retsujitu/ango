"""Baselines arm — 固定 10 戦略を DSL AST として同一パイプラインで評価する
(凍結: docs/phase3/bakeoff_protocol.md §9)。

    python -m mce.search.baselines_arm

探索ではなく参照点。FROZEN_BASELINES の定義が正であり変更しない。
"""

import argparse
import json
from datetime import datetime
from pathlib import Path

from mce.search.runner import Evaluator, SearchConfig


def _g(x, thr):
    return {"op": "greater", "x": x, "threshold": thr}


def _l(x, thr):
    return {"op": "less", "x": x, "threshold": thr}


def _ret(w):
    return {"op": "return", "window": w}


def _mom(w):
    return {"type": "strategy", "long_if": _g(_ret(w), 0.0), "short_if": _l(_ret(w), 0.0)}


FROZEN_BASELINES: list[tuple[str, dict]] = [
    ("always_long", {"type": "strategy", "long_if": _g(_ret(1), -1.0), "short_if": None}),
    ("always_short", {"type": "strategy", "long_if": None, "short_if": _l(_ret(1), 1.0)}),
    ("momentum_1h", _mom(12)),
    ("momentum_4h", _mom(48)),
    ("momentum_1d", _mom(288)),
    ("reversal_1h", {"type": "strategy", "long_if": _l(_ret(12), 0.0), "short_if": _g(_ret(12), 0.0)}),
    (
        "momentum_1h_persist3",
        {
            "type": "strategy",
            "long_if": {"op": "holds_for", "a": _g(_ret(12), 0.0), "bars": 3},
            "short_if": {"op": "holds_for", "a": _l(_ret(12), 0.0), "bars": 3},
        },
    ),
    ("momentum_1h_hold12", {**_mom(12), "max_holding_bars": 12}),
    (
        # 両側条件は max_parameters(6)を超えるため long 側のみ(凍結制約に適合)
        "lowvol_momentum_long",
        {
            "type": "strategy",
            "long_if": {"op": "and", "a": _g(_ret(12), 0.0), "b": _l({"op": "volatility", "window": 48}, 0.002)},
            "short_if": None,
        },
    ),
    (
        "volume_shock_fade",
        {"type": "strategy", "long_if": None, "short_if": _g({"op": "volume_z", "window": 24}, 3.0), "max_holding_bars": 12},
    ),
]


def run_baselines(cfg: SearchConfig, out_dir: Path) -> dict:
    ev = Evaluator(cfg, out_dir)
    names = {}
    for name, ast in FROZEN_BASELINES:
        record = ev.evaluate(ast)
        names[name] = record["ast_hash"] if record else None
    return ev.summary(extra={"arm": "baselines", "baseline_hashes": names})


def main() -> None:
    parser = argparse.ArgumentParser(description="Baselines arm: 固定10戦略の評価(凍結)")
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()

    out_dir = args.out or Path("experiments") / "phase3" / "baselines"
    cfg = SearchConfig(method="baselines", seed=0, budget=len(FROZEN_BASELINES))
    report = run_baselines(cfg, out_dir)

    record = {"created_at": datetime.now().astimezone().isoformat(), **report}
    summary_path = out_dir / "summary.json"
    summary_path.write_text(json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    print("method=baselines budget=", len(FROZEN_BASELINES))
    print("counters:", json.dumps(report["counters"], ensure_ascii=False))
    hashes = report["arm_meta"]["baseline_hashes"]
    lines = {json.loads(x)["ast_hash"]: json.loads(x) for x in open(out_dir / "candidates.jsonl") if "evaluated" in x}
    for name, h in hashes.items():
        rec = lines.get(h)
        if rec:
            m = rec["research"]["base_taker"]
            print(
                f"  {name:<22} net {m['total_return']:+9.4f} sharpe {str(m['sharpe'])[:7]:>7} "
                f"trades {m['trade_count']:>6} pass={rec['research_pass']} survivor={rec['survivor']}"
            )
    print(f"summary -> {summary_path}")


if __name__ == "__main__":
    main()

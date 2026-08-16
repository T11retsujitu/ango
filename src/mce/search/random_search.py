"""Arm A — Random Grammar Search(凍結: docs/phase3/bakeoff_protocol.md §4)。

    python -m mce.search.random_search

凍結値: budget 30 / seed 20260818。seed の回し直しは禁止(公式runは1回)。
"""

import argparse
import json
from datetime import datetime
from pathlib import Path

from mce.search.grammar import strategy_stream
from mce.search.runner import SearchConfig, run_search

FROZEN_SEED = 20260818
FROZEN_BUDGET = 30


def main() -> None:
    parser = argparse.ArgumentParser(description="Arm A: random grammar search(凍結プロトコル)")
    parser.add_argument("--budget", type=int, default=FROZEN_BUDGET)
    parser.add_argument("--seed", type=int, default=FROZEN_SEED)
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()

    if args.seed != FROZEN_SEED or args.budget != FROZEN_BUDGET:
        print(f"警告: 凍結値(seed={FROZEN_SEED}, budget={FROZEN_BUDGET})以外での実行。公式runとしては無効")

    out_dir = args.out or Path("experiments") / "phase3" / f"random_seed{args.seed}"
    cfg = SearchConfig(method="random", seed=args.seed, budget=args.budget)
    report = run_search(strategy_stream(args.seed), cfg, out_dir)

    record = {"created_at": datetime.now().astimezone().isoformat(), **report}
    summary_path = out_dir / "summary.json"
    summary_path.write_text(json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    print(f"method=random seed={args.seed} budget={args.budget}")
    print("counters:", json.dumps(report["counters"], ensure_ascii=False))
    if report["survivors"]:
        for s in report["survivors"]:
            print(
                f"  survivor {s['ast_hash'][:12]}: research net {s['research']['total_return']:+.4f} "
                f"(sharpe {s['research']['sharpe']:.2f}, trades {s['research']['trade_count']}) / "
                f"validation net {s['validation']['total_return']:+.4f} "
                f"(sharpe {s['validation']['sharpe']:.2f}, trades {s['validation']['trade_count']})"
            )
    else:
        print("  survivors: なし")
    print(f"summary -> {summary_path}")


if __name__ == "__main__":
    main()

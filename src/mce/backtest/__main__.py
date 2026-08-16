"""backtest CLI。

    python -m mce.backtest --strategy buy_and_hold --split research --cost base_taker
    python -m mce.backtest --strategy random --seed 42 --split validation --cost stress

data/features/ の observable features を guard 付き loader 経由で読み、
baseline を実行して metrics を表示、experiments/runs/ へ artifact を保存する。
final_oos は指定できない(loader が封印している)。
"""

import argparse

from mce import config, experiments
from mce.backtest import baselines, data
from mce.backtest.costs import SCENARIOS
from mce.backtest.engine import run_backtest
from mce.backtest.execution import ExecutionConfig
from mce.manifest import dataset_manifest


def _strategy(args) -> "baselines.StrategySpec":
    if args.strategy == "always_flat":
        return baselines.always_flat()
    if args.strategy == "buy_and_hold":
        return baselines.buy_and_hold()
    if args.strategy == "momentum":
        return baselines.naive_momentum()
    if args.strategy == "random":
        if args.seed is None:
            raise SystemExit("--strategy random には --seed が必須(Determinism 規約)")
        return baselines.random_signal(seed=args.seed)
    raise SystemExit(f"未知の strategy: {args.strategy}")


def main() -> None:
    parser = argparse.ArgumentParser(description="固定 baseline の backtest を実行する")
    parser.add_argument("--strategy", required=True, choices=["always_flat", "buy_and_hold", "momentum", "random"])
    parser.add_argument("--split", default="research", help="research / validation(final_oos は封印)")
    parser.add_argument("--cost", default="base_taker", choices=sorted(SCENARIOS))
    parser.add_argument("--seed", type=int, default=None)
    parser.add_argument("--max-holding-bars", type=int, default=None)
    parser.add_argument("--no-artifact", action="store_true", help="artifact を保存しない")
    args = parser.parse_args()

    features = data.load_features(args.split)
    exec_cfg = ExecutionConfig(max_holding_bars=args.max_holding_bars)
    result = run_backtest(features, _strategy(args), SCENARIOS[args.cost], exec_cfg)

    print(f"strategy={result.strategy.name} split={args.split} cost={args.cost} bars={result.metrics['bars']}")
    for k in [
        "total_return", "annualized_return", "sharpe", "sortino", "max_drawdown",
        "turnover_total", "trade_count", "hit_rate", "profit_factor", "exposure",
        "break_even_cost_bps", "cancelled_fills",
    ]:
        print(f"  {k:<20}: {result.metrics[k]}")

    if not args.no_artifact:
        fp = config.features_parquet()
        hashes = {"features": dataset_manifest(fp)["sha256"]} if fp.exists() else {}
        artifact = experiments.build_artifact(result, split=args.split, manifest_hashes=hashes)
        path = experiments.save_artifact(artifact)
        print(f"artifact -> {path}")


if __name__ == "__main__":
    main()

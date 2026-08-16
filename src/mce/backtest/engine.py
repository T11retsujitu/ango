"""backtest engine: features → strategy → execution → cost → metrics の一気通貫。

strategy は「observable features → target position 系列」の決定的 callable に
限定する(自由な Python 実行環境・ネットワーク・ファイルアクセスを与えない。
将来の DSL/AST 移行を見据えた最小契約)。乱数を使う strategy は必ず明示 seed を
受け取って構築する(engine 側に暗黙の乱数源はない)。
"""

from dataclasses import dataclass, field
from typing import Callable

import polars as pl

from mce.backtest.costs import CostConfig
from mce.backtest.execution import ExecutionConfig, bar_returns, execute
from mce.backtest.metrics import compute_metrics


@dataclass(frozen=True)
class StrategySpec:
    """name/params は artifact 記録用。fn は features → target(-1/0/+1)の純関数。"""

    name: str
    fn: Callable[[pl.DataFrame], pl.Series]
    params: dict = field(default_factory=dict)
    seed: int | None = None


@dataclass
class BacktestResult:
    strategy: StrategySpec
    cost: CostConfig
    execution: ExecutionConfig
    bar_df: pl.DataFrame  # ts, position, gross_return, turnover, cost, net_return
    fills: pl.DataFrame
    trades: pl.DataFrame
    cancelled_count: int
    metrics: dict


def run_backtest(
    features: pl.DataFrame,
    strategy: StrategySpec,
    cost: CostConfig,
    exec_cfg: ExecutionConfig | None = None,
) -> BacktestResult:
    from mce.backtest.costs import apply_costs  # 循環import回避のためここで

    exec_cfg = exec_cfg or ExecutionConfig()
    if features.is_empty():
        raise ValueError("features が空(split・データを確認)")
    target = strategy.fn(features)
    if len(target) != features.height:
        raise ValueError(f"strategy {strategy.name} の target 長({len(target)})が bars({features.height})と不一致")

    res = execute(features.select("ts", "open"), target, exec_cfg)
    bar_df = apply_costs(bar_returns(features.select("ts", "open"), res.positions), cost)
    metrics = compute_metrics(bar_df, res.trades, cost)
    metrics["cancelled_fills"] = res.cancelled_count
    return BacktestResult(
        strategy=strategy,
        cost=cost,
        execution=exec_cfg,
        bar_df=bar_df,
        fills=res.fills,
        trades=res.trades,
        cancelled_count=res.cancelled_count,
        metrics=metrics,
    )

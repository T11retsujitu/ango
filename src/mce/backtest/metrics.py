"""backtest metrics(純関数)。

前提(ADR: 固定 notional 1 単位・非複利):
- リターンは算術合算(equity = cumsum)。annualized は mean × BARS_PER_YEAR。
- 5分足・24時間365日 → BARS_PER_YEAR = 288 × 365 = 105,120。
- 欠損ギャップは1区間として扱われるため、年率化は近似(欠損数は manifest 参照)。

定義できない場合(取引ゼロ・分散ゼロ等)は NaN ではなく None を返す。
DSR / PBO / SPA / Reality Check / block bootstrap は Phase 5(ここには置かない)。
"""

import math

import polars as pl

from mce.backtest.costs import CostConfig, break_even_cost_bps

BARS_PER_YEAR = 288 * 365  # 105,120


def compute_metrics(bar_df: pl.DataFrame, trades: pl.DataFrame, cfg: CostConfig) -> dict:
    """bar_df: apply_costs 済み(net_return, gross_return, turnover, position)。
    trades: execution の round trip(closed のみ集計対象)。"""
    n = bar_df.height
    net = bar_df["net_return"]
    closed = trades.filter(pl.col("closed")) if trades.height else trades
    trade_net = (
        closed.with_columns(
            (pl.col("gross_return") - cfg.roundtrip_bps * 1e-4).alias("net_return")
        )["net_return"]
        if closed.height
        else pl.Series([], dtype=pl.Float64)
    )

    mean = net.mean() if n else None
    std = net.std() if n >= 2 else None
    downside = net.filter(net < 0) if n else pl.Series([], dtype=pl.Float64)
    downside_dev = math.sqrt((downside**2).sum() / n) if n else None

    equity = net.cum_sum()
    drawdown = (equity.cum_max() - equity) if n else pl.Series([], dtype=pl.Float64)

    wins = trade_net.filter(trade_net > 0)
    losses = trade_net.filter(trade_net < 0)

    return {
        "bars": n,
        "total_return": _f(net.sum()) if n else 0.0,
        "annualized_return": _f(mean * BARS_PER_YEAR) if mean is not None else None,
        "sharpe": _f(mean / std * math.sqrt(BARS_PER_YEAR)) if std else None,
        "sortino": _f(mean / downside_dev * math.sqrt(BARS_PER_YEAR)) if downside_dev else None,
        "max_drawdown": _f(drawdown.max()) if n else 0.0,  # 正の大きさ(非複利 equity 基準)
        "turnover_total": _f(bar_df["turnover"].sum()) if n else 0.0,
        "trade_count": closed.height,
        "hit_rate": _f(wins.len() / trade_net.len()) if trade_net.len() else None,
        "profit_factor": _f(wins.sum() / abs(losses.sum())) if losses.len() and losses.sum() != 0 else None,
        "exposure": _f((bar_df["position"] != 0).sum() / n) if n else 0.0,
        "gross_total_return": _f(bar_df["gross_return"].sum()) if n else 0.0,
        "cost_total": _f(bar_df["cost"].sum()) if n else 0.0,
        "break_even_cost_bps": _f(break_even_cost_bps(bar_df)),
        "cost_scenario": cfg.name,
        "cost_per_side_bps": cfg.per_side_bps,
    }


def _f(x) -> float | None:
    return None if x is None else float(x)

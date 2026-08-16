"""固定 baseline strategies(ROADMAP Phase 0「fixed baseline strategies」)。

いずれも observable features のみを参照する決定的な StrategySpec を返す。
random_signal だけが乱数を使い、明示 seed が必須(Determinism Test の対象)。
"""

import random

import polars as pl

from mce.backtest.engine import StrategySpec


def always_flat() -> StrategySpec:
    return StrategySpec("always_flat", lambda df: pl.Series([0] * df.height, dtype=pl.Int8))


def buy_and_hold() -> StrategySpec:
    return StrategySpec("buy_and_hold", lambda df: pl.Series([1] * df.height, dtype=pl.Int8))


def naive_momentum(feature: str = "return_1h") -> StrategySpec:
    """直近リターンの符号に追随(null は flat)。"""

    def fn(df: pl.DataFrame) -> pl.Series:
        return (
            df.select(
                pl.when(pl.col(feature) > 0)
                .then(1)
                .when(pl.col(feature) < 0)
                .then(-1)
                .otherwise(0)
                .cast(pl.Int8)
                .alias("t")
            )["t"]
        )

    return StrategySpec("naive_momentum", fn, params={"feature": feature})


def random_signal(seed: int, p_long: float = 0.05, p_short: float = 0.05) -> StrategySpec:
    """seed 固定の random baseline(LLM/Genetic 比較の最低ライン用の雛形)。"""

    def fn(df: pl.DataFrame) -> pl.Series:
        rng = random.Random(seed)
        vals = []
        for _ in range(df.height):
            u = rng.random()
            vals.append(1 if u < p_long else -1 if u < p_long + p_short else 0)
        return pl.Series(vals, dtype=pl.Int8)

    return StrategySpec("random_signal", fn, params={"p_long": p_long, "p_short": p_short}, seed=seed)


def cost_aware_target(edge_bps: pl.Series, roundtrip_cost_bps: float) -> pl.Series:
    """Phase 1A(cost-aware abstention)の中核変換:
    予測エッジ(bps)が往復コストを超えるときだけ sign(edge) を取り、それ以外は flat。

    モデル(Logistic Regression 等)は Phase 1 で供給する。ここはモデル非依存の
    「forecast → trade conversion」規則のみ。"""
    return (
        pl.select(
            pl.when(edge_bps > roundtrip_cost_bps)
            .then(1)
            .when(edge_bps < -roundtrip_cost_bps)
            .then(-1)
            .otherwise(0)
            .cast(pl.Int8)
            .alias("t")
        )["t"]
    )

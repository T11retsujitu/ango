"""AST → 凍結済み Judge の strategy への決定的 compile(whitelist のみ)。

- compile 前に validator を必ず実行する
- 同一部分木は hash で memoize され1回だけ計算される
- null な条件は False(= flat / abstain)に解決される(Data Contract:
  「不明なら取引しない」)
仕様: docs/phase2/dsl_spec.md §4
"""

from dataclasses import dataclass

import polars as pl

from mce.backtest.engine import StrategySpec
from mce.backtest.execution import ExecutionConfig
from mce.dsl import ops
from mce.dsl.nodes import ast_hash, canonical_strategy, node_hash
from mce.dsl.validator import validate_strategy


@dataclass(frozen=True)
class CompiledStrategy:
    spec: StrategySpec
    execution: ExecutionConfig
    ast: dict  # 正規形
    ast_hash: str


def _eval_node(node: dict, df: pl.DataFrame, cache: dict) -> pl.Series:
    key = node_hash(node)
    if key in cache:
        return cache[key]
    op = node["op"]
    if op in ("return", "trend", "volatility", "range", "volume_z", "ma_slope"):
        out = getattr(ops, f"op_{op}")(df, node["window"])
    elif op in ("rolling_mean", "rolling_std", "zscore"):
        out = getattr(ops, f"op_{op}")(df, _eval_node(node["x"], df, cache), node["window"])
    elif op in ("greater", "less"):
        out = getattr(ops, f"op_{op}")(_eval_node(node["x"], df, cache), node["threshold"])
    elif op in ("and", "or"):
        out = getattr(ops, f"op_{op}")(_eval_node(node["a"], df, cache), _eval_node(node["b"], df, cache))
    elif op == "not":
        out = ops.op_not(_eval_node(node["a"], df, cache))
    elif op == "clock_is":
        out = ops.op_clock_is(df, node["period"], node["phase"])
    elif op == "holds_for":
        out = ops.op_holds_for(df, _eval_node(node["a"], df, cache), node["bars"])
    else:  # validator 通過後は到達しない
        raise ValueError(f"未知の op: {op}")
    cache[key] = out
    return out


def _cond(strategy: dict, key: str, df: pl.DataFrame, cache: dict) -> pl.Series:
    node = strategy.get(key)
    if node is None:
        return pl.Series([False] * df.height)
    return _eval_node(node, df, cache).fill_null(False)


def compile_strategy(strategy: dict) -> CompiledStrategy:
    validate_strategy(strategy)
    canon = canonical_strategy(strategy)
    h = ast_hash(canon)

    def fn(df: pl.DataFrame) -> pl.Series:
        cache: dict = {}
        long = _cond(canon, "long_if", df, cache)
        short = _cond(canon, "short_if", df, cache)
        frame = pl.DataFrame({"long": long, "short": short})
        target = frame.select(
            pl.when(pl.col("long") & ~pl.col("short"))
            .then(1)
            .when(pl.col("short") & ~pl.col("long"))
            .then(-1)
            .otherwise(0)
            .cast(pl.Int8)
            .alias("t")
        )["t"]
        if canon.get("flat_if") is not None:
            flat = _cond(canon, "flat_if", df, cache)
            target = pl.DataFrame({"t": target, "f": flat}).select(
                pl.when(pl.col("f")).then(0).otherwise(pl.col("t")).cast(pl.Int8).alias("t")
            )["t"]
        if canon.get("abstain_unless") is not None:
            gate = _cond(canon, "abstain_unless", df, cache)
            target = pl.DataFrame({"t": target, "g": gate}).select(
                pl.when(pl.col("g")).then(pl.col("t")).otherwise(0).cast(pl.Int8).alias("t")
            )["t"]
        return target

    spec = StrategySpec(name=f"dsl_{h[:12]}", fn=fn, params={"ast_hash": h})
    exec_cfg = ExecutionConfig(max_holding_bars=canon.get("max_holding_bars"))
    return CompiledStrategy(spec=spec, execution=exec_cfg, ast=canon, ast_hash=h)

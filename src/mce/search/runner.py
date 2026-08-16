"""bakeoff 共通評価パイプライン(凍結: docs/phase3/bakeoff_protocol.md §2–§3)。

全 arm(Random / Genetic / LLM / Baselines)が同一のこの runner を通る:

    draw → validate → dedupe → compile → research 評価 → 通過判定
         → validation 評価 → 生存判定

- selection は primary コスト(base_taker)のみで行い、secondary(maker_low)は
  参考記録。validation split はここでのみ使用。final_oos は封印(loader が拒否)。
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

import polars as pl

from mce import config, experiments
from mce.backtest import data as btdata
from mce.backtest.costs import SCENARIOS
from mce.backtest.engine import run_backtest
from mce.dsl.compiler import compile_strategy
from mce.dsl.nodes import ast_hash, canonical_strategy
from mce.dsl.validator import DslValidationError, validate_strategy
from mce.manifest import sha256_file
from mce.search.budget import SearchLedger

PROTOCOL = "phase3_bakeoff_v1"
PRIMARY_COST = "base_taker"
SECONDARY_COST = "maker_low"
MAX_DRAWS_PER_EVAL = 60  # 総draw上限 = budget × 60

RESEARCH_RULE = {"min_trades": 30}
VALIDATION_RULE = {"min_trades": 10}

_KEEP = ("total_return", "sharpe", "trade_count", "turnover_total", "break_even_cost_bps", "exposure", "max_drawdown", "cancelled_fills")


@dataclass(frozen=True)
class SearchConfig:
    method: str
    seed: int
    budget: int
    features_path: Path | None = None


def _metrics(feats: pl.DataFrame, compiled, scenario: str) -> dict:
    res = run_backtest(feats, compiled.spec, SCENARIOS[scenario], compiled.execution)
    return {k: res.metrics[k] for k in _KEEP}


def _passes(m: dict, rule: dict) -> bool:
    return (
        m["trade_count"] >= rule["min_trades"]
        and m["total_return"] > 0
        and (m["sharpe"] is not None and m["sharpe"] > 0)
    )


def run_search(candidates: Iterator[dict], cfg: SearchConfig, out_dir: Path) -> dict:
    research = btdata.load_features("research", path=cfg.features_path)
    validation = btdata.load_features("validation", path=cfg.features_path)
    if research.is_empty() or validation.is_empty():
        raise ValueError("research / validation の features が空")

    ledger = SearchLedger(out_dir)
    c = ledger.counters
    seen: set[str] = set()
    survivors: list[dict] = []
    max_draws = cfg.budget * MAX_DRAWS_PER_EVAL

    for ast in candidates:
        if c.evaluated_count >= cfg.budget or c.candidate_count >= max_draws:
            break
        c.candidate_count += 1
        idx = c.candidate_count
        try:
            validate_strategy(ast)
        except DslValidationError as e:
            c.rejected_candidate_count += 1
            ledger.record({"i": idx, "status": "rejected", "reason": str(e), "ast": ast})
            continue
        canon = canonical_strategy(ast)
        h = ast_hash(canon)
        if h in seen:
            c.duplicate_count += 1
            ledger.record({"i": idx, "status": "duplicate", "ast_hash": h})
            continue
        seen.add(h)
        c.unique_candidate_count += 1
        try:
            compiled = compile_strategy(canon)
            res_primary = _metrics(research, compiled, PRIMARY_COST)
            res_secondary = _metrics(research, compiled, SECONDARY_COST)
        except Exception as e:  # 決定的评估中の想定外エラーは記録して続行
            c.runtime_failure_count += 1
            ledger.record({"i": idx, "status": "runtime_failure", "ast_hash": h, "reason": repr(e), "ast": canon})
            continue
        c.evaluated_count += 1
        record = {
            "i": idx,
            "status": "evaluated",
            "ast_hash": h,
            "ast": canon,
            "research": {PRIMARY_COST: res_primary, SECONDARY_COST: res_secondary},
            "research_pass": _passes(res_primary, RESEARCH_RULE),
            "validation": None,
            "survivor": False,
        }
        if record["research_pass"]:
            c.research_pass_count += 1
            c.validation_count += 1
            val_primary = _metrics(validation, compiled, PRIMARY_COST)
            record["validation"] = {PRIMARY_COST: val_primary}
            if _passes(val_primary, VALIDATION_RULE):
                c.survivor_count += 1
                record["survivor"] = True
                survivors.append(
                    {"ast_hash": h, "ast": canon, "research": res_primary, "validation": val_primary}
                )
        ledger.record(record)

    if c.evaluated_count < cfg.budget:
        raise RuntimeError(
            f"draw 上限 {max_draws} 以内に budget {cfg.budget} を消化できなかった"
            f"(evaluated={c.evaluated_count})。grammar か制約を確認"
        )

    fp = cfg.features_path if cfg.features_path is not None else config.features_parquet()
    return {
        "protocol": PROTOCOL,
        "method": cfg.method,
        "config": {
            "seed": cfg.seed,
            "budget": cfg.budget,
            "primary_cost": PRIMARY_COST,
            "secondary_cost": SECONDARY_COST,
            "research_rule": RESEARCH_RULE,
            "validation_rule": VALIDATION_RULE,
        },
        "counters": ledger.summary_counters(),
        "survivors": survivors,
        "manifest_sha256": {"features": sha256_file(Path(fp))} if Path(fp).exists() else {},
        "source_commit": experiments.git_commit_hash(),
    }

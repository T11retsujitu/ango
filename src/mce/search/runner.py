"""bakeoff 共通評価パイプライン(凍結: docs/phase3/bakeoff_protocol.md §2–§3)。

全 arm(Random / Genetic / LLM / Baselines)が同一の Evaluator を通る:

    draw → validate → dedupe → compile → research 評価 → 通過判定
         → validation 評価 → 生存判定

- selection / fitness に使ってよいのは primary コスト(base_taker)の research
  metrics のみ。validation は記録専用(防火壁)。secondary(maker_low)は参考記録。
- validator 拒否・duplicate は budget を消費しない(全て台帳に記録)。
- Evaluator.evaluate は duplicate に対して評価済み record を返す
  (Genetic の fitness 再利用用。budget 非消費)。
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


class BudgetExhausted(RuntimeError):
    """budget または draw 上限に到達した。"""


@dataclass(frozen=True)
class SearchConfig:
    method: str
    seed: int
    budget: int
    features_path: Path | None = None


def _passes(m: dict, rule: dict) -> bool:
    return (
        m["trade_count"] >= rule["min_trades"]
        and m["total_return"] > 0
        and (m["sharpe"] is not None and m["sharpe"] > 0)
    )


class Evaluator:
    """budget 会計つきの candidate 評価器(全 arm 共用)。"""

    def __init__(self, cfg: SearchConfig, out_dir: Path):
        self.cfg = cfg
        self.research = btdata.load_features("research", path=cfg.features_path)
        self.validation = btdata.load_features("validation", path=cfg.features_path)
        if self.research.is_empty() or self.validation.is_empty():
            raise ValueError("research / validation の features が空")
        self.ledger = SearchLedger(out_dir)
        self.counters = self.ledger.counters
        self._records: dict[str, dict] = {}  # ast_hash → evaluated record
        self.survivors: list[dict] = []

    def budget_left(self) -> int:
        return self.cfg.budget - self.counters.evaluated_count

    def draws_left(self) -> int:
        return self.cfg.budget * MAX_DRAWS_PER_EVAL - self.counters.candidate_count

    def _metrics(self, feats: pl.DataFrame, compiled, scenario: str) -> dict:
        res = run_backtest(feats, compiled.spec, SCENARIOS[scenario], compiled.execution)
        return {k: res.metrics[k] for k in _KEEP}

    def evaluate(self, ast: dict) -> dict | None:
        """candidate を1つ処理する。返り値:
        - evaluated record(新規評価、または duplicate の場合は評価済み record)
        - None(validator 拒否 / runtime failure)
        budget・draw が尽きていれば BudgetExhausted。"""
        if self.budget_left() <= 0 or self.draws_left() <= 0:
            raise BudgetExhausted
        c = self.counters
        c.candidate_count += 1
        idx = c.candidate_count
        try:
            validate_strategy(ast)
        except DslValidationError as e:
            c.rejected_candidate_count += 1
            self.ledger.record({"i": idx, "status": "rejected", "reason": str(e), "ast": ast})
            return None
        canon = canonical_strategy(ast)
        h = ast_hash(canon)
        if h in self._records:
            c.duplicate_count += 1
            self.ledger.record({"i": idx, "status": "duplicate", "ast_hash": h})
            return self._records[h]
        c.unique_candidate_count += 1
        try:
            compiled = compile_strategy(canon)
            res_primary = self._metrics(self.research, compiled, PRIMARY_COST)
            res_secondary = self._metrics(self.research, compiled, SECONDARY_COST)
        except Exception as e:
            c.runtime_failure_count += 1
            self.ledger.record({"i": idx, "status": "runtime_failure", "ast_hash": h, "reason": repr(e), "ast": canon})
            return None
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
            val_primary = self._metrics(self.validation, compiled, PRIMARY_COST)
            record["validation"] = {PRIMARY_COST: val_primary}
            if _passes(val_primary, VALIDATION_RULE):
                c.survivor_count += 1
                record["survivor"] = True
                self.survivors.append(
                    {"ast_hash": h, "ast": canon, "research": res_primary, "validation": val_primary}
                )
        self.ledger.record(record)
        self._records[h] = record
        return record

    def summary(self, extra: dict | None = None) -> dict:
        fp = self.cfg.features_path if self.cfg.features_path is not None else config.features_parquet()
        report = {
            "protocol": PROTOCOL,
            "method": self.cfg.method,
            "config": {
                "seed": self.cfg.seed,
                "budget": self.cfg.budget,
                "primary_cost": PRIMARY_COST,
                "secondary_cost": SECONDARY_COST,
                "research_rule": RESEARCH_RULE,
                "validation_rule": VALIDATION_RULE,
            },
            "counters": self.ledger.summary_counters(),
            "survivors": self.survivors,
            "manifest_sha256": {"features": sha256_file(Path(fp))} if Path(fp).exists() else {},
            "source_commit": experiments.git_commit_hash(),
        }
        if extra:
            report["arm_meta"] = extra
        return report


def run_search(candidates: Iterator[dict], cfg: SearchConfig, out_dir: Path) -> dict:
    """iterator 駆動の探索(Random / Baselines 用)。budget 消化まで evaluate する。"""
    ev = Evaluator(cfg, out_dir)
    for ast in candidates:
        try:
            ev.evaluate(ast)
        except BudgetExhausted:
            break
    if ev.counters.evaluated_count < cfg.budget:
        raise RuntimeError(
            f"draw 上限 {cfg.budget * MAX_DRAWS_PER_EVAL} 以内に budget {cfg.budget} を"
            f"消化できなかった(evaluated={ev.counters.evaluated_count})"
        )
    return ev.summary()

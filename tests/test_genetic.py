"""Arm B(genetic)と Baselines arm のテスト(合成データ)。"""

import random
from datetime import datetime, timezone

import pytest

from conftest import make_ohlcv
from mce.dsl.nodes import ast_hash
from mce.dsl.validator import DslValidationError, validate_strategy
from mce.features import build_features
from mce.search import genetic
from mce.search.baselines_arm import FROZEN_BASELINES, run_baselines
from mce.search.runner import SearchConfig

UTC = timezone.utc


def _features_parquet(tmp_path):
    bars = []
    for base, days in [(datetime(2024, 3, 1, tzinfo=UTC), 4), (datetime(2025, 7, 2, tzinfo=UTC), 2)]:
        t0 = int(base.timestamp() * 1000) // 60_000
        for i in range(288 * days):
            px = 50_000 * (1 + 0.001 * ((i * 13) % 7 - 3))
            bars.append((t0 + 5 * i, px, 1.0 + (i % 5)))
    path = tmp_path / "features.parquet"
    build_features(make_ohlcv(bars)).write_parquet(path)
    return path


def _seed_strategy():
    return {
        "type": "strategy",
        "long_if": {"op": "greater", "x": {"op": "return", "window": 12}, "threshold": 0.001},
        "short_if": {"op": "less", "x": {"op": "return", "window": 12}, "threshold": -0.001},
        "max_holding_bars": 12,
    }


# ---- 遺伝操作 ----

def test_mutate_is_deterministic_and_changes_ast():
    s = _seed_strategy()
    a = genetic.mutate(s, random.Random(5))
    b = genetic.mutate(s, random.Random(5))
    c = genetic.mutate(s, random.Random(6))
    assert a == b
    assert ast_hash(a) != ast_hash(c) or a != c  # seed が違えば通常異なる
    assert s == _seed_strategy()  # 元の AST は不変(deep copy)


def test_mutate_mostly_valid():
    s = _seed_strategy()
    rng = random.Random(20260819)
    valid = 0
    for _ in range(100):
        m = genetic.mutate(s, rng)
        try:
            validate_strategy(m)
            valid += 1
        except DslValidationError:
            pass
    assert valid >= 70  # 摂動はメニュー内で閉じるので大半は有効


def test_mutation_changes_hash_often():
    s = _seed_strategy()
    rng = random.Random(1)
    changed = sum(ast_hash(genetic.mutate(s, rng)) != ast_hash(s) for _ in range(50))
    assert changed >= 40


def test_crossover_transplants_condition():
    a = _seed_strategy()
    b = {
        "type": "strategy",
        "long_if": {"op": "clock_is", "period": 15, "phase": 0},
        "short_if": None,
    }
    rng = random.Random(3)
    found_transplant = False
    for _ in range(20):
        child = genetic.crossover(a, b, rng)
        if any(child.get(k) == b["long_if"] for k in ("long_if", "short_if", "flat_if", "abstain_unless")):
            found_transplant = True
            break
    assert found_transplant
    assert a == _seed_strategy()  # 親は不変


def test_node_count_and_fitness_penalties():
    rec_small = {"ast": _seed_strategy(), "research": {"base_taker": {"sharpe": 1.0, "trade_count": 100}}}
    assert genetic.node_count(_seed_strategy()) == 4
    f_ok = genetic.fitness(rec_small)
    assert f_ok == pytest.approx(1.0 - 0.02 * 4)
    rec_few_trades = {"ast": _seed_strategy(), "research": {"base_taker": {"sharpe": 1.0, "trade_count": 10}}}
    assert genetic.fitness(rec_few_trades) == pytest.approx(f_ok - 2.0)
    rec_none = {"ast": _seed_strategy(), "research": {"base_taker": {"sharpe": None, "trade_count": 0}}}
    assert genetic.fitness(rec_none) < -90


# ---- GA 本体 ----

def test_run_genetic_accounting_and_determinism(tmp_path):
    path = _features_parquet(tmp_path)
    reports = []
    for name in ("a", "b"):
        cfg = SearchConfig(method="genetic", seed=11, budget=8, features_path=path)
        rep = genetic.run_genetic(cfg, tmp_path / name)
        rep.pop("source_commit")
        reports.append(rep)
    assert reports[0] == reports[1]  # 決定性
    c = reports[0]["counters"]
    assert c["evaluated_count"] == 8
    assert c["candidate_count"] >= 8
    assert reports[0]["arm_meta"]["generations"] >= 1
    assert len(reports[0]["arm_meta"]["final_population"]) <= genetic.POPULATION
    la = (tmp_path / "a" / "candidates.jsonl").read_text()
    lb = (tmp_path / "b" / "candidates.jsonl").read_text()
    assert la == lb


def test_genetic_fitness_never_reads_validation():
    """fitness は research primary のみ参照する(validation キー無しでも計算可能)。"""
    rec = {"ast": _seed_strategy(), "research": {"base_taker": {"sharpe": 0.5, "trade_count": 50}}}
    genetic.fitness(rec)  # validation キーが無くても例外にならない = 参照していない


# ---- Baselines arm ----

def test_baselines_are_valid_and_frozen_count():
    assert len(FROZEN_BASELINES) == 10
    names = [n for n, _ in FROZEN_BASELINES]
    assert len(set(names)) == 10
    for name, ast in FROZEN_BASELINES:
        validate_strategy(ast)  # 全て凍結制約に適合


def test_run_baselines(tmp_path):
    path = _features_parquet(tmp_path)
    cfg = SearchConfig(method="baselines", seed=0, budget=10, features_path=path)
    rep = run_baselines(cfg, tmp_path / "run")
    c = rep["counters"]
    assert c["candidate_count"] == 10
    assert c["evaluated_count"] == 10  # 全baseline有効・非重複
    assert c["rejected_candidate_count"] == 0
    assert set(rep["arm_meta"]["baseline_hashes"]) == {n for n, _ in FROZEN_BASELINES}
    assert all(h is not None for h in rep["arm_meta"]["baseline_hashes"].values())

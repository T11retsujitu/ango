"""bakeoff 基盤(grammar / budget / runner / random search)のテスト。"""

import json
import random
from datetime import datetime, timedelta, timezone

import polars as pl
import pytest

from conftest import make_ohlcv
from mce.dsl.nodes import ast_hash
from mce.dsl.validator import DslValidationError, validate_strategy
from mce.features import build_features
from mce.search import grammar
from mce.search.budget import SearchLedger
from mce.search.runner import SearchConfig, run_search

UTC = timezone.utc


# ---- grammar ----

def test_sampler_is_deterministic_and_diverse():
    def hashes(seed, n=60):
        rng = random.Random(seed)
        return [ast_hash(grammar.sample_strategy(rng)) for _ in range(n)]

    a, b, c = hashes(1), hashes(1), hashes(2)
    assert a == b
    assert a != c
    assert len(set(a)) >= 45  # 60サンプル中45以上ユニーク(多様性)


def test_sampler_mostly_valid_and_rejections_are_constraint_only():
    rng = random.Random(20260818)
    valid = 0
    for _ in range(200):
        s = grammar.sample_strategy(rng)
        try:
            validate_strategy(s)
            valid += 1
        except DslValidationError as e:
            # 拒否理由は制約(depth/features/params/feature必須)のみ許容。
            # 型・語彙エラーが出たら grammar のバグ
            assert any(
                k in str(e) for k in ("max_ast_depth", "max_features", "max_parameters", "feature ノード")
            ), str(e)
    assert valid >= 80  # 有効率4割以上(draw上限60倍に対し十分)


# ---- budget ledger ----

def test_ledger_appends_and_refuses_rerun(tmp_path):
    led = SearchLedger(tmp_path / "run")
    led.record({"i": 1, "status": "rejected"})
    led.record({"i": 2, "status": "evaluated"})
    lines = [json.loads(x) for x in (tmp_path / "run" / "candidates.jsonl").read_text().splitlines()]
    assert [x["i"] for x in lines] == [1, 2]
    with pytest.raises(FileExistsError):
        SearchLedger(tmp_path / "run")  # 公式runの上書き禁止


# ---- runner(合成データ: research + validation の両 split 区間を含む)----

def _features_parquet(tmp_path):
    bars = []
    for base, days in [(datetime(2024, 3, 1, tzinfo=UTC), 4), (datetime(2025, 7, 2, tzinfo=UTC), 2)]:
        t0 = int(base.timestamp() * 1000) // 60_000  # 分
        for i in range(288 * days):
            m = t0 + 5 * i
            px = 50_000 * (1 + 0.001 * ((i * 13) % 7 - 3))
            bars.append((m, px, 1.0 + (i % 5)))
    df = build_features(make_ohlcv(bars))
    path = tmp_path / "features.parquet"
    df.write_parquet(path)
    return path


def _momentum_ast(w):
    return {
        "type": "strategy",
        "long_if": {"op": "greater", "x": {"op": "return", "window": w}, "threshold": 0.0},
        "short_if": {"op": "less", "x": {"op": "return", "window": w}, "threshold": 0.0},
    }


def test_runner_accounting_and_dedupe(tmp_path):
    path = _features_parquet(tmp_path)
    bad = {"type": "strategy", "long_if": {"op": "future", "window": 1}, "short_if": None}
    cands = [bad, _momentum_ast(3), _momentum_ast(3), _momentum_ast(6), _momentum_ast(12)]
    cfg = SearchConfig(method="test", seed=0, budget=3, features_path=path)
    report = run_search(iter(cands), cfg, tmp_path / "run")
    c = report["counters"]
    assert c["candidate_count"] == 5
    assert c["rejected_candidate_count"] == 1
    assert c["duplicate_count"] == 1
    assert c["evaluated_count"] == 3  # budget どおり
    assert c["unique_candidate_count"] == 3
    assert c["validation_count"] == c["research_pass_count"]
    assert c["survivor_count"] == len(report["survivors"])
    lines = [json.loads(x) for x in (tmp_path / "run" / "candidates.jsonl").read_text().splitlines()]
    assert [x["status"] for x in lines] == ["rejected", "evaluated", "duplicate", "evaluated", "evaluated"]


def test_runner_is_deterministic(tmp_path):
    path = _features_parquet(tmp_path)
    reports = []
    for name in ("a", "b"):
        cfg = SearchConfig(method="random", seed=7, budget=4, features_path=path)
        rep = run_search(grammar.strategy_stream(7), cfg, tmp_path / name)
        rep.pop("source_commit")
        reports.append(rep)
    assert reports[0] == reports[1]
    la = (tmp_path / "a" / "candidates.jsonl").read_text()
    lb = (tmp_path / "b" / "candidates.jsonl").read_text()
    assert la == lb


def test_runner_budget_exhaustion_guard(tmp_path):
    path = _features_parquet(tmp_path)
    bad = {"type": "strategy", "long_if": {"op": "future", "window": 1}, "short_if": None}

    def all_bad():
        while True:
            yield bad  # 最初の1回で hash 重複にもならず…validator 拒否が続く

    cfg = SearchConfig(method="test", seed=0, budget=2, features_path=path)
    with pytest.raises(RuntimeError):
        run_search(all_bad(), cfg, tmp_path / "run")


def test_survivor_rule_applied(tmp_path):
    """research/validation とも上昇一辺倒のデータで buy_and_hold 相当 AST が生存する。"""
    bars = []
    for base, days in [(datetime(2024, 3, 1, tzinfo=UTC), 3), (datetime(2025, 7, 2, tzinfo=UTC), 2)]:
        t0 = int(base.timestamp() * 1000) // 60_000
        for i in range(288 * days):
            bars.append((t0 + 5 * i, 50_000 * (1.0002**i) * (1 + 0.0005 * (i % 2)), 1.0))
    path = tmp_path / "f.parquet"
    build_features(make_ohlcv(bars)).write_parquet(path)
    # 常にロング相当(return(288) が定義され次第 long)
    always_long = {
        "type": "strategy",
        "long_if": {"op": "greater", "x": {"op": "return", "window": 288}, "threshold": -1.0},
        "short_if": None,
        "max_holding_bars": 6,  # 定期的に強制exitして trade_count を稼ぐ
    }
    cfg = SearchConfig(method="test", seed=0, budget=1, features_path=path)
    report = run_search(iter([always_long]), cfg, tmp_path / "run")
    c = report["counters"]
    assert c["research_pass_count"] == 1
    assert c["survivor_count"] == 1
    s = report["survivors"][0]
    assert s["research"]["total_return"] > 0
    assert s["validation"]["trade_count"] >= 10

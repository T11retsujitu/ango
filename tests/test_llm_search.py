"""Arm C(LLM semantic search)のテスト。

ネットワークは一切使わず、fake propose 関数で LLM を差し替える。
検証対象: plan→AST 翻訳、閾値の量子化、masking、feedback が research のみ、
refusal 処理、replay の決定性、budget 会計。
"""

import json
from datetime import datetime, timezone

import pytest

from conftest import make_ohlcv
from mce.dsl.validator import validate_strategy
from mce.features import build_features
from mce.search import grammar, llm
from mce.search.plan import PlanError, hypothesis_to_ast, plan_to_ast, quantize_threshold
from mce.search.runner import SearchConfig
from mce.search.llm_search import run_llm_search

UTC = timezone.utc


# ---- plan → AST ----

def _plan(**over):
    plan = {
        "signal_family": "conditioned_momentum",
        "side": "long",
        "entry": {"feature": "return", "window": 12, "op": "greater", "threshold": 0.002},
        "filters": [],
        "clock": None,
        "persistence_bars": None,
        "holding_bars": None,
    }
    plan.update(over)
    return plan


def test_plan_to_ast_minimal():
    ast = plan_to_ast(_plan())
    assert ast["long_if"] == {"op": "greater", "x": {"op": "return", "window": 12}, "threshold": 0.002}
    assert ast["short_if"] is None
    validate_strategy(ast)


def test_side_both_mirrors_entry_only():
    ast = plan_to_ast(_plan(side="both", filters=[
        {"feature": "volatility", "window": 48, "op": "less", "threshold": 0.002}]))
    long_entry = ast["long_if"]["b"]
    short_entry = ast["short_if"]["b"]
    assert long_entry["op"] == "greater" and short_entry["op"] == "less"
    assert short_entry["threshold"] == -long_entry["threshold"]
    assert ast["long_if"]["a"] == ast["short_if"]["a"]  # filter は共通(反転しない)


def test_side_both_doubles_parameter_cost():
    """凍結 validator はパラメータを出現ごとに数えるため、side=both は条件木を
    複製してコストが倍になる。both+filter(8) は予算超過で棄却され、
    both 単独(4)は通る。Arm A/B と同じ規則(grammar も両側を独立サンプルする)。"""
    validate_strategy(plan_to_ast(_plan(side="both")))  # 4 params
    with pytest.raises(Exception):
        validate_strategy(plan_to_ast(_plan(side="both", filters=[
            {"feature": "volatility", "window": 48, "op": "less", "threshold": 0.002}])))  # 8 params


def test_persistence_and_clock_and_holding():
    ast = plan_to_ast(_plan(persistence_bars=3, clock={"period": 15, "phase": 0}, holding_bars=12))
    assert ast["max_holding_bars"] == 12
    assert ast["long_if"]["b"]["op"] == "holds_for"
    assert ast["long_if"]["a"]["op"] == "clock_is"
    validate_strategy(ast)


def test_threshold_quantized_to_frozen_menu():
    # LLM が任意の連続値を出しても凍結メニューへ量子化される(探索空間の同一性)
    ast = plan_to_ast(_plan(entry={"feature": "return", "window": 12, "op": "greater", "threshold": 0.0037}))
    menu, _ = grammar.THRESHOLD_MENUS["return"]
    assert ast["long_if"]["threshold"] in menu
    assert quantize_threshold("return", -0.0037) == -quantize_threshold("return", 0.0037)
    assert quantize_threshold("volatility", -0.002) > 0  # 符号なし特徴量は絶対値


@pytest.mark.parametrize("bad", [
    {"side": "sideways"},
    {"entry": {"feature": "fwd_return", "window": 12, "op": "greater", "threshold": 0.1}},
    {"entry": {"feature": "return", "window": 7, "op": "greater", "threshold": 0.1}},
    {"persistence_bars": 5},
    {"holding_bars": 100},
    {"clock": {"period": 10, "phase": 0}},
])
def test_plan_rejections(bad):
    with pytest.raises(PlanError):
        plan_to_ast(_plan(**bad))


def _hypothesis(hid="H001", **plan_over):
    return {
        "hypothesis_id": hid,
        "event": "momentum",
        "context": ["low_volatility"],
        "quality": ["persistence"],
        "direction": "continuation",
        "action": "long",
        "hypothesis": "Short-horizon continuation persists when volatility is low.",
        "expected_failure_mode": "transaction_cost",
        "dsl_plan": _plan(**plan_over),
    }


def test_hypothesis_to_ast_validates_semantic_schema():
    validate_strategy(hypothesis_to_ast(_hypothesis()))
    bad = _hypothesis()
    bad["event"] = "moon_phase"
    with pytest.raises(ValueError):
        hypothesis_to_ast(bad)


# ---- prompt / masking ----

def test_prompt_is_masked():
    prompt = llm.SYSTEM_PROMPT + "\n" + llm.build_user_prompt(6, [], 169708)
    lowered = prompt.lower()
    for leak in ("btc", "bitcoin", "okx", "binance", "usdt", "2024", "2025", "2023"):
        assert leak not in lowered, f"masking 漏れ: {leak}"
    assert "ASSET_X" in prompt


def test_prompt_history_contains_no_validation():
    history = [{"hypothesis_id": "H1", "signal_family": "f", "trades": 100,
                "net_bps_per_trade": -3.2, "sharpe": -0.4, "turnover": 200, "outcome": "net_negative"}]
    prompt = llm.build_user_prompt(6, history, 1000)
    assert "H1" in prompt and "net_negative" in prompt
    # validation は「意図的に伏せている」という説明としてのみ現れ、数値は現れない
    assert "withheld by design" in prompt
    assert "final_oos" not in prompt.lower()
    assert "sealed" not in prompt.lower()
    # feedback 行に含まれる指標は research primary の6項目だけ
    feedback_line = next(line for line in prompt.splitlines() if "H1 " in line)
    assert set(k for k in ("trades", "net_bps_per_trade", "sharpe", "turnover") if k in feedback_line) == {
        "trades", "net_bps_per_trade", "sharpe", "turnover"}
    assert "drawdown" not in feedback_line and "break_even" not in feedback_line


def test_prompt_hash_is_stable():
    a = llm.prompt_hash("sys", "user")
    assert a == llm.prompt_hash("sys", "user") and a != llm.prompt_hash("sys", "user2")


# ---- runner(fake LLM)----

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


def _fake_propose(batches):
    """呼び出しごとに batches[i] を返す fake。呼び出し回数も記録する。"""
    calls = []

    def propose(system, user):
        calls.append(user)
        if not batches:
            return {"hypotheses": []}
        return {"hypotheses": batches.pop(0)}

    return propose, calls


def test_llm_search_accounting_and_feedback(tmp_path):
    path = _features_parquet(tmp_path)
    good = [_hypothesis(f"H{i}", entry={"feature": "return", "window": w, "op": "greater", "threshold": 0.001})
            for i, w in enumerate((3, 6, 12, 24))]
    bad_plan = _hypothesis("HBAD", entry={"feature": "return", "window": 7, "op": "greater", "threshold": 0.1})
    propose, calls = _fake_propose([good[:2] + [bad_plan], good[2:]])
    cfg = SearchConfig(method="llm", seed=0, budget=4, features_path=path)
    report = run_llm_search(cfg, tmp_path / "run", propose=propose, model="fake-model")

    c = report["counters"]
    assert c["evaluated_count"] == 4
    assert c["rejected_candidate_count"] == 1  # plan エラーは budget 非消費
    assert report["arm_meta"]["api_calls"] == 2
    assert report["arm_meta"]["plan_errors"] == 1
    assert report["arm_meta"]["deterministic"] is False
    # 2回目のプロンプトに1回目の research 結果が入り、validation は入らない
    assert "H0" in calls[1] and "net_bps_per_trade" in calls[1]
    assert "validation net" not in calls[1]


def test_duplicate_plan_not_re_evaluated(tmp_path):
    path = _features_parquet(tmp_path)
    same = [_hypothesis("H1"), _hypothesis("H2"), _hypothesis("H3", entry={
        "feature": "trend", "window": 24, "op": "greater", "threshold": 0.001})]
    propose, _ = _fake_propose([same])
    cfg = SearchConfig(method="llm", seed=0, budget=2, features_path=path)
    report = run_llm_search(cfg, tmp_path / "run", propose=propose, model="fake")
    assert report["counters"]["evaluated_count"] == 2  # H1 と H3 のみ(H2 は同一 plan)


def test_refusal_is_recorded_and_loop_continues(tmp_path):
    path = _features_parquet(tmp_path)
    state = {"n": 0}

    def propose(system, user):
        state["n"] += 1
        if state["n"] == 1:
            raise llm.Refusal("declined (category=cyber)")
        return {"hypotheses": [_hypothesis("H9")]}

    cfg = SearchConfig(method="llm", seed=0, budget=1, features_path=path)
    report = run_llm_search(cfg, tmp_path / "run", propose=propose, model="fake")
    assert report["arm_meta"]["refusals"] == 1
    assert report["counters"]["evaluated_count"] == 1
    entries = llm.Transcript(tmp_path / "run" / "llm_transcript.jsonl").rounds()
    assert entries[0]["status"] == "refusal"
    assert "user_prompt" in entries[0] and "prompt_sha256" in entries[0]


def test_replay_is_deterministic_and_calls_no_api(tmp_path):
    path = _features_parquet(tmp_path)
    hyps = [_hypothesis(f"H{i}", entry={"feature": "return", "window": w, "op": "greater", "threshold": 0.001})
            for i, w in enumerate((3, 6, 12))]
    propose, _ = _fake_propose([hyps])
    cfg = SearchConfig(method="llm", seed=0, budget=3, features_path=path)
    live = run_llm_search(cfg, tmp_path / "live", propose=propose, model="fake")

    recorded = llm.replay_hypotheses(tmp_path / "live" / "llm_transcript.jsonl")
    assert len(recorded) == 3

    def exploding(system, user):  # replay 中に API が呼ばれたら失敗させる
        raise AssertionError("replay で API を呼んではいけない")

    reports = []
    for name in ("r1", "r2"):
        cfg2 = SearchConfig(method="llm", seed=0, budget=3, features_path=path)
        rep = run_llm_search(cfg2, tmp_path / name, propose=exploding, replay=recorded, model="fake")
        rep.pop("source_commit")
        reports.append(rep)
    assert reports[0] == reports[1]  # 評価側は完全決定的
    live.pop("source_commit")
    assert reports[0]["counters"]["evaluated_count"] == live["counters"]["evaluated_count"]
    assert reports[0]["arm_meta"]["mode"] == "replay"


def test_schema_enums_match_frozen_menus():
    from mce.search.plan import HYPOTHESIS_SCHEMA

    plan = HYPOTHESIS_SCHEMA["properties"]["hypotheses"]["items"]["properties"]["dsl_plan"]["properties"]
    assert plan["entry"]["properties"]["window"]["enum"] == list(grammar.WINDOW_MENU)
    assert plan["holding_bars"]["enum"] == list(grammar.HOLDING_MENU) + [None]
    assert set(plan["entry"]["properties"]["feature"]["enum"]) == set(grammar.FEATURE_OPS)

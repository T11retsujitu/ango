"""AST 正規化/hash・validator・compiler・semantic schema のテスト。"""

import polars as pl
import pytest

from conftest import make_ohlcv
from mce.backtest.baselines import naive_momentum
from mce.backtest.costs import SCENARIOS
from mce.backtest.engine import run_backtest
from mce.dsl import schema
from mce.dsl.compiler import compile_strategy
from mce.dsl.nodes import ast_hash, from_json, to_json
from mce.dsl.validator import DslValidationError, validate_strategy
from mce.features import build_features


def _g(op_node, thr=0.0):
    return {"op": "greater", "x": op_node, "threshold": thr}


def _ret(w):
    return {"op": "return", "window": w}


def momentum_ast(w=12):
    return {
        "type": "strategy",
        "long_if": _g(_ret(w), 0.0),
        "short_if": {"op": "less", "x": _ret(w), "threshold": 0.0},
    }


def _features(n=40):
    return build_features(make_ohlcv([(5 * i, 100.0 + ((i * 7) % 13) - 6, 1.0) for i in range(n)]))


# ---- nodes / hash ----

def test_hash_invariant_to_key_order_and_commutativity():
    a = {"op": "and", "a": _g(_ret(3)), "b": {"op": "clock_is", "period": 15, "phase": 0}}
    b = {"op": "and", "b": _g(_ret(3)), "a": {"phase": 0, "period": 15, "op": "clock_is"}}
    s1 = {"type": "strategy", "long_if": a, "short_if": None}
    s2 = {"short_if": None, "long_if": b, "type": "strategy"}
    assert ast_hash(s1) == ast_hash(s2)


def test_json_roundtrip_preserves_hash():
    s = momentum_ast()
    assert ast_hash(from_json(to_json(s))) == ast_hash(s)


# ---- validator ----

def test_validator_accepts_momentum():
    validate_strategy(momentum_ast())


@pytest.mark.parametrize(
    "mutate",
    [
        lambda s: s.update({"long_if": {"op": "future_return", "window": 3}}),  # 未知op
        lambda s: s.update({"long_if": _g({"op": "return", "window": 0})}),  # window下限
        lambda s: s.update({"long_if": _g({"op": "trend", "window": 289})}),  # window上限
        lambda s: s.update({"long_if": _g({"op": "clock_is", "period": 15, "phase": 0})}),  # 型不一致(num位置にbool)
        lambda s: s.update({"long_if": {"op": "not", "a": _ret(3)}}),  # 型不一致(bool位置にnum)
        lambda s: s.update({"long_if": {"op": "clock_is", "period": 10, "phase": 0}}),  # period不正
        lambda s: s.update({"long_if": {"op": "clock_is", "period": 15, "phase": 7}}),  # phase不正
        lambda s: s.update({"max_holding_bars": 49}),  # holding上限
        lambda s: s.update({"long_if": None, "short_if": None}),  # 条件なし
        lambda s: s.update({"exec_hack": 1}),  # 不明なrootキー
        lambda s: s.update({"long_if": _g(_ret(3), "high")}),  # threshold型
    ],
)
def test_validator_rejections(mutate):
    s = momentum_ast()
    mutate(s)
    with pytest.raises(DslValidationError):
        validate_strategy(s)


def test_validator_depth_limit():
    node = _g(_ret(3))  # greater(1段) + return(2段)
    for _ in range(3):
        node = {"op": "not", "a": node}
    validate_strategy({"type": "strategy", "long_if": node, "short_if": None})  # 5段 → OK
    node6 = {"op": "not", "a": node}
    with pytest.raises(DslValidationError):
        validate_strategy({"type": "strategy", "long_if": node6, "short_if": None})  # 6段


def test_validator_parameter_cap():
    # feature 3つ × (window+threshold) = 6 → OK。もう1条件足すと7 → NG
    c1 = {"op": "and", "a": _g(_ret(3)), "b": _g({"op": "volatility", "window": 12}, 0.01)}
    ok = {"type": "strategy", "long_if": {"op": "and", "a": c1, "b": _g({"op": "trend", "window": 20}, 0.0)}, "short_if": None}
    validate_strategy(ok)
    ng = {**ok, "max_holding_bars": 12}  # 7個目のパラメータ
    with pytest.raises(DslValidationError):
        validate_strategy(ng)


def test_validator_requires_market_feature():
    pure_clock = {"type": "strategy", "long_if": {"op": "clock_is", "period": 15, "phase": 0}, "short_if": None}
    with pytest.raises(DslValidationError):
        validate_strategy(pure_clock)  # 無条件時刻売買の構造的禁止


def test_duplicate_feature_counted_once():
    s = momentum_ast()  # return(12) が long/short 両方に登場
    validate_strategy(s)  # feature数1・param数4 で通る


# ---- compiler ----

def test_compiled_momentum_matches_baseline():
    feats = _features(40)
    cs = compile_strategy(momentum_ast(12))
    dsl_target = cs.spec.fn(feats)
    base_target = naive_momentum().fn(feats)  # return_1h 列 = return(12) と同定義
    assert dsl_target.to_list() == base_target.to_list()
    assert cs.spec.name == f"dsl_{cs.ast_hash[:12]}"


def test_conflict_resolves_to_flat():
    s = {
        "type": "strategy",
        "long_if": _g(_ret(1), -1.0),  # 定義できる行では常に真
        "short_if": {"op": "less", "x": _ret(1), "threshold": 1.0},
    }
    target = compile_strategy(s).spec.fn(_features(10))
    assert set(target.to_list()) == {0}


def test_warmup_nulls_are_flat():
    target = compile_strategy(momentum_ast(12)).spec.fn(_features(20))
    assert target[0] == 0  # return(12) 未定義の行は取引しない


def test_abstain_unless_gates_target():
    s = momentum_ast(1)
    s["abstain_unless"] = {"op": "clock_is", "period": 15, "phase": 0}
    feats = _features(30)
    target = compile_strategy(s).spec.fn(feats)
    minutes = feats["ts"].dt.minute().to_list()
    for m, t in zip(minutes, target.to_list()):
        if m % 15 != 0:
            assert t == 0


def test_flat_if_overrides():
    s = momentum_ast(1)
    # long/short と同じ feature 可用性で常に真 → 定義域全体が flat 化される
    s["flat_if"] = _g(_ret(1), -999.0)
    target = compile_strategy(s).spec.fn(_features(20))
    assert set(target.to_list()) == {0}
    # 対照: flat_if が無ければ非ゼロの target が存在する
    assert set(compile_strategy(momentum_ast(1)).spec.fn(_features(20)).to_list()) != {0}


def test_compile_is_deterministic_and_runs_in_judge():
    feats = _features(60)
    cs1 = compile_strategy(momentum_ast(3))
    cs2 = compile_strategy(momentum_ast(3))
    assert cs1.ast_hash == cs2.ast_hash
    assert cs1.spec.fn(feats).to_list() == cs2.spec.fn(feats).to_list()
    res = run_backtest(feats, cs1.spec, SCENARIOS["base_taker"], cs1.execution)
    assert res.metrics["bars"] == feats.height
    assert res.metrics["cost_scenario"] == "base_taker"


def test_compile_rejects_invalid():
    with pytest.raises(DslValidationError):
        compile_strategy({"type": "strategy", "long_if": {"op": "eval", "code": "1"}, "short_if": None})


def test_max_holding_flows_to_execution_config():
    s = {**momentum_ast(3), "max_holding_bars": 6}
    cs = compile_strategy(s)
    assert cs.execution.max_holding_bars == 6


# ---- schema ----

def _hypothesis():
    return {
        "hypothesis_id": "H017",
        "event": "clock_boundary",
        "context": ["low_volatility"],
        "quality": ["persistence"],
        "direction": "continuation",
        "action": "abstain",
        "hypothesis": "Short-horizon continuation becomes more tradable near quarter-hour boundaries.",
        "expected_failure_mode": "transaction_cost",
    }


def test_schema_accepts_roadmap_style_record():
    schema.validate_hypothesis(_hypothesis())


@pytest.mark.parametrize(
    "mutate",
    [
        lambda h: h.update({"event": "moon_phase"}),
        lambda h: h.update({"context": ["moderate_volatility"]}),
        lambda h: h.update({"context": []}),
        lambda h: h.update({"action": "buy"}),
        lambda h: h.pop("expected_failure_mode"),
        lambda h: h.update({"hypothesis": "  "}),
    ],
)
def test_schema_rejections(mutate):
    h = _hypothesis()
    mutate(h)
    with pytest.raises(schema.SchemaValidationError):
        schema.validate_hypothesis(h)

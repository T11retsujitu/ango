"""dsl_plan → AST の決定的変換(凍結: docs/phase3/bakeoff_protocol.md §10.1)。

LLM は AST を書かない。semantic な dsl_plan のみを出力し、このモジュールが
決定的に AST へ翻訳する。翻訳後は既存の validator / compiler / Evaluator を通る。

探索空間の同一性のため、閾値は凍結メニュー(grammar.THRESHOLD_MENUS)の
最近傍へ量子化する(LLM だけが連続値を使える不公平を排除)。
"""

from mce.dsl.schema import validate_hypothesis
from mce.search import grammar

FEATURES = grammar.FEATURE_OPS
OPS = ("greater", "less")
SIDES = ("long", "short", "both")


class PlanError(ValueError):
    """dsl_plan の構造が不正(語彙外・必須欠落など)。"""


def quantize_threshold(feature: str, value: float) -> float:
    """凍結メニューの最近傍へ量子化する(符号は保持。符号なし特徴量は絶対値)。"""
    menu, signed = grammar.THRESHOLD_MENUS[feature]
    magnitude = min(menu, key=lambda m: abs(m - abs(float(value))))
    if not signed:
        return magnitude
    return -magnitude if float(value) < 0 else magnitude


def _comparison(cond: dict) -> dict:
    for key in ("feature", "window", "op", "threshold"):
        if key not in cond:
            raise PlanError(f"条件に {key} がない: {cond!r}")
    if cond["feature"] not in FEATURES:
        raise PlanError(f"未知の feature: {cond['feature']!r}")
    if cond["op"] not in OPS:
        raise PlanError(f"未知の op: {cond['op']!r}")
    if cond["window"] not in grammar.WINDOW_MENU:
        raise PlanError(f"window はメニュー {grammar.WINDOW_MENU} のいずれか: {cond['window']!r}")
    return {
        "op": cond["op"],
        "x": {"op": cond["feature"], "window": int(cond["window"])},
        "threshold": quantize_threshold(cond["feature"], cond["threshold"]),
    }


def _mirror(cmp_node: dict) -> dict:
    """entry の反転(op 反転・閾値符号反転)。short 側の生成に使う。"""
    return {
        "op": "less" if cmp_node["op"] == "greater" else "greater",
        "x": dict(cmp_node["x"]),
        "threshold": -cmp_node["threshold"],
    }


def _and(a: dict | None, b: dict) -> dict:
    return b if a is None else {"op": "and", "a": a, "b": b}


def plan_to_ast(plan: dict) -> dict:
    """dsl_plan(意味レベル)→ AST。制約検査は validator が行う(ここは構造変換のみ)。"""
    if not isinstance(plan, dict):
        raise PlanError("dsl_plan は object であること")
    side = plan.get("side")
    if side not in SIDES:
        raise PlanError(f"side は {SIDES} のいずれか: {side!r}")
    if "entry" not in plan:
        raise PlanError("dsl_plan に entry がない")

    entry = _comparison(plan["entry"])
    entry_short = _mirror(entry)

    persistence = plan.get("persistence_bars")
    if persistence is not None:
        if persistence not in grammar.HOLDS_MENU:
            raise PlanError(f"persistence_bars はメニュー {grammar.HOLDS_MENU} のいずれか: {persistence!r}")
        entry = {"op": "holds_for", "a": entry, "bars": int(persistence)}
        entry_short = {"op": "holds_for", "a": entry_short, "bars": int(persistence)}

    context: dict | None = None
    for f in plan.get("filters") or []:
        context = _and(context, _comparison(f))
    clock = plan.get("clock")
    if clock is not None:
        if clock.get("period") not in (15, 60):
            raise PlanError(f"clock.period は 15 か 60: {clock!r}")
        context = _and(context, {"op": "clock_is", "period": int(clock["period"]), "phase": int(clock.get("phase", 0))})

    long_cond = _and(context, entry) if side in ("long", "both") else None
    short_cond = _and(context, entry_short) if side in ("short", "both") else None

    ast: dict = {"type": "strategy", "long_if": long_cond, "short_if": short_cond}
    holding = plan.get("holding_bars")
    if holding is not None:
        if holding not in grammar.HOLDING_MENU:
            raise PlanError(f"holding_bars はメニュー {grammar.HOLDING_MENU} のいずれか: {holding!r}")
        ast["max_holding_bars"] = int(holding)
    return ast


def hypothesis_to_ast(record: dict) -> dict:
    """仮説レコード全体(semantic schema + dsl_plan)を検証して AST を返す。"""
    validate_hypothesis({k: v for k, v in record.items() if k != "dsl_plan"})
    if "dsl_plan" not in record:
        raise PlanError("仮説レコードに dsl_plan がない")
    return plan_to_ast(record["dsl_plan"])


# ---- structured output 用 JSON Schema(語彙・メニューを enum で拘束)----

_CONDITION_SCHEMA = {
    "type": "object",
    "properties": {
        "feature": {"type": "string", "enum": list(FEATURES)},
        "window": {"type": "integer", "enum": list(grammar.WINDOW_MENU)},
        "op": {"type": "string", "enum": list(OPS)},
        "threshold": {"type": "number"},
    },
    "required": ["feature", "window", "op", "threshold"],
    "additionalProperties": False,
}

HYPOTHESIS_SCHEMA = {
    "type": "object",
    "properties": {
        "hypotheses": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "hypothesis_id": {"type": "string"},
                    "event": {"type": "string", "enum": ["momentum", "reversal", "volatility_shock", "volume_shock", "breakout", "clock_boundary"]},
                    "context": {"type": "array", "items": {"type": "string", "enum": ["high_volatility", "low_volatility", "trend", "range", "high_volume", "low_volume"]}},
                    "quality": {"type": "array", "items": {"type": "string", "enum": ["persistence", "acceleration", "exhaustion", "divergence", "confirmation"]}},
                    "direction": {"type": "string", "enum": ["continuation", "reversal"]},
                    "action": {"type": "string", "enum": ["long", "short", "flat", "abstain", "exit"]},
                    "hypothesis": {"type": "string"},
                    "expected_failure_mode": {"type": "string"},
                    "dsl_plan": {
                        "type": "object",
                        "properties": {
                            "signal_family": {"type": "string"},
                            "side": {"type": "string", "enum": list(SIDES)},
                            "entry": _CONDITION_SCHEMA,
                            "filters": {"type": "array", "items": _CONDITION_SCHEMA},
                            "clock": {
                                "type": ["object", "null"],
                                "properties": {
                                    "period": {"type": "integer", "enum": [15, 60]},
                                    "phase": {"type": "integer", "enum": list(range(0, 60, 5))},
                                },
                                "required": ["period", "phase"],
                                "additionalProperties": False,
                            },
                            "persistence_bars": {"type": ["integer", "null"], "enum": list(grammar.HOLDS_MENU) + [None]},
                            "holding_bars": {"type": ["integer", "null"], "enum": list(grammar.HOLDING_MENU) + [None]},
                        },
                        "required": ["signal_family", "side", "entry", "filters", "clock", "persistence_bars", "holding_bars"],
                        "additionalProperties": False,
                    },
                },
                "required": ["hypothesis_id", "event", "context", "quality", "direction", "action", "hypothesis", "expected_failure_mode", "dsl_plan"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["hypotheses"],
    "additionalProperties": False,
}

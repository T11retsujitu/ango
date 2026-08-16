"""AST の静的検証(compile 前に必ず実行)。

検査項目(docs/phase2/dsl_spec.md §3–§5):
- op が whitelist に存在するか / 型(num・bool)が合っているか
- パラメータの型・範囲
- max_ast_depth 5 / max_features 4 / max_parameters 6 / max_holding_bars 48
違反は DslValidationError。
"""

import json

from mce.dsl.nodes import ROOT_CONDITION_KEYS, iter_nodes

MAX_AST_DEPTH = 5
MAX_FEATURES = 4
MAX_PARAMETERS = 6
MAX_HOLDING_BARS = 48
MAX_WINDOW = 288

# op 名 → (返り値kind, {子キー: 要求kind}, {パラメータ: (最小, 最大)})
OP_SPECS: dict[str, tuple[str, dict, dict]] = {
    "return": ("num", {}, {"window": (1, MAX_WINDOW)}),
    "trend": ("num", {}, {"window": (2, MAX_WINDOW)}),
    "volatility": ("num", {}, {"window": (2, MAX_WINDOW)}),
    "range": ("num", {}, {"window": (2, MAX_WINDOW)}),
    "volume_z": ("num", {}, {"window": (2, MAX_WINDOW)}),
    "ma_slope": ("num", {}, {"window": (2, MAX_WINDOW)}),
    "rolling_mean": ("num", {"x": "num"}, {"window": (2, MAX_WINDOW)}),
    "rolling_std": ("num", {"x": "num"}, {"window": (2, MAX_WINDOW)}),
    "zscore": ("num", {"x": "num"}, {"window": (2, MAX_WINDOW)}),
    "greater": ("bool", {"x": "num"}, {"threshold": (-1e6, 1e6)}),
    "less": ("bool", {"x": "num"}, {"threshold": (-1e6, 1e6)}),
    "and": ("bool", {"a": "bool", "b": "bool"}, {}),
    "or": ("bool", {"a": "bool", "b": "bool"}, {}),
    "not": ("bool", {"a": "bool"}, {}),
    "clock_is": ("bool", {}, {"phase": (0, 55)}),  # period は個別検査
    "holds_for": ("bool", {"a": "bool"}, {"bars": (2, MAX_HOLDING_BARS)}),
}

FEATURE_OPS = {"return", "trend", "volatility", "range", "volume_z", "ma_slope"}
INT_PARAMS = {"window", "bars", "phase", "period", "max_holding_bars"}


class DslValidationError(ValueError):
    pass


def _check_node(node: dict, expected_kind: str, depth: int) -> int:
    """ノードを再帰検証し、部分木の深さを返す。"""
    if depth > MAX_AST_DEPTH:
        raise DslValidationError(f"max_ast_depth {MAX_AST_DEPTH} 超過")
    if not isinstance(node, dict) or "op" not in node:
        raise DslValidationError(f"ノードは op を持つ object であること: {node!r}")
    op = node["op"]
    if op not in OP_SPECS:
        raise DslValidationError(f"未知の op: {op!r}")
    kind, children, params = OP_SPECS[op]
    if kind != expected_kind:
        raise DslValidationError(f"op {op} は {kind} だが {expected_kind} の位置に置かれている")

    allowed_keys = {"op"} | set(children) | set(params) | ({"period"} if op == "clock_is" else set())
    extra = set(node) - allowed_keys
    if extra:
        raise DslValidationError(f"op {op} に不明なキー {sorted(extra)}")

    for pname, (lo, hi) in params.items():
        if pname not in node:
            raise DslValidationError(f"op {op} にパラメータ {pname} がない")
        v = node[pname]
        if pname in INT_PARAMS:
            if not isinstance(v, int) or isinstance(v, bool):
                raise DslValidationError(f"op {op} の {pname} は int であること: {v!r}")
        elif not isinstance(v, (int, float)) or isinstance(v, bool):
            raise DslValidationError(f"op {op} の {pname} は数値であること: {v!r}")
        if not (lo <= v <= hi):
            raise DslValidationError(f"op {op} の {pname}={v} が範囲 [{lo}, {hi}] 外")

    if op == "clock_is":
        period = node.get("period")
        if period not in (15, 60):
            raise DslValidationError(f"clock_is の period は 15 か 60: {period!r}")
        if node["phase"] % 5 != 0 or not (0 <= node["phase"] < period):
            raise DslValidationError(f"clock_is の phase={node['phase']} は 5 の倍数かつ 0 <= phase < {period}")

    max_child_depth = 0
    for ckey, ckind in children.items():
        if ckey not in node:
            raise DslValidationError(f"op {op} に子 {ckey} がない")
        max_child_depth = max(max_child_depth, _check_node(node[ckey], ckind, depth + 1))
    return 1 + max_child_depth


def _count_parameters(strategy: dict) -> int:
    n = 0
    for key in ROOT_CONDITION_KEYS:
        cond = strategy.get(key)
        if isinstance(cond, dict):
            for node in iter_nodes(cond):
                _, _, params = OP_SPECS.get(node.get("op"), ("", {}, {}))
                n += len(params)
    if strategy.get("max_holding_bars") is not None:
        n += 1
    return n


def _distinct_features(strategy: dict) -> set[str]:
    out = set()
    for key in ROOT_CONDITION_KEYS:
        cond = strategy.get(key)
        if isinstance(cond, dict):
            for node in iter_nodes(cond):
                if node.get("op") in FEATURE_OPS:
                    out.add(json.dumps(node, sort_keys=True))
    return out


def validate_strategy(strategy: dict) -> None:
    if not isinstance(strategy, dict) or strategy.get("type") != "strategy":
        raise DslValidationError('root は {"type": "strategy", ...} であること')
    allowed = {"type", "max_holding_bars", *ROOT_CONDITION_KEYS}
    extra = set(strategy) - allowed
    if extra:
        raise DslValidationError(f"root に不明なキー {sorted(extra)}")
    if strategy.get("long_if") is None and strategy.get("short_if") is None:
        raise DslValidationError("long_if / short_if の少なくとも一方が必要")

    for key in ROOT_CONDITION_KEYS:
        cond = strategy.get(key)
        if cond is not None:
            _check_node(cond, "bool", depth=1)

    mh = strategy.get("max_holding_bars")
    if mh is not None and (not isinstance(mh, int) or isinstance(mh, bool) or not (1 <= mh <= MAX_HOLDING_BARS)):
        raise DslValidationError(f"max_holding_bars={mh!r} は 1..{MAX_HOLDING_BARS} の int であること")

    n_feat = len(_distinct_features(strategy))
    if n_feat > MAX_FEATURES:
        raise DslValidationError(f"feature 数 {n_feat} が max_features {MAX_FEATURES} 超過")
    if n_feat == 0:
        raise DslValidationError("feature ノードが1つも無い(市場情報を参照しない strategy は不可)")

    n_params = _count_parameters(strategy)
    if n_params > MAX_PARAMETERS:
        raise DslValidationError(f"パラメータ数 {n_params} が max_parameters {MAX_PARAMETERS} 超過")

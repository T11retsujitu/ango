"""AST の正規化・hash・(de)serialization。

AST は JSON object のみで表現する(Python コードは存在しない)。
正規形: パラメータキーをソートし、可換演算子(and / or)の2子を子 hash 順に
並べ替える。ast_hash は正規形 JSON の sha256(duplicate control の第1層)。
仕様: docs/phase2/dsl_spec.md §6
"""

import hashlib
import json

COMMUTATIVE_OPS = {"and", "or"}
CHILD_KEYS = ("x", "a", "b")  # 子ノードを持ちうるキー

ROOT_CONDITION_KEYS = ("long_if", "short_if", "flat_if", "abstain_unless")


def canonical(node: dict) -> dict:
    """ノードの正規形(再帰)。入力は変更しない。"""
    out = {}
    for k, v in node.items():
        out[k] = canonical(v) if isinstance(v, dict) else v
    if out.get("op") in COMMUTATIVE_OPS:
        a, b = out["a"], out["b"]
        if node_hash(a) > node_hash(b):
            out["a"], out["b"] = b, a
    return out


def node_hash(node: dict) -> str:
    return hashlib.sha256(json.dumps(node, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def canonical_strategy(strategy: dict) -> dict:
    out = {}
    for k, v in strategy.items():
        out[k] = canonical(v) if isinstance(v, dict) else v
    return out


def ast_hash(strategy: dict) -> str:
    return node_hash(canonical_strategy(strategy))


def to_json(strategy: dict) -> str:
    return json.dumps(canonical_strategy(strategy), sort_keys=True, ensure_ascii=False)


def from_json(text: str) -> dict:
    obj = json.loads(text)
    if not isinstance(obj, dict):
        raise ValueError("strategy AST は JSON object であること")
    return obj


def iter_nodes(node: dict):
    """ノードとその全子孫を列挙する。"""
    yield node
    for k in CHILD_KEYS:
        child = node.get(k)
        if isinstance(child, dict):
            yield from iter_nodes(child)

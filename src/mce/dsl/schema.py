"""Semantic Schema — searcher(Random / Genetic / LLM)が使える仮説語彙。

AlphaSchema 流の Event × Context × Quality × Direction × Action 空間
(docs/phase2/dsl_spec.md §7)。仮説レコードはこの語彙のみで記述でき、
語彙外の値は拒否される。hypothesis → AST の変換は Phase 3 の searcher 側の責務。
"""

EVENTS = ("momentum", "reversal", "volatility_shock", "volume_shock", "breakout", "clock_boundary")
CONTEXTS = ("high_volatility", "low_volatility", "trend", "range", "high_volume", "low_volume")
QUALITIES = ("persistence", "acceleration", "exhaustion", "divergence", "confirmation")
DIRECTIONS = ("continuation", "reversal")
ACTIONS = ("long", "short", "flat", "abstain", "exit")

REQUIRED_FIELDS = (
    "hypothesis_id",
    "event",
    "context",
    "quality",
    "direction",
    "action",
    "hypothesis",
    "expected_failure_mode",
)


class SchemaValidationError(ValueError):
    pass


def validate_hypothesis(record: dict) -> None:
    """仮説レコードの検証(語彙外・欠落は SchemaValidationError)。"""
    if not isinstance(record, dict):
        raise SchemaValidationError("仮説レコードは object であること")
    missing = [f for f in REQUIRED_FIELDS if f not in record]
    if missing:
        raise SchemaValidationError(f"必須フィールド欠落: {missing}")
    if record["event"] not in EVENTS:
        raise SchemaValidationError(f"未知の event: {record['event']!r}")
    for field, vocab, kind in [("context", CONTEXTS, "context"), ("quality", QUALITIES, "quality")]:
        values = record[field]
        if not isinstance(values, list) or not values:
            raise SchemaValidationError(f"{field} は非空 list であること")
        for v in values:
            if v not in vocab:
                raise SchemaValidationError(f"未知の {kind}: {v!r}")
    if record["direction"] not in DIRECTIONS:
        raise SchemaValidationError(f"未知の direction: {record['direction']!r}")
    if record["action"] not in ACTIONS:
        raise SchemaValidationError(f"未知の action: {record['action']!r}")
    for field in ("hypothesis", "expected_failure_mode", "hypothesis_id"):
        if not isinstance(record[field], str) or not record[field].strip():
            raise SchemaValidationError(f"{field} は非空文字列であること")

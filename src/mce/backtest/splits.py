"""research / validation / final_oos の split 境界の単一定義。

境界は docs/data_contract.md §9 と一致させること。Phase 1 終了後に freeze し、
以降の searcher は変更できない。

- 各区間は [start, end)(end 排他)。final_oos に上限はない
  (将来蓄積される新データも自動的に封印域へ入る)。
- research 開始より古いバーはどの split にも属さない。
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

UTC = timezone.utc

RESEARCH_START = datetime(2023, 11, 19, tzinfo=UTC)
VALIDATION_START = datetime(2025, 7, 1, tzinfo=UTC)  # = research の排他的上限
FINAL_OOS_START = datetime(2026, 1, 1, tzinfo=UTC)  # = validation の排他的上限。上限なし

# 通常 loader が扱ってよい split
RESEARCH_SPLITS = ("research", "validation")
SEALED_SPLIT = "final_oos"


def split_bounds(name: str) -> tuple[datetime, datetime | None]:
    """split 名 → (start, end)。end=None は上限なし。"""
    if name == "research":
        return (RESEARCH_START, VALIDATION_START)
    if name == "validation":
        return (VALIDATION_START, FINAL_OOS_START)
    if name == SEALED_SPLIT:
        return (FINAL_OOS_START, None)
    raise ValueError(f"未知の split: {name!r}(research / validation / final_oos)")


def assign(ts: datetime) -> str | None:
    """timestamp が属する split 名(どれにも属さなければ None)。"""
    if ts < RESEARCH_START:
        return None
    if ts < VALIDATION_START:
        return "research"
    if ts < FINAL_OOS_START:
        return "validation"
    return SEALED_SPLIT


@dataclass(frozen=True)
class Fold:
    """walk-forward の 1 fold。各区間は [start, end)。train_end == test_start。"""

    train_start: datetime
    train_end: datetime
    test_start: datetime
    test_end: datetime


def walk_forward_folds(
    train_days: int,
    test_days: int,
    start: datetime = RESEARCH_START,
    end: datetime = FINAL_OOS_START,
    step_days: int | None = None,
) -> list[Fold]:
    """[start, end) 内に納まる walk-forward folds を返す(Phase 1A 用)。

    final_oos に食い込む fold は作れない(end の既定と上限チェックで強制)。
    """
    if end > FINAL_OOS_START:
        raise ValueError("walk-forward folds は final_oos に入ってはならない")
    step = timedelta(days=step_days if step_days is not None else test_days)
    train, test = timedelta(days=train_days), timedelta(days=test_days)
    folds: list[Fold] = []
    t0 = start
    while t0 + train + test <= end:
        folds.append(Fold(t0, t0 + train, t0 + train, t0 + train + test))
        t0 = t0 + step
    return folds

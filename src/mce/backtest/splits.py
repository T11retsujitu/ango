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

# --- Phase 8 追加(2026-08-17。人間の決定ログにより承認。freeze v2)-------------
#
# Phase 8 の prospective final 窓。**FINAL_OOS_START は一切変更しない。**
# この定数は既存 split の定義を上書きせず、Phase 8 の評価窓を別に定めるだけである。
#
#   [FINAL_OOS_START, PHASE8_PROSPECTIVE_START) = Phase 8 にとって「汚染域」
#       ango 自身が 2026-07 まで funding を測定済み(K9)であり、
#       文献も 2026 年に言及している。Phase 8 の結果評価に**決して読まない**。
#   [PHASE8_PROSPECTIVE_START, ∞)             = Phase 8 の GO/NO-GO 判定窓
#
# 既存 split(research / validation / final_oos)の意味は変わらない。
# final_oos は従来どおり 2026-01-01 以降すべてであり、Phase 8 の汚染域は
# その部分集合である。
PHASE8_PROSPECTIVE_START = datetime(2026, 9, 1, tzinfo=UTC)

# Phase 8 の結果評価で読んではならない区間 [start, end)
PHASE8_CONTAMINATED_BAND = (FINAL_OOS_START, PHASE8_PROSPECTIVE_START)

# layer 1 / layer 2 の境界。max(A2 2024-03-11, A1 2024-07, K11 2023-06-23,
# K12 2025-05-31) を月境界へ切り上げたもの。
PHASE8_LAYER1_START = datetime(2020, 1, 1, tzinfo=UTC)
PHASE8_LAYER1_END = datetime(2025, 6, 1, tzinfo=UTC)


def phase8_layer(ts: datetime) -> str:
    """Phase 8 の層名を返す(既存の split とは独立の分類)。

    - ``literature_in_sample``      : 先行研究が使用済み。GO 判定に使わない
    - ``contaminated_confirmation`` : 論文外だが要約統計を知ってしまった窓
    - ``phase8_contaminated``       : 既存封印域かつ外部/在庫汚染。**読まない**
    - ``phase8_prospective_final``  : GO / NO-GO をここで判定する
    """
    if ts < PHASE8_LAYER1_END:
        return "literature_in_sample"
    if ts < FINAL_OOS_START:
        return "contaminated_confirmation"
    if ts < PHASE8_PROSPECTIVE_START:
        return "phase8_contaminated"
    return "phase8_prospective_final"

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

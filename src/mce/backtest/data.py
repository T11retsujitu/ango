"""backtest からデータへ到達する唯一の入口(guard 付き loader)。

契約(docs/data_contract.md):
- features のみを読む。fwd_ 接頭辞列(ラベル)を検出したら LeakageError。
- availability 未宣言の列を検出したら UndeclaredFeatureError。
- split 名は必須。research / validation のみ通常 API で読める。
- final_oos は封印されており load_features では読めない(SealedAccessError)。
  Phase 6 の sealed evaluator のみが load_sealed_final_oos を明示の確認文字列
  付きで呼ぶこと。結果を research loop へ戻すことは禁止(ROADMAP §4.2)。
"""

from pathlib import Path

import polars as pl

from mce import config
from mce.backtest import splits
from mce.features import AVAILABILITY, META_COLUMNS


class LeakageError(RuntimeError):
    """label(fwd_*)列が strategy 入力に混入している。"""


class UndeclaredFeatureError(RuntimeError):
    """availability 未宣言の列が strategy 入力に混入している。"""


class SealedAccessError(RuntimeError):
    """封印済み final_oos への不正アクセス。"""


SEALED_ACK = "I_UNDERSTAND_THIS_EXPOSES_FINAL_OOS"


def _validate_columns(df: pl.DataFrame) -> None:
    leaks = [c for c in df.columns if c.startswith("fwd_")]
    if leaks:
        raise LeakageError(f"label 列 {leaks} は strategy feature として利用できない(data/labels/ を join できるのは評価側のみ)")
    unknown = [c for c in df.columns if c not in META_COLUMNS and c not in AVAILABILITY]
    if unknown:
        raise UndeclaredFeatureError(f"availability 未宣言の列 {unknown}(mce.features.AVAILABILITY へ宣言すること)")


def load_features(split: str, path: Path | None = None) -> pl.DataFrame:
    """split 済み observable features を返す。final_oos は読めない。"""
    if split == splits.SEALED_SPLIT:
        raise SealedAccessError(
            "final_oos は封印されている。Phase 6 の sealed evaluator のみが load_sealed_final_oos を使用できる"
        )
    start, end = splits.split_bounds(split)  # 未知の split はここで ValueError
    df = pl.read_parquet(path if path is not None else config.features_parquet())
    _validate_columns(df)
    cond = pl.col("ts") >= start
    if end is not None:
        cond = cond & (pl.col("ts") < end)
    return df.filter(cond).sort("ts")


def load_sealed_final_oos(acknowledgement: str, path: Path | None = None) -> pl.DataFrame:
    """封印域の読み出し。Phase 6 sealed evaluator 専用。

    呼び出しには確認文字列 SEALED_ACK が必要。結果(データ・メトリクス・
    失敗理由のいずれも)を Research/Validation loop や memory へ戻してはならない。
    """
    if acknowledgement != SEALED_ACK:
        raise SealedAccessError("確認文字列が一致しない。final_oos へのアクセスは Phase 6 sealed evaluator に限られる")
    start, _ = splits.split_bounds(splits.SEALED_SPLIT)
    df = pl.read_parquet(path if path is not None else config.features_parquet())
    _validate_columns(df)
    return df.filter(pl.col("ts") >= start).sort("ts")

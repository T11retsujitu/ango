"""raw 保存と normalized Parquet へのマージ(冪等)。"""

import gzip
import json
from datetime import datetime, timezone
from pathlib import Path

import polars as pl


def append_raw(raw_dir: Path, dataset: str, run_id: str, record: dict) -> Path:
    """API レスポンス1件を JSON Lines (gzip) で追記保存する。

    raw は原形保持が目的なので重複があっても構わない。重複排除は normalized 層で行う。
    """
    path = raw_dir / dataset / f"{run_id}.jsonl.gz"
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "at", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    return path


def new_run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def merge_parquet(path: Path, df: pl.DataFrame, key_cols: list[str]) -> int:
    """既存 Parquet と結合し、key_cols で重複排除して書き戻す(冪等)。

    同じデータを何度マージしても行数は増えない。一時ファイルに書いてから
    rename するので途中失敗で既存ファイルが壊れることはない。
    戻り値は新規に追加された行数。
    """
    if df.is_empty():
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        existing = pl.read_parquet(path)
        before = existing.height
        merged = pl.concat([existing, df.select(existing.columns)], how="vertical")
    else:
        before = 0
        merged = df
    merged = merged.unique(subset=key_cols, keep="first").sort(key_cols)
    tmp = path.with_suffix(".parquet.tmp")
    merged.write_parquet(tmp)
    tmp.replace(path)
    return merged.height - before


def max_ts_ms(path: Path, ts_col: str = "ts") -> int | None:
    """既存 Parquet の最大タイムスタンプ(ms)。差分取得の再開位置に使う。"""
    if not path.exists():
        return None
    ts = pl.read_parquet(path, columns=[ts_col])[ts_col].max()
    if ts is None:
        return None
    return int(ts.timestamp() * 1000)

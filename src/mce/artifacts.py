"""運用 artifact(append-only ledger と atomic JSON)の共通書き込み。

collector supervisor・quality gate・日次 ingest はどれも「途中で電源が落ちても
壊れた artifact を残さない」ことを前提にする。そのため:

- ledger は **append-only JSONL**。行は 1 回の ``write`` で追記し、``fsync`` して
  から戻る。途中まで書かれた行を後から読み手が黙って捨てないよう、読み出しは
  末尾の不完全な行を明示的な error として扱う。
- report は **atomic JSON**。同一 directory の temp file へ書き、``fsync`` して
  から ``os.replace`` する。読み手は常に完全な JSON だけを見る。

raw 層(:mod:`mce.stream_store` 等)と同じ耐久化規律を、分析側 artifact にも
そのまま適用する。
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Iterator, Mapping


class LedgerError(RuntimeError):
    """append-only ledger を安全に読み書きできなかった。"""


def _fsync_directory(directory: Path) -> None:
    """rename/create 自体の durability も可能な環境では確保する。"""

    directory_fd: int | None = None
    try:
        directory_fd = os.open(directory, os.O_RDONLY)
        os.fsync(directory_fd)
    except OSError:
        # directory fsync を持たない platform ではファイル本体の確定を優先する。
        pass
    finally:
        if directory_fd is not None:
            os.close(directory_fd)


def json_line(record: Mapping[str, Any]) -> str:
    """ledger 1 行分の正規化 JSON(key 順を固定して差分を読みやすくする)。"""

    return json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def append_jsonl(path: Path | str, record: Mapping[str, Any]) -> Path:
    """1 レコードを追記して fsync する。"""

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json_line(record) + "\n"
    try:
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())
    except OSError as exc:
        raise LedgerError(f"failed to append to ledger {path}") from exc
    return path


def read_jsonl(path: Path | str) -> Iterator[dict[str, Any]]:
    """ledger を先頭から読む。存在しなければ空。壊れた行は fail-closed。"""

    path = Path(path)
    if not path.exists():
        return
    with path.open("r", encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, 1):
            text = line.strip()
            if not text:
                continue
            if not line.endswith("\n"):
                # crash 直後の切れた末尾行。黙って捨てると欠損が見えなくなる。
                raise LedgerError(f"{path}:{line_no}: truncated ledger line")
            try:
                value = json.loads(text)
            except json.JSONDecodeError as exc:
                raise LedgerError(f"{path}:{line_no}: invalid ledger JSON") from exc
            if not isinstance(value, dict):
                raise LedgerError(f"{path}:{line_no}: ledger row must be an object")
            yield value


def atomic_write_json(path: Path | str, payload: Any) -> Path:
    """完全な JSON だけが読み手に見えるよう temp → fsync → rename する。"""

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2)
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_name = handle.name
            handle.write(text)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
        temporary_name = None
        _fsync_directory(path.parent)
    except OSError as exc:
        raise LedgerError(f"failed to write JSON artifact {path}") from exc
    finally:
        if temporary_name is not None:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass
    return path

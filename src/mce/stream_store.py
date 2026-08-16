"""WebSocket フレームを到着順の gzip JSONL として保存する。

REST 向けの :mod:`mce.store` は API 応答ごとに gzip を開閉するが、高頻度な
WebSocket feed では接続中ずっと開いた writer が必要になる。このモジュールは
1 接続 = 1 ファイルとし、受信フレームだけでなく subscribe/ping とローカルの
接続イベントも同じ連番に記録する。
"""

from __future__ import annotations

import gzip
import io
import json
import os
import re
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


SCHEMA_VERSION = 1
_SAFE_STREAM_NAME = re.compile(r"^[A-Za-z0-9_.-]+$")


class StreamWriteError(RuntimeError):
    """raw stream を安全に保存できなかった。collector は継続してはならない。"""


class GzipJsonlStreamWriter:
    """接続単位の append-only gzip JSONL writer。

    ``frame_no`` は local/out/in を含むファイル内の全レコードに対して単調増加する。
    したがって、複数 channel を同じ WebSocket 接続で購読しても到着順を失わない。

    Parameters
    ----------
    raw_root:
        通常は ``data/raw``。実ファイルは
        ``raw_root/okx/ws/<stream>/YYYY/MM/DD`` 以下に作る。
    stream:
        接続の論理名。現在は ``public`` または ``business``。
    flush_every:
        この件数ごとに gzip/TextIO のバッファを flush する。既定値 1 はデータの
        消失窓を最小化する。耐久化の ``fsync`` は clean close 時に行う。
    """

    def __init__(
        self,
        raw_root: Path,
        stream: str,
        *,
        session_id: str | None = None,
        started_at: datetime | None = None,
        flush_every: int = 1,
        time_ns: Callable[[], int] = time.time_ns,
        monotonic_ns: Callable[[], int] = time.monotonic_ns,
    ) -> None:
        if not _SAFE_STREAM_NAME.fullmatch(stream):
            raise ValueError(f"unsafe stream name: {stream!r}")
        if flush_every < 1:
            raise ValueError("flush_every must be >= 1")

        started_at = started_at or datetime.now(timezone.utc)
        if started_at.tzinfo is None:
            raise ValueError("started_at must be timezone-aware")
        started_at = started_at.astimezone(timezone.utc)

        self.stream = stream
        self.session_id = session_id or uuid.uuid4().hex
        if not _SAFE_STREAM_NAME.fullmatch(self.session_id):
            raise ValueError(f"unsafe session_id: {self.session_id!r}")
        self._time_ns = time_ns
        self._monotonic_ns = monotonic_ns
        self._flush_every = flush_every
        self._since_flush = 0
        self._next_frame_no = 0
        self._closed = False
        self._lock = threading.Lock()

        directory = (
            Path(raw_root)
            / "okx"
            / "ws"
            / stream
            / started_at.strftime("%Y")
            / started_at.strftime("%m")
            / started_at.strftime("%d")
        )
        try:
            directory.mkdir(parents=True, exist_ok=True)
            stamp = started_at.strftime("%Y%m%dT%H%M%S.%fZ")
            self.path = directory / f"{stamp}_{self.session_id}.jsonl.gz"
            # 完了ファイルだけを downstream が読むよう、書込み中は明示的な
            # ``.partial`` suffix に隔離する。process crash / SIGKILL 時は partial が
            # 残り、正常な gzip と誤認されない。
            self.partial_path = self.path.with_name(f"{self.path.name}.partial")
            self._raw_file = self.partial_path.open("xb")
            self._gzip_file = gzip.GzipFile(
                filename=self.path.name,
                mode="wb",
                fileobj=self._raw_file,
                mtime=int(started_at.timestamp()),
            )
            self._file = io.TextIOWrapper(
                self._gzip_file,
                encoding="utf-8",
                newline="\n",
            )
        except OSError as exc:
            raise StreamWriteError(f"failed to open raw stream under {directory}") from exc

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def records_written(self) -> int:
        return self._next_frame_no

    def append(
        self,
        payload: str,
        *,
        direction: str,
        kind: str = "frame",
        received_at_ns: int | None = None,
        monotonic_ns: int | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> int:
        """1 レコードを保存し、その ``frame_no`` を返す。

        ``payload`` は受け取った文字列を再 parse/re-serializeせずそのまま格納する。
        JSONL の外側 wrapper を JSON 化する際のエスケープは行われるが、読み戻した
        ``payload`` 文字列は入力と一致する。
        """

        if not isinstance(payload, str):
            raise TypeError("payload must be str")
        if direction not in {"in", "out", "local"}:
            raise ValueError(f"unsupported direction: {direction!r}")

        with self._lock:
            if self._closed:
                raise StreamWriteError("raw stream is already closed")
            frame_no = self._next_frame_no
            record: dict[str, Any] = {
                "schema_version": SCHEMA_VERSION,
                "stream": self.stream,
                "session_id": self.session_id,
                "frame_no": frame_no,
                "direction": direction,
                "kind": kind,
                "received_at_ns": received_at_ns if received_at_ns is not None else self._time_ns(),
                "monotonic_ns": monotonic_ns if monotonic_ns is not None else self._monotonic_ns(),
                "payload": payload,
            }
            if metadata:
                record["metadata"] = metadata

            try:
                line = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
                self._file.write(line)
                self._file.write("\n")
                self._next_frame_no += 1
                self._since_flush += 1
                if self._since_flush >= self._flush_every:
                    self._file.flush()
                    self._since_flush = 0
            except (OSError, ValueError, TypeError) as exc:
                raise StreamWriteError(f"failed to append frame {frame_no} to {self.path}") from exc
            return frame_no

    def append_event(self, event: str, **details: Any) -> int:
        """接続・再接続などのローカルイベントを同じ stream に記録する。"""

        payload = json.dumps(details, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        return self.append(payload, direction="local", kind=event)

    def flush(self) -> None:
        with self._lock:
            if self._closed:
                return
            try:
                self._file.flush()
                self._since_flush = 0
            except OSError as exc:
                raise StreamWriteError(f"failed to flush {self.path}") from exc

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            try:
                # TextIO -> gzip footer -> underlying fd の順で確定し、fdをdurableにして
                # から atomic rename する。GzipFile は外から渡した fileobj を閉じない。
                self._file.flush()
                self._file.close()
                self._raw_file.flush()
                os.fsync(self._raw_file.fileno())
                self._raw_file.close()
                os.replace(self.partial_path, self.path)

                # rename 自体のdurabilityも可能な環境では確保する。directory fsyncを
                # サポートしないplatformではファイル本体の確定を優先して継続する。
                directory_fd: int | None = None
                try:
                    directory_fd = os.open(self.path.parent, os.O_RDONLY)
                    os.fsync(directory_fd)
                except OSError:
                    pass
                finally:
                    if directory_fd is not None:
                        os.close(directory_fd)
            except OSError as exc:
                raise StreamWriteError(f"failed to close {self.path}") from exc
            finally:
                self._closed = True

    def __enter__(self) -> GzipJsonlStreamWriter:
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()

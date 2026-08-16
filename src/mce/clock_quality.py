"""Linux kernel clock quality sampling via read-only ``adjtimex(2)``.

The microstructure protocol compares exchange timestamps with the collector's
wall clock.  A wall-clock timestamp without a contemporaneous synchronization
measurement is not enough to establish that ordering, so this module stores a
small, durable raw record at startup and at a fixed cadence.

Calling ``adjtimex`` with ``modes == 0`` is a read-only operation available to
unprivileged processes.  The ctypes layout below follows glibc's LP64
``struct timex``.  Unsupported platforms/ABIs fail explicitly instead of
silently producing a clock-quality gap.
"""

from __future__ import annotations

import asyncio
import ctypes
import gzip
import json
import os
import sys
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


CLOCK_QUALITY_SCHEMA_VERSION = 1
CLOCK_QUALITY_SOURCE = "linux_kernel_adjtimex"

# Linux <sys/timex.h> status flags.
STA_UNSYNC = 0x0040
STA_NANO = 0x2000

# adjtimex return states.
TIME_OK = 0
TIME_INS = 1
TIME_DEL = 2
TIME_OOP = 3
TIME_WAIT = 4
TIME_ERROR = 5
_STATE_NAMES = {
    TIME_OK: "TIME_OK",
    TIME_INS: "TIME_INS",
    TIME_DEL: "TIME_DEL",
    TIME_OOP: "TIME_OOP",
    TIME_WAIT: "TIME_WAIT",
    TIME_ERROR: "TIME_ERROR",
}


class ClockQualityUnavailable(RuntimeError):
    """Kernel clock quality cannot be observed, so protocol T0 is unavailable."""


class ClockQualityWriteError(RuntimeError):
    """A clock sample could not be durably published."""


class _Timeval(ctypes.Structure):
    _fields_ = [
        ("tv_sec", ctypes.c_long),
        ("tv_usec", ctypes.c_long),
    ]


class _Timex(ctypes.Structure):
    """glibc LP64 ``struct timex`` (208 bytes on supported Linux ABIs)."""

    _fields_ = [
        ("modes", ctypes.c_uint),
        ("offset", ctypes.c_long),
        ("freq", ctypes.c_long),
        ("maxerror", ctypes.c_long),
        ("esterror", ctypes.c_long),
        ("status", ctypes.c_int),
        ("constant", ctypes.c_long),
        ("precision", ctypes.c_long),
        ("tolerance", ctypes.c_long),
        ("time", _Timeval),
        ("tick", ctypes.c_long),
        ("ppsfreq", ctypes.c_long),
        ("jitter", ctypes.c_long),
        ("shift", ctypes.c_int),
        ("stabil", ctypes.c_long),
        ("jitcnt", ctypes.c_long),
        ("calcnt", ctypes.c_long),
        ("errcnt", ctypes.c_long),
        ("stbcnt", ctypes.c_long),
        ("tai", ctypes.c_int),
        ("_padding", ctypes.c_int * 11),
    ]


@dataclass(frozen=True)
class ClockQualitySample:
    """The protocol-relevant read-only subset of ``struct timex``.

    ``offset`` changes unit with ``STA_NANO``.  Linux documents
    ``maxerror``, ``esterror`` and ``precision`` in microseconds regardless of
    that flag, so their units remain explicit and stable in the raw record.
    """

    state: int
    status: int
    offset: int
    maxerror: int
    esterror: int
    precision: int

    @property
    def state_name(self) -> str:
        return _STATE_NAMES[self.state]

    @property
    def sta_nano(self) -> bool:
        return bool(self.status & STA_NANO)

    @property
    def sta_unsync(self) -> bool:
        return bool(self.status & STA_UNSYNC)

    @property
    def synchronized(self) -> bool:
        return self.state != TIME_ERROR and not self.sta_unsync

    @property
    def offset_unit(self) -> str:
        return "nanoseconds" if self.sta_nano else "microseconds"

    @property
    def offset_ns(self) -> int:
        return self.offset if self.sta_nano else self.offset * 1_000

    def as_record_fields(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "state_name": self.state_name,
            "status": self.status,
            "sta_nano": self.sta_nano,
            "sta_unsync": self.sta_unsync,
            "synchronized": self.synchronized,
            "offset": self.offset,
            "offset_unit": self.offset_unit,
            "offset_ns": self.offset_ns,
            "maxerror": self.maxerror,
            "maxerror_unit": "microseconds",
            "maxerror_ns": self.maxerror * 1_000,
            "esterror": self.esterror,
            "esterror_unit": "microseconds",
            "esterror_ns": self.esterror * 1_000,
            "precision": self.precision,
            "precision_unit": "microseconds",
            "precision_ns": self.precision * 1_000,
        }


AdjtimexFunction = Callable[[Any], int]
ClockReader = Callable[[], ClockQualitySample]
ClockWriter = Callable[..., Path]


def _unavailable(reason: str) -> ClockQualityUnavailable:
    return ClockQualityUnavailable(
        f"clock quality unavailable; microstructure protocol T0 cannot be "
        f"evaluated: {reason}"
    )


def _load_adjtimex() -> AdjtimexFunction:
    if not sys.platform.startswith("linux"):
        raise _unavailable(f"adjtimex is Linux-only (platform={sys.platform!r})")
    # This definition intentionally fails closed on an unverified ABI.  glibc's
    # 64-bit struct is 208 bytes on x86_64 and aarch64.
    if (
        ctypes.sizeof(ctypes.c_void_p) != 8
        or ctypes.sizeof(ctypes.c_long) != 8
        or ctypes.sizeof(_Timex) != 208
    ):
        raise _unavailable(
            "unsupported struct timex ABI "
            f"(pointer={ctypes.sizeof(ctypes.c_void_p)}, "
            f"long={ctypes.sizeof(ctypes.c_long)}, timex={ctypes.sizeof(_Timex)})"
        )
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        function = libc.adjtimex
    except (OSError, AttributeError) as exc:
        raise _unavailable(f"libc adjtimex symbol is unavailable: {exc}") from exc
    function.argtypes = [ctypes.POINTER(_Timex)]
    function.restype = ctypes.c_int
    return function


def read_adjtimex(
    adjtimex_function: AdjtimexFunction | None = None,
) -> ClockQualitySample:
    """Read kernel clock state without modifying it.

    ``adjtimex_function`` is injectable for fixtures.  Production calls load
    libc only after checking the supported LP64 Linux ABI.
    """

    function = adjtimex_function or _load_adjtimex()
    value = _Timex()  # zero initialization is important: modes == 0 is read-only.
    ctypes.set_errno(0)
    state = int(function(ctypes.byref(value)))
    if state < 0:
        error_number = ctypes.get_errno()
        description = os.strerror(error_number) if error_number else "unknown error"
        raise _unavailable(
            f"adjtimex failed with errno {error_number} ({description})"
        )
    if state not in _STATE_NAMES:
        raise _unavailable(
            f"adjtimex returned unexpected state {state}; struct ABI may be incompatible"
        )
    return ClockQualitySample(
        state=state,
        status=int(value.status),
        offset=int(value.offset),
        maxerror=int(value.maxerror),
        esterror=int(value.esterror),
        precision=int(value.precision),
    )


def _iso_utc_from_ns(value: int) -> str:
    seconds, nanoseconds = divmod(value, 1_000_000_000)
    timestamp = datetime.fromtimestamp(seconds, tz=timezone.utc).replace(
        microsecond=nanoseconds // 1_000
    )
    return timestamp.isoformat(timespec="microseconds").replace("+00:00", "Z")


def write_clock_quality_sample(
    raw_dir: Path,
    sample: ClockQualitySample,
    *,
    received_at_ns: int | None = None,
    monotonic_ns: int | None = None,
) -> Path:
    """Publish one append-only gzip JSONL sample using partial/fsync/rename."""

    received_at_ns = time.time_ns() if received_at_ns is None else received_at_ns
    monotonic_ns = time.monotonic_ns() if monotonic_ns is None else monotonic_ns
    if received_at_ns < 0 or monotonic_ns < 0:
        raise ValueError("clock sample timestamps must be nonnegative")
    received_at = datetime.fromtimestamp(
        received_at_ns // 1_000_000_000, tz=timezone.utc
    )
    directory = (
        Path(raw_dir)
        / "host"
        / "clock_quality"
        / received_at.strftime("%Y")
        / received_at.strftime("%m")
        / received_at.strftime("%d")
    )
    record = {
        "schema_version": CLOCK_QUALITY_SCHEMA_VERSION,
        "source": CLOCK_QUALITY_SOURCE,
        "received_at": _iso_utc_from_ns(received_at_ns),
        "received_at_ns": received_at_ns,
        "monotonic_ns": monotonic_ns,
        **sample.as_record_fields(),
    }
    stamp = received_at.strftime("%Y%m%dT%H%M%SZ")
    identity = uuid.uuid4().hex
    path = directory / (
        f"{stamp}_{received_at_ns}_{monotonic_ns}_{identity}.jsonl.gz"
    )
    partial_path = path.with_name(f"{path.name}.partial")

    try:
        directory.mkdir(parents=True, exist_ok=True)
        with gzip.open(
            partial_path, mode="xt", encoding="utf-8", newline="\n"
        ) as handle:
            handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")))
            handle.write("\n")
        # gzip.open has emitted the footer; make the completed bytes durable
        # before publishing the filename atomically.
        with partial_path.open("rb") as handle:
            os.fsync(handle.fileno())
        os.replace(partial_path, path)
        directory_fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except OSError as exc:
        raise ClockQualityWriteError(
            f"failed to durably publish clock sample under {directory}"
        ) from exc
    return path


async def sample_clock_quality(
    stop_event: asyncio.Event,
    raw_dir: Path,
    *,
    interval_seconds: float = 60.0,
    reader: ClockReader = read_adjtimex,
    writer: ClockWriter = write_clock_quality_sample,
) -> None:
    """Sample immediately, then periodically, while remaining stop-responsive."""

    if interval_seconds <= 0:
        raise ValueError("clock sample interval must be > 0")
    while not stop_event.is_set():
        sample = reader()
        writer(raw_dir, sample)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval_seconds)
            return
        except asyncio.TimeoutError:
            continue

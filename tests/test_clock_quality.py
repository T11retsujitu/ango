import asyncio
import ctypes
import errno
import gzip
import json
import time
from collections import deque

import pytest

from mce.clock_quality import (
    CLOCK_QUALITY_SOURCE,
    STA_NANO,
    STA_UNSYNC,
    TIME_ERROR,
    TIME_OK,
    ClockQualitySample,
    ClockQualityUnavailable,
    _Timex,
    read_adjtimex,
    sample_clock_quality,
    write_clock_quality_sample,
)


def _populate(pointer, **values):
    timex = ctypes.cast(pointer, ctypes.POINTER(_Timex)).contents
    assert timex.modes == 0
    for field, value in values.items():
        setattr(timex, field, value)


def test_read_adjtimex_is_read_only_and_interprets_sta_nano_and_unsync():
    def fake_adjtimex(pointer):
        _populate(
            pointer,
            status=STA_NANO | STA_UNSYNC,
            offset=-12_345,
            maxerror=20_001,
            esterror=321,
            precision=1,
        )
        return TIME_ERROR

    sample = read_adjtimex(fake_adjtimex)

    assert sample == ClockQualitySample(
        state=TIME_ERROR,
        status=STA_NANO | STA_UNSYNC,
        offset=-12_345,
        maxerror=20_001,
        esterror=321,
        precision=1,
    )
    assert sample.state_name == "TIME_ERROR"
    assert sample.sta_nano is True
    assert sample.sta_unsync is True
    assert sample.synchronized is False
    assert sample.offset_unit == "nanoseconds"
    assert sample.offset_ns == -12_345
    fields = sample.as_record_fields()
    assert fields["maxerror_unit"] == "microseconds"
    assert fields["maxerror_ns"] == 20_001_000
    assert fields["esterror_ns"] == 321_000
    assert fields["precision_unit"] == "microseconds"
    assert fields["precision_ns"] == 1_000


def test_read_adjtimex_converts_microsecond_offset_only_when_sta_nano_clear():
    def fake_adjtimex(pointer):
        _populate(
            pointer,
            status=0,
            offset=5_432,
            maxerror=100,
            esterror=50,
            precision=1,
        )
        return TIME_OK

    sample = read_adjtimex(fake_adjtimex)

    assert sample.offset_unit == "microseconds"
    assert sample.offset_ns == 5_432_000
    assert sample.synchronized is True


def test_read_adjtimex_failure_is_explicitly_t0_unavailable():
    def failing_adjtimex(pointer):
        ctypes.set_errno(errno.EPERM)
        return -1

    with pytest.raises(
        ClockQualityUnavailable,
        match=r"protocol T0 cannot be evaluated.*errno 1",
    ):
        read_adjtimex(failing_adjtimex)


def test_unexpected_kernel_state_is_treated_as_possible_abi_failure():
    def invalid_adjtimex(pointer):
        _populate(pointer, status=0)
        return 99

    with pytest.raises(ClockQualityUnavailable, match="unexpected state 99"):
        read_adjtimex(invalid_adjtimex)


def test_clock_sample_is_utc_partitioned_atomic_and_self_describing(tmp_path):
    sample = ClockQualitySample(
        state=TIME_ERROR,
        status=STA_UNSYNC,
        offset=-7,
        maxerror=101,
        esterror=12,
        precision=1,
    )
    received_at_ns = 1_786_838_400_123_456_789
    path = write_clock_quality_sample(
        tmp_path,
        sample,
        received_at_ns=received_at_ns,
        monotonic_ns=9_876_543_210,
    )

    assert path.parent == tmp_path / "host/clock_quality/2026/08/16"
    assert path.suffixes == [".jsonl", ".gz"]
    assert list(tmp_path.glob("host/clock_quality/**/*.partial")) == []
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        rows = [json.loads(line) for line in handle]
    assert len(rows) == 1
    row = rows[0]
    assert row["schema_version"] == 1
    assert row["source"] == CLOCK_QUALITY_SOURCE
    assert row["received_at"] == "2026-08-16T00:00:00.123456Z"
    assert row["received_at_ns"] == received_at_ns
    assert row["monotonic_ns"] == 9_876_543_210
    assert row["state"] == TIME_ERROR
    assert row["status"] == STA_UNSYNC
    assert row["sta_unsync"] is True
    assert row["synchronized"] is False
    assert row["offset"] == -7
    assert row["offset_unit"] == "microseconds"
    assert row["offset_ns"] == -7_000


def test_async_sampler_samples_at_startup_and_stops_during_long_interval(tmp_path):
    async def scenario():
        writes = []
        first_write = asyncio.Event()
        stop = asyncio.Event()

        def reader():
            return ClockQualitySample(TIME_OK, 0, len(writes), 1, 1, 1)

        def writer(raw_dir, sample):
            writes.append((raw_dir, sample))
            first_write.set()
            return tmp_path / f"sample-{len(writes)}"

        task = asyncio.create_task(
            sample_clock_quality(
                stop,
                tmp_path,
                interval_seconds=60,
                reader=reader,
                writer=writer,
            )
        )
        await asyncio.wait_for(first_write.wait(), timeout=0.1)
        started = time.monotonic()
        stop.set()
        await asyncio.wait_for(task, timeout=0.1)

        assert time.monotonic() - started < 0.1
        assert len(writes) == 1
        assert writes[0][0] == tmp_path

    asyncio.run(scenario())


def test_async_sampler_repeats_at_configured_interval(tmp_path):
    async def scenario():
        writes = []
        stop = asyncio.Event()
        values = deque(range(10))

        def reader():
            return ClockQualitySample(TIME_OK, 0, values.popleft(), 1, 1, 1)

        def writer(raw_dir, sample):
            writes.append(sample.offset)
            if len(writes) == 3:
                stop.set()
            return tmp_path / f"sample-{len(writes)}"

        await asyncio.wait_for(
            sample_clock_quality(
                stop,
                tmp_path,
                interval_seconds=0.005,
                reader=reader,
                writer=writer,
            ),
            timeout=0.1,
        )
        assert writes == [0, 1, 2]

    asyncio.run(scenario())

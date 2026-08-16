import asyncio
import gzip
import json
from collections import deque
from datetime import datetime, timezone
from unittest.mock import Mock

import pytest

from mce.collect_microstructure import (
    InstrumentMetadataError,
    _build_parser,
    _refresh_instrument_metadata_daily,
    collect_microstructure,
    collect_instrument_metadata_snapshot,
    seconds_until_next_utc_day,
    validate_instrument_metadata,
    write_instrument_metadata,
)
from mce.clock_quality import ClockQualitySample, ClockQualityUnavailable, TIME_OK
from mce.okx import OkxClient


INST_ID = "BTC-USDT-SWAP"


def _metadata_response():
    return {
        "code": "0",
        "msg": "",
        "data": [
            {
                "instId": INST_ID,
                "instType": "SWAP",
                "ctVal": "0.01",
                "ctMult": "1",
                "ctValCcy": "BTC",
                "tickSz": "0.1",
                "lotSz": "0.01",
                "state": "live",
            }
        ],
    }


def _read_metadata(path):
    with gzip.open(path, "rt", encoding="utf-8") as file:
        return [json.loads(line) for line in file]


def test_okx_client_instruments_uses_public_endpoint():
    client = object.__new__(OkxClient)
    client._get = Mock(return_value=_metadata_response())

    assert client.instruments("SWAP", INST_ID) == _metadata_response()
    client._get.assert_called_once_with(
        "/api/v5/public/instruments",
        {"instType": "SWAP", "instId": INST_ID},
    )


def test_metadata_raw_is_atomic_and_preserves_response(tmp_path):
    response = _metadata_response()
    received_at_ns = 1_786_838_400_123_456_789
    path = write_instrument_metadata(
        tmp_path,
        INST_ID,
        response,
        received_at_ns=received_at_ns,
    )

    assert path.exists()
    assert list(tmp_path.glob("okx/rest/instruments/**/*.partial")) == []
    rows = _read_metadata(path)
    assert rows == [
        {
            "schema_version": 1,
            "source": "okx",
            "endpoint": "/api/v5/public/instruments",
            "request": {"instType": "SWAP", "instId": INST_ID},
            "received_at_ns": received_at_ns,
            "response": response,
        }
    ]
    assert validate_instrument_metadata(response, INST_ID)["ctVal"] == "0.01"


def test_metadata_validation_rejects_missing_contract_conversion_field():
    response = _metadata_response()
    del response["data"][0]["ctVal"]

    with pytest.raises(InstrumentMetadataError, match="ctVal"):
        validate_instrument_metadata(response, INST_ID)


def test_startup_snapshot_fetch_is_separated_and_testable(tmp_path):
    calls = []

    def fetcher(inst_id):
        calls.append(inst_id)
        return _metadata_response()

    path = asyncio.run(
        collect_instrument_metadata_snapshot(tmp_path, INST_ID, fetcher)
    )

    assert calls == [INST_ID]
    assert _read_metadata(path)[0]["response"] == _metadata_response()


def test_seconds_until_next_utc_day():
    now = datetime(2026, 8, 16, 23, 59, 30, tzinfo=timezone.utc)
    assert seconds_until_next_utc_day(now) == 30


def test_daily_metadata_refresher_takes_new_snapshot(tmp_path, monkeypatch):
    async def scenario():
        calls = deque()
        stop = asyncio.Event()

        def fetcher(inst_id):
            calls.append(inst_id)
            return _metadata_response()

        monkeypatch.setattr(
            "mce.collect_microstructure.seconds_until_next_utc_day",
            lambda: 0.005,
        )
        task = asyncio.create_task(
            _refresh_instrument_metadata_daily(stop, tmp_path, INST_ID, fetcher)
        )
        for _ in range(100):
            if calls:
                break
            await asyncio.sleep(0.002)
        else:
            raise AssertionError("daily metadata snapshot was not fetched")
        stop.set()
        await asyncio.wait_for(task, timeout=0.2)

        assert list(calls) == [INST_ID]
        assert len(list(tmp_path.glob("okx/rest/instruments/**/*.jsonl.gz"))) == 1

    asyncio.run(scenario())


def test_cli_enables_60_second_clock_sampling_by_default_and_can_disable_it():
    defaults = _build_parser().parse_args([])
    assert defaults.clock_sample_seconds == 60.0
    assert defaults.skip_clock_quality is False

    disabled = _build_parser().parse_args(["--skip-clock-quality"])
    assert disabled.skip_clock_quality is True


def test_collector_clock_reader_and_writer_are_injectable(tmp_path):
    async def scenario():
        writes = []

        async def hanging_connector(url):
            await asyncio.Event().wait()

        def reader():
            return ClockQualitySample(TIME_OK, 0, 123, 5, 2, 1)

        def writer(raw_dir, sample):
            writes.append((raw_dir, sample))
            return tmp_path / "fixture-clock.jsonl.gz"

        await asyncio.wait_for(
            collect_microstructure(
                raw_dir=tmp_path,
                inst_id=INST_ID,
                duration=0.02,
                connector=hanging_connector,
                collect_instrument_metadata=False,
                collect_clock_quality=True,
                clock_sample_seconds=60,
                clock_reader=reader,
                clock_writer=writer,
            ),
            timeout=0.2,
        )
        assert len(writes) == 1
        assert writes[0][0] == tmp_path
        assert writes[0][1].offset == 123

    asyncio.run(scenario())


def test_collector_can_explicitly_disable_clock_sampling_for_fixture(tmp_path):
    async def scenario():
        calls = 0

        async def hanging_connector(url):
            await asyncio.Event().wait()

        def reader():
            nonlocal calls
            calls += 1
            raise AssertionError("disabled reader must not be called")

        await asyncio.wait_for(
            collect_microstructure(
                raw_dir=tmp_path,
                inst_id=INST_ID,
                duration=0.01,
                connector=hanging_connector,
                collect_instrument_metadata=False,
                collect_clock_quality=False,
                clock_reader=reader,
            ),
            timeout=0.2,
        )
        assert calls == 0

    asyncio.run(scenario())


def test_collector_fails_closed_when_clock_quality_is_unavailable(tmp_path):
    async def scenario():
        async def hanging_connector(url):
            await asyncio.Event().wait()

        def unavailable_reader():
            raise ClockQualityUnavailable(
                "clock quality unavailable; microstructure protocol T0 cannot be evaluated"
            )

        with pytest.raises(ClockQualityUnavailable, match="T0 cannot be evaluated"):
            await asyncio.wait_for(
                collect_microstructure(
                    raw_dir=tmp_path,
                    inst_id=INST_ID,
                    duration=1,
                    connector=hanging_connector,
                    collect_instrument_metadata=False,
                    collect_clock_quality=True,
                    clock_reader=unavailable_reader,
                ),
                timeout=0.2,
            )

    asyncio.run(scenario())

"""OKX 約定・BBO・板の live raw collector CLI。

例::

    python -m mce.collect_microstructure --duration 60

``--duration`` を省略すると SIGINT/SIGTERM まで動作する。
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import gzip
import inspect
import json
import logging
import os
import re
import signal
import time
from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime, time as datetime_time, timedelta, timezone
from pathlib import Path
from typing import Any

from mce import config
from mce.clock_quality import (
    ClockReader,
    ClockWriter,
    read_adjtimex,
    sample_clock_quality,
    write_clock_quality_sample,
)
from mce.okx import AsyncOkxClient
from mce.okx_ws import (
    BUSINESS_WS_URL,
    PUBLIC_WS_URL,
    ConnectionStats,
    Connector,
    OkxWsConnection,
    business_subscriptions,
    default_connector,
    public_subscriptions,
)
from mce.stream_store import GzipJsonlStreamWriter


INSTRUMENT_METADATA_SCHEMA_VERSION = 1
_SAFE_INST_ID = re.compile(r"^[A-Za-z0-9_.-]+$")
_REQUIRED_SWAP_METADATA = {
    "instId",
    "instType",
    "ctVal",
    "ctMult",
    "ctValCcy",
    "tickSz",
    "lotSz",
}
MetadataFetcher = Callable[
    [str], Mapping[str, Any] | Awaitable[Mapping[str, Any]]
]


class InstrumentMetadataError(RuntimeError):
    """SWAP数量を安全に解釈するためのmetadataが取得・検証できなかった。"""


async def fetch_instrument_metadata(inst_id: str) -> Mapping[str, Any]:
    """OKX RESTから対象SWAPのmetadata response bodyを取得する。"""

    client = AsyncOkxClient()
    try:
        return await client.instruments("SWAP", inst_id)
    finally:
        await client.close()


def validate_instrument_metadata(
    response: Mapping[str, Any], inst_id: str
) -> Mapping[str, Any]:
    """対象instrument 1件とcontract換算に必要なfieldの存在をfail-closed検証する。"""

    if str(response.get("code")) != "0":
        raise InstrumentMetadataError(
            f"instrument metadata response failed: {response.get('code')} {response.get('msg')}"
        )
    data = response.get("data")
    if not isinstance(data, list):
        raise InstrumentMetadataError("instrument metadata response has no data array")
    matches = [
        item
        for item in data
        if isinstance(item, Mapping) and item.get("instId") == inst_id
    ]
    if len(matches) != 1:
        raise InstrumentMetadataError(
            f"expected exactly one metadata row for {inst_id}, got {len(matches)}"
        )
    instrument = matches[0]
    missing = sorted(
        field
        for field in _REQUIRED_SWAP_METADATA
        if field not in instrument or instrument[field] in (None, "")
    )
    if missing:
        raise InstrumentMetadataError(
            f"instrument metadata for {inst_id} is missing fields: {missing}"
        )
    if instrument.get("instType") != "SWAP":
        raise InstrumentMetadataError(
            f"instrument metadata for {inst_id} is not SWAP: {instrument.get('instType')!r}"
        )
    return instrument


def write_instrument_metadata(
    raw_dir: Path,
    inst_id: str,
    response: Mapping[str, Any],
    *,
    received_at_ns: int | None = None,
) -> Path:
    """REST responseをgzip JSONLへfsyncし、partialからatomic publishする。"""

    if not _SAFE_INST_ID.fullmatch(inst_id):
        raise ValueError(f"unsafe inst_id: {inst_id!r}")
    received_at_ns = received_at_ns if received_at_ns is not None else time.time_ns()
    received_at = datetime.fromtimestamp(received_at_ns / 1_000_000_000, tz=timezone.utc)
    directory = (
        Path(raw_dir)
        / "okx"
        / "rest"
        / "instruments"
        / received_at.strftime("%Y")
        / received_at.strftime("%m")
        / received_at.strftime("%d")
    )
    directory.mkdir(parents=True, exist_ok=True)
    stamp = received_at.strftime("%Y%m%dT%H%M%S.%fZ")
    path = directory / f"{stamp}_{received_at_ns}_{inst_id}.jsonl.gz"
    partial_path = path.with_name(f"{path.name}.partial")
    record = {
        "schema_version": INSTRUMENT_METADATA_SCHEMA_VERSION,
        "source": "okx",
        "endpoint": "/api/v5/public/instruments",
        "request": {"instType": "SWAP", "instId": inst_id},
        "received_at_ns": received_at_ns,
        "response": response,
    }
    try:
        with gzip.open(partial_path, mode="xt", encoding="utf-8", newline="\n") as file:
            file.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")))
            file.write("\n")
        # gzip footerまでcloseされたpartialをdurableにしてから公開する。
        with partial_path.open("rb") as file:
            os.fsync(file.fileno())
        os.replace(partial_path, path)
        directory_fd: int | None = None
        try:
            directory_fd = os.open(directory, os.O_RDONLY)
            os.fsync(directory_fd)
        except OSError:
            pass
        finally:
            if directory_fd is not None:
                os.close(directory_fd)
    except OSError as exc:
        raise InstrumentMetadataError(
            f"failed to store instrument metadata under {directory}"
        ) from exc
    return path


async def collect_instrument_metadata_snapshot(
    raw_dir: Path,
    inst_id: str,
    fetcher: MetadataFetcher,
) -> Path:
    """1回分を取得・raw確定し、contract換算fieldを検証する。"""

    fetched = fetcher(inst_id)
    response = await fetched if inspect.isawaitable(fetched) else fetched
    # 不完全なresponseも調査可能にするためrawを先に確定し、その後fail-closed検証。
    path = write_instrument_metadata(raw_dir, inst_id, response)
    validate_instrument_metadata(response, inst_id)
    return path


def seconds_until_next_utc_day(now: datetime | None = None) -> float:
    """startup snapshotとは別に次のUTC日を保存するまでの待機秒数。"""

    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    now = now.astimezone(timezone.utc)
    next_day = datetime.combine(
        now.date() + timedelta(days=1), datetime_time.min, tzinfo=timezone.utc
    )
    return max(0.0, (next_day - now).total_seconds())


async def _refresh_instrument_metadata_daily(
    stop_event: asyncio.Event,
    raw_dir: Path,
    inst_id: str,
    fetcher: MetadataFetcher,
) -> None:
    """各UTC日境界で新しいmetadata raw snapshotを保存する。"""

    while not stop_event.is_set():
        try:
            await asyncio.wait_for(
                stop_event.wait(), timeout=seconds_until_next_utc_day()
            )
            return
        except asyncio.TimeoutError:
            await collect_instrument_metadata_snapshot(raw_dir, inst_id, fetcher)


def _stats_dict(stats: ConnectionStats) -> dict[str, Any]:
    return dict(vars(stats))


async def collect_microstructure(
    *,
    raw_dir: Path,
    inst_id: str,
    duration: float | None = None,
    public_url: str = PUBLIC_WS_URL,
    business_url: str = BUSINESS_WS_URL,
    connector: Connector = default_connector,
    heartbeat_seconds: float = 20.0,
    pong_timeout_seconds: float = 10.0,
    subscribe_timeout_seconds: float = 10.0,
    reconnect_min_seconds: float = 1.0,
    reconnect_max_seconds: float = 60.0,
    session_rotation_seconds: float | None = 3600.0,
    connect_timeout_seconds: float = 20.0,
    cancellation_timeout_seconds: float = 1.0,
    collect_instrument_metadata: bool = True,
    metadata_fetcher: MetadataFetcher | None = None,
    collect_clock_quality: bool = True,
    clock_sample_seconds: float = 60.0,
    clock_reader: ClockReader | None = None,
    clock_writer: ClockWriter | None = None,
    stop_event: asyncio.Event | None = None,
) -> dict[str, ConnectionStats]:
    """public/business の2接続を同時に動かし、終了時の統計を返す。"""

    if duration is not None and duration < 0:
        raise ValueError("duration must be >= 0")
    if collect_clock_quality and clock_sample_seconds <= 0:
        raise ValueError("clock_sample_seconds must be > 0")
    stop_event = stop_event or asyncio.Event()

    active_metadata_fetcher = metadata_fetcher or fetch_instrument_metadata
    if collect_instrument_metadata:
        await collect_instrument_metadata_snapshot(
            raw_dir, inst_id, active_metadata_fetcher
        )

    def writer_factory(name: str) -> GzipJsonlStreamWriter:
        return GzipJsonlStreamWriter(raw_dir, name)

    common = {
        "writer_factory": writer_factory,
        "connector": connector,
        "heartbeat_seconds": heartbeat_seconds,
        "pong_timeout_seconds": pong_timeout_seconds,
        "subscribe_timeout_seconds": subscribe_timeout_seconds,
        "reconnect_min_seconds": reconnect_min_seconds,
        "reconnect_max_seconds": reconnect_max_seconds,
        "session_rotation_seconds": session_rotation_seconds,
        "connect_timeout_seconds": connect_timeout_seconds,
        "cancellation_timeout_seconds": cancellation_timeout_seconds,
    }
    public = OkxWsConnection(
        name="public",
        url=public_url,
        subscriptions=public_subscriptions(inst_id),
        **common,
    )
    business = OkxWsConnection(
        name="business",
        url=business_url,
        subscriptions=business_subscriptions(inst_id),
        **common,
    )

    loop = asyncio.get_running_loop()
    installed_signals: list[signal.Signals] = []
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
            installed_signals.append(sig)
        except (NotImplementedError, RuntimeError, ValueError):
            # Windows、非main thread、fixture event loopではsignal handlerを使えない。
            pass

    timer: asyncio.Task[None] | None = None
    if duration is not None:
        async def stop_after_duration() -> None:
            await asyncio.sleep(duration)
            stop_event.set()

        timer = asyncio.create_task(stop_after_duration(), name="collector-duration")

    tasks = [
        asyncio.create_task(public.run(stop_event), name="okx-public"),
        asyncio.create_task(business.run(stop_event), name="okx-business"),
    ]
    if collect_instrument_metadata:
        tasks.append(
            asyncio.create_task(
                _refresh_instrument_metadata_daily(
                    stop_event,
                    raw_dir,
                    inst_id,
                    active_metadata_fetcher,
                ),
                name="okx-instrument-metadata-daily",
            )
        )
    if collect_clock_quality:
        tasks.append(
            asyncio.create_task(
                sample_clock_quality(
                    stop_event,
                    raw_dir,
                    interval_seconds=clock_sample_seconds,
                    reader=clock_reader or read_adjtimex,
                    writer=clock_writer or write_clock_quality_sample,
                ),
                name="host-clock-quality",
            )
        )
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
        for task in done:
            exc = task.exception()
            if exc is not None:
                raise exc
        # duration/signalで一方が正常終了した場合、他方にも停止を伝えて待つ。
        stop_event.set()
        await asyncio.gather(*tasks)
    finally:
        stop_event.set()
        if timer is not None:
            timer.cancel()
            await asyncio.gather(timer, return_exceptions=True)
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for sig in installed_signals:
            with contextlib.suppress(Exception):
                loop.remove_signal_handler(sig)

    return {"public": public.stats, "business": business.stats}


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="OKX public WebSocketから約定・BBO・400段板をraw保存する"
    )
    parser.add_argument("--inst-id", default=config.INST_ID)
    parser.add_argument("--raw-dir", type=Path, default=config.RAW_DIR)
    parser.add_argument(
        "--duration",
        type=float,
        default=None,
        help="実行秒数。省略時はSIGINT/SIGTERMまで継続",
    )
    parser.add_argument("--public-url", default=PUBLIC_WS_URL)
    parser.add_argument("--business-url", default=BUSINESS_WS_URL)
    parser.add_argument("--heartbeat-seconds", type=float, default=20.0)
    parser.add_argument("--pong-timeout-seconds", type=float, default=10.0)
    parser.add_argument("--subscribe-timeout-seconds", type=float, default=10.0)
    parser.add_argument("--reconnect-min-seconds", type=float, default=1.0)
    parser.add_argument("--reconnect-max-seconds", type=float, default=60.0)
    parser.add_argument("--connect-timeout-seconds", type=float, default=20.0)
    parser.add_argument("--cancellation-timeout-seconds", type=float, default=1.0)
    parser.add_argument(
        "--session-rotation-seconds",
        type=float,
        default=3600.0,
        help="rawを確定するためにWS sessionを計画再接続する間隔",
    )
    parser.add_argument(
        "--skip-instrument-metadata",
        action="store_true",
        help="疎通fixture等で起動時REST metadata取得を明示的に省略",
    )
    parser.add_argument(
        "--clock-sample-seconds",
        type=float,
        default=60.0,
        help="Linux adjtimex clock qualityの保存間隔（起動時にも即時保存）",
    )
    parser.add_argument(
        "--skip-clock-quality",
        action="store_true",
        help="非Linux fixture等でclock quality取得を明示的に省略（T0評価不可）",
    )
    parser.add_argument("--log-level", default="INFO")
    return parser


def main() -> None:
    args = _build_parser().parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        stats = asyncio.run(
            collect_microstructure(
                raw_dir=args.raw_dir,
                inst_id=args.inst_id,
                duration=args.duration,
                public_url=args.public_url,
                business_url=args.business_url,
                heartbeat_seconds=args.heartbeat_seconds,
                pong_timeout_seconds=args.pong_timeout_seconds,
                subscribe_timeout_seconds=args.subscribe_timeout_seconds,
                reconnect_min_seconds=args.reconnect_min_seconds,
                reconnect_max_seconds=args.reconnect_max_seconds,
                session_rotation_seconds=args.session_rotation_seconds,
                connect_timeout_seconds=args.connect_timeout_seconds,
                cancellation_timeout_seconds=args.cancellation_timeout_seconds,
                collect_instrument_metadata=not args.skip_instrument_metadata,
                collect_clock_quality=not args.skip_clock_quality,
                clock_sample_seconds=args.clock_sample_seconds,
            )
        )
    except KeyboardInterrupt:
        return
    print(json.dumps({name: _stats_dict(value) for name, value in stats.items()}, ensure_ascii=False))


if __name__ == "__main__":
    main()

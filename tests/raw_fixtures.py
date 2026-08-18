"""closed raw session の共有 fixture(gate / 日次 ingest 用)。

実 collector と同じ :class:`~mce.stream_store.GzipJsonlStreamWriter` を使うので、
gzip footer・frame 連番・subscribe ACK・lifecycle が本物と同じ形になる。
"""

from __future__ import annotations

import gzip
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from mce.stream_store import GzipJsonlStreamWriter

INST_ID = "BTC-USDT-SWAP"
#: 2026-08-16T02:00:00Z 付近。すべての fixture がこの近傍の受信時刻を持つ。
BASE_NS = int(datetime(2026, 8, 16, 2, 0, 0, tzinfo=timezone.utc).timestamp()) * 1_000_000_000


def public_subscriptions() -> list[dict]:
    return [
        {"channel": "trades", "instId": INST_ID},
        {"channel": "bbo-tbt", "instId": INST_ID},
        {"channel": "books", "instId": INST_ID},
        {"channel": "instruments", "instType": "SWAP"},
    ]


def business_subscriptions() -> list[dict]:
    return [{"channel": "trades-all", "instId": INST_ID}]


def message(channel: str, data: list[dict], **extra) -> str:
    payload = {"arg": {"channel": channel, "instId": INST_ID}, "data": data}
    payload.update(extra)
    return json.dumps(payload, separators=(",", ":"))


def public_frames(ts_ms: int, *, seq_start: int = 10) -> list[str]:
    return [
        message(
            "trades",
            [
                {
                    "instId": INST_ID,
                    "ts": str(ts_ms),
                    "px": "60000.5",
                    "sz": "1",
                    "tradeId": "1",
                    "side": "buy",
                    "count": "1",
                }
            ],
        ),
        message(
            "bbo-tbt",
            [
                {
                    "ts": str(ts_ms + 1),
                    "asks": [["60001", "2", "0", "1"]],
                    "bids": [["60000", "3", "0", "1"]],
                }
            ],
        ),
        message(
            "books",
            [
                {
                    "ts": str(ts_ms + 2),
                    "asks": [["60001", "2", "0", "1"]],
                    "bids": [["60000", "3", "0", "1"]],
                    "prevSeqId": -1,
                    "seqId": seq_start,
                }
            ],
            action="snapshot",
        ),
        message(
            "books",
            [
                {
                    "ts": str(ts_ms + 3),
                    "asks": [["60001", "1", "0", "1"]],
                    "bids": [],
                    "prevSeqId": seq_start,
                    "seqId": seq_start + 1,
                }
            ],
            action="update",
        ),
    ]


def business_frames(ts_ms: int) -> list[str]:
    return [
        message(
            "trades-all",
            [
                {
                    "instId": INST_ID,
                    "ts": str(ts_ms),
                    "px": "60000.5",
                    "sz": "0.5",
                    "tradeId": "a",
                    "side": "buy",
                }
            ],
        )
    ]


def write_session(
    raw_dir: Path,
    stream: str,
    frames: list[str],
    *,
    session_id: str,
    first_received_ns: int = BASE_NS,
    step_ns: int = 1_000_000,
    span_ns: int | None = None,
    broken_books: bool = False,
) -> Path:
    """1 closed session を書いて path を返す。

    wall/monotonic clock を注入するので、受信窓は
    ``[first_received_ns, first_received_ns + step_ns * (records - 1)]`` に決まる。
    ``broken_books`` は snapshot 無しの update を混ぜ、books sequence gap を作る。
    """

    subscriptions = (
        public_subscriptions() if stream == "public" else business_subscriptions()
    )
    started_at = datetime.fromtimestamp(first_received_ns / 1_000_000_000, tz=timezone.utc)
    # local event + subscribe + ACK + frame + stopped の総数。span_ns 指定時は
    # 受信窓がちょうどその長さになるよう刻み幅を決める。
    record_count = 4 + len(subscriptions) + len(frames) + (1 if broken_books else 0) + 1
    if span_ns is not None:
        step_ns = span_ns // (record_count - 1)
    ticks = iter(range(10_000))

    def _wall_ns() -> int:
        return first_received_ns + step_ns * next(ticks)

    monotonic_ticks = iter(range(10_000))

    def _monotonic_ns() -> int:
        return step_ns * next(monotonic_ticks)

    writer = GzipJsonlStreamWriter(
        raw_dir,
        stream,
        session_id=session_id,
        started_at=started_at,
        time_ns=_wall_ns,
        monotonic_ns=_monotonic_ns,
    )
    writer.append_event("session_opening", url=f"wss://fixture/{stream}", subscriptions=subscriptions)
    writer.append_event("connected", url=f"wss://fixture/{stream}")
    writer.append(
        json.dumps({"id": session_id, "op": "subscribe", "args": subscriptions}, separators=(",", ":")),
        direction="out",
        kind="subscribe",
    )
    for subscription in subscriptions:
        writer.append(
            json.dumps(
                {"event": "subscribe", "arg": subscription, "connId": session_id},
                separators=(",", ":"),
            ),
            direction="in",
        )
    writer.append_event("subscriptions_ready", subscriptions=subscriptions)
    payloads = list(frames)
    if broken_books:
        payloads.append(
            message(
                "books",
                [
                    {
                        "ts": "1786800100000",
                        "asks": [],
                        "bids": [],
                        "prevSeqId": 999,
                        "seqId": 1000,
                    }
                ],
                action="update",
            )
        )
    for payload in payloads:
        writer.append(payload, direction="in")
    writer.append_event("stopped")
    path = writer.path
    writer.close()
    # closed session の mtime は close 時刻。gate の settle 判定を決定的にする。
    last_ns = first_received_ns + step_ns * (record_count - 1)
    os.utime(path, ns=(last_ns, last_ns))
    return path


def write_clock_sample(raw_dir: Path, received_at_ns: int) -> Path:
    """collector が書く clock quality raw と同じ命名の 1 sample。"""

    moment = datetime.fromtimestamp(received_at_ns / 1_000_000_000, tz=timezone.utc)
    directory = (
        Path(raw_dir)
        / "host"
        / "clock_quality"
        / moment.strftime("%Y")
        / moment.strftime("%m")
        / moment.strftime("%d")
    )
    directory.mkdir(parents=True, exist_ok=True)
    stamp = moment.strftime("%Y%m%dT%H%M%SZ")
    path = directory / f"{stamp}_{received_at_ns}_1_fixture.jsonl.gz"
    record = {
        "schema_version": 1,
        "source": "linux_kernel_adjtimex",
        "received_at_ns": received_at_ns,
        "monotonic_ns": 1,
        "state": 0,
        "state_name": "TIME_OK",
        "status": 0,
        "synchronized": True,
    }
    with gzip.open(path, "wt", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(record, separators=(",", ":")))
        handle.write("\n")
    os.utime(path, ns=(received_at_ns, received_at_ns))
    return path


def valid_pair(raw_dir: Path, *, first_received_ns: int = BASE_NS) -> tuple[Path, Path]:
    """public / business の valid session と、その窓を覆う clock sample。"""

    ts_ms = first_received_ns // 1_000_000
    public = write_session(
        raw_dir,
        "public",
        public_frames(ts_ms),
        session_id="publicok",
        first_received_ns=first_received_ns,
    )
    business = write_session(
        raw_dir,
        "business",
        business_frames(ts_ms),
        session_id="businessok",
        first_received_ns=first_received_ns,
    )
    write_clock_sample(raw_dir, first_received_ns)
    return public, business

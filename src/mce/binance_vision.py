"""Binance Vision(一括ダンプ)の取得 — Phase 7 Tier 0 情報集合。

    python -m mce.binance_vision --start 2020-01 --end 2025-12

対象は USDT-M perpetual(既定 BTCUSDT)の3系列:

| dataset | 粒度 | 情報集合 |
|---|---|---|
| `klines_5m` | 月次dump | 集約 aggressive flow(taker buy 数量・約定件数) |
| `metrics_5m` | 日次dump | derivatives state(OI・long/short ratio・taker L/S vol ratio) |
| `premium_index_5m` | 月次dump | perp/index premium(basis) |

方針(既存の raw 層と同じ):

- **immutable**: 一度落とした zip は上書きしない。公開 `.CHECKSUM`(SHA-256)で検証し、
  一致しないものは残さない。同じ日を再実行しても再取得しない(冪等)。
- **append-only ledger**: URL・published sha256・bytes・HTTP status を JSONL に追記する。
  404 は失敗ではなく `absent`(その日付が公開されていない)として記録する。
- **再配布しない**: ローカル個人研究の範囲(docs/data_sources.md)。

取得先が別 venue(Binance)である点は情報存在検定の前提そのものなので、
normalize 層まで `source="binance"` を必ず持ち回る。
"""

import argparse
import hashlib
import json
import time
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

import httpx

from mce import config

BASE_URL = "https://data.binance.vision"
MARKET_PATH = "data/futures/um"
DEFAULT_SYMBOL = "BTCUSDT"
MARKET_TYPE = "perp_linear"
SOURCE = "binance"

MIN_INTERVAL_SEC = 0.05
MAX_RETRIES = 5
RETRY_STATUS = {429, 500, 502, 503, 504}


class BinanceVisionError(RuntimeError):
    pass


@dataclass(frozen=True)
class DatasetSpec:
    name: str
    cadence: str  # "monthly" / "daily"
    path_template: str  # {sym} / {period} で展開

    def relative_path(self, symbol: str, period: str) -> str:
        return self.path_template.format(sym=symbol, period=period)


DATASETS: dict[str, DatasetSpec] = {
    "klines_5m": DatasetSpec(
        "klines_5m", "monthly", f"{MARKET_PATH}/monthly/klines/{{sym}}/5m/{{sym}}-5m-{{period}}.zip"
    ),
    "premium_index_5m": DatasetSpec(
        "premium_index_5m",
        "monthly",
        f"{MARKET_PATH}/monthly/premiumIndexKlines/{{sym}}/5m/{{sym}}-5m-{{period}}.zip",
    ),
    # metrics は月次dumpが公開されていない(2026-08-16 実測)ため日次のみ
    "metrics_5m": DatasetSpec(
        "metrics_5m", "daily", f"{MARKET_PATH}/daily/metrics/{{sym}}/{{sym}}-metrics-{{period}}.zip"
    ),
}


def raw_dir(dataset: str, symbol: str = DEFAULT_SYMBOL) -> Path:
    return config.RAW_DIR / SOURCE / "vision" / dataset / symbol


def ledger_path(dataset: str, symbol: str = DEFAULT_SYMBOL) -> Path:
    return raw_dir(dataset, symbol) / "download_ledger.jsonl"


def months(start: str, end: str) -> list[str]:
    """"YYYY-MM" 区間(両端含む)を列挙する。"""
    sy, sm = (int(x) for x in start.split("-"))
    ey, em = (int(x) for x in end.split("-"))
    out = []
    y, m = sy, sm
    while (y, m) <= (ey, em):
        out.append(f"{y:04d}-{m:02d}")
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def days(start: str, end: str) -> list[str]:
    """"YYYY-MM" 区間に含まれる全 UTC 日を "YYYY-MM-DD" で列挙する。"""
    sy, sm = (int(x) for x in start.split("-"))
    ey, em = (int(x) for x in end.split("-"))
    cur = date(sy, sm, 1)
    last_year, last_month = (ey + 1, 1) if em == 12 else (ey, em + 1)
    stop = date(last_year, last_month, 1)
    out = []
    while cur < stop:
        out.append(cur.isoformat())
        cur += timedelta(days=1)
    return out


def periods_for(spec: DatasetSpec, start: str, end: str) -> list[str]:
    return months(start, end) if spec.cadence == "monthly" else days(start, end)


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def parse_checksum(text: str) -> str:
    """公開 `.CHECKSUM`("<sha256>  <filename>")から hash を取り出す。"""
    parts = text.split()
    if not parts or len(parts[0]) != 64:
        raise BinanceVisionError(f"CHECKSUM の形式が不正: {text!r}")
    return parts[0].lower()


class VisionClient:
    """必要最小限の GET(レート制限・リトライつき)。"""

    def __init__(self, base_url: str = BASE_URL, timeout: float = 60.0):
        self._http = httpx.Client(base_url=base_url, timeout=timeout, follow_redirects=True)
        self._last_request_at = 0.0

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "VisionClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def get(self, path: str) -> tuple[int, bytes]:
        """(status, body)。404 は正常な戻り値として返す(未公開の日付)。"""
        for attempt in range(MAX_RETRIES):
            wait = MIN_INTERVAL_SEC - (time.monotonic() - self._last_request_at)
            if wait > 0:
                time.sleep(wait)
            self._last_request_at = time.monotonic()
            try:
                resp = self._http.get("/" + path.lstrip("/"))
            except httpx.TransportError:
                time.sleep(2**attempt)
                continue
            if resp.status_code in RETRY_STATUS:
                time.sleep(2**attempt)
                continue
            return resp.status_code, resp.content
        raise BinanceVisionError(f"max retries exceeded: {path}")


def _append_ledger(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


def download_period(
    client: VisionClient,
    spec: DatasetSpec,
    period: str,
    symbol: str = DEFAULT_SYMBOL,
    out_dir: Path | None = None,
) -> dict:
    """1 period を取得して検証・保存する。戻り値は ledger record。

    status: `saved` / `cached`(既存・検証済み)/ `absent`(404)/ `checksum_mismatch`
    """
    out_dir = out_dir or raw_dir(spec.name, symbol)
    rel = spec.relative_path(symbol, period)
    filename = rel.rsplit("/", 1)[-1]
    target = out_dir / filename
    record = {"dataset": spec.name, "symbol": symbol, "period": period, "url": f"{BASE_URL}/{rel}"}

    checksum_status, checksum_body = client.get(rel + ".CHECKSUM")
    published = parse_checksum(checksum_body.decode()) if checksum_status == 200 else None

    if target.exists():
        local = sha256_bytes(target.read_bytes())
        if published is not None and local != published:
            raise BinanceVisionError(
                f"既存 raw が公開 checksum と一致しない(手で消してから再取得する): {target}"
            )
        record |= {"status": "cached", "sha256": local, "bytes": target.stat().st_size}
        return record

    status, body = client.get(rel)
    if status == 404:
        record |= {"status": "absent", "http_status": 404}
        return record
    if status != 200:
        raise BinanceVisionError(f"HTTP {status}: {rel}")

    local = sha256_bytes(body)
    if published is not None and local != published:
        record |= {"status": "checksum_mismatch", "sha256": local, "published_sha256": published}
        return record

    out_dir.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".partial")
    tmp.write_bytes(body)
    tmp.replace(target)
    if published is not None:
        (out_dir / (filename + ".CHECKSUM")).write_bytes(checksum_body)
    record |= {
        "status": "saved",
        "sha256": local,
        "published_sha256": published,
        "checksum_verified": published is not None,
        "bytes": len(body),
    }
    return record


def download_dataset(
    dataset: str,
    start: str,
    end: str,
    symbol: str = DEFAULT_SYMBOL,
    client: VisionClient | None = None,
    out_dir: Path | None = None,
    ledger: Path | None = None,
    verbose: bool = True,
) -> dict:
    spec = DATASETS[dataset]
    owns_client = client is None
    client = client or VisionClient()
    ledger = ledger or ledger_path(dataset, symbol)
    counts: dict[str, int] = {}
    absent: list[str] = []
    mismatched: list[str] = []
    try:
        for i, period in enumerate(periods_for(spec, start, end), 1):
            record = download_period(client, spec, period, symbol, out_dir)
            counts[record["status"]] = counts.get(record["status"], 0) + 1
            if record["status"] == "absent":
                absent.append(period)
            elif record["status"] == "checksum_mismatch":
                mismatched.append(period)
            _append_ledger(ledger, record)
            if verbose and i % 100 == 0:
                print(f"  {dataset}: {i} periods … {counts}")
    finally:
        if owns_client:
            client.close()
    if mismatched:
        raise BinanceVisionError(f"{dataset}: checksum 不一致 {len(mismatched)} 件 {mismatched[:5]}")
    return {"dataset": dataset, "symbol": symbol, "counts": counts, "absent_periods": absent}


def source_digest(dataset: str, symbol: str = DEFAULT_SYMBOL, ledger: Path | None = None) -> dict:
    """ledger から **環境非依存の** 出所指紋を作る。

    parquet の sha256 は polars の版で変わりうるが、公開 zip の SHA-256 は変わらない。
    `digest` は「`<period> <sha256>` を period 昇順に並べた本文」の SHA-256。
    同じ期間を取得した誰の環境でも同じ値になる。
    """
    ledger = ledger or ledger_path(dataset, symbol)
    if not ledger.exists():
        return {"present": False, "path": ledger.as_posix()}
    latest: dict[str, dict] = {}
    for line in ledger.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        latest[record["period"]] = record  # 後の記録で上書き(再実行しても最新が正)
    present = {p: r for p, r in latest.items() if r.get("sha256")}
    body = "\n".join(f"{p} {present[p]['sha256']}" for p in sorted(present))
    return {
        "present": True,
        "periods_with_file": len(present),
        "periods_absent": sum(1 for r in latest.values() if r.get("status") == "absent"),
        "checksum_verified": sum(1 for r in present.values() if r.get("checksum_verified")),
        "digest": hashlib.sha256(body.encode()).hexdigest(),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Binance Vision Tier 0 dump downloader")
    parser.add_argument("--start", default="2020-01", help="開始月 YYYY-MM")
    parser.add_argument("--end", default="2025-12", help="終了月 YYYY-MM(含む)")
    parser.add_argument("--symbol", default=DEFAULT_SYMBOL)
    parser.add_argument(
        "--datasets", nargs="*", default=list(DATASETS), choices=list(DATASETS)
    )
    args = parser.parse_args()

    with VisionClient() as client:
        for dataset in args.datasets:
            print(f"{dataset}: {args.start} .. {args.end}")
            result = download_dataset(dataset, args.start, args.end, args.symbol, client=client)
            print(f"  -> {result['counts']}  absent={len(result['absent_periods'])}")


if __name__ == "__main__":
    main()

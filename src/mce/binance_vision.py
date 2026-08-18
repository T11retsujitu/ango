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
- **差分同期**: `--through-latest-closed` は ledger の watermark(最後に `saved` /
  `cached` を確認できた period)の次から、公開遅延を差し引いた「最後の閉じた
  period」までだけを取得する。未確定の当日・当月は取りに行かない。

    python -m mce.binance_vision --through-latest-closed
    python -m mce.binance_vision --report-only

  運用手順は docs/local_collection_ops.md を参照。

取得先が別 venue(Binance)である点は情報存在検定の前提そのものなので、
normalize 層まで `source="binance"` を必ず持ち回る。
"""

import argparse
import hashlib
import json
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Sequence

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

# Vision は当日・当月の dump を即座には公開しない。未確定 period を「未公開」として
# ledger に書くと、後から本当に欠けている period と区別できなくなる。既定の余裕日数。
PUBLICATION_LAG_DAYS = 2

# watermark が無い dataset を差分同期するときの既定開始月。
DEFAULT_START_MONTH = "2020-01"


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


def _next_period(spec: DatasetSpec, period: str) -> str:
    """period 表記の次の 1 単位。"""
    if spec.cadence == "monthly":
        year, month = (int(x) for x in period.split("-"))
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
        return f"{year:04d}-{month:02d}"
    return (date.fromisoformat(period) + timedelta(days=1)).isoformat()


def latest_closed_period(
    spec: DatasetSpec,
    today: date | None = None,
    publication_lag_days: int = PUBLICATION_LAG_DAYS,
) -> str:
    """公開遅延を差し引いた「最後の閉じた period」。

    当日・当月の dump はまだ確定していないので取りに行かない。未確定 period を
    404 のまま `absent` として ledger に書くと、後から本当に欠けている period と
    区別できなくなるためである。
    """
    if publication_lag_days < 0:
        raise ValueError("publication_lag_days must be >= 0")
    today = today or datetime.now(timezone.utc).date()
    cutoff = today - timedelta(days=publication_lag_days)
    if spec.cadence == "daily":
        return cutoff.isoformat()
    # 月次 dump は月が閉じてから公開される。cutoff の属する月はまだ閉じていない。
    last_closed_month = cutoff.replace(day=1) - timedelta(days=1)
    return f"{last_closed_month.year:04d}-{last_closed_month.month:02d}"


def ledger_watermark(
    dataset: str, symbol: str = DEFAULT_SYMBOL, ledger: Path | None = None
) -> str | None:
    """ledger 上で最後に `saved` / `cached` を確認できた period。

    `absent` や `checksum_mismatch` は watermark を進めない。公開が遅れているだけの
    period を「取得済み」と誤認して飛ばさないためである。
    """
    ledger = ledger or ledger_path(dataset, symbol)
    if not Path(ledger).exists():
        return None
    verified: str | None = None
    for line in Path(ledger).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        if record.get("status") not in {"saved", "cached"}:
            continue
        period = record.get("period")
        # "YYYY-MM" も "YYYY-MM-DD" も辞書順が時系列順になる。
        if isinstance(period, str) and (verified is None or period > verified):
            verified = period
    return verified


def incremental_periods(
    spec: DatasetSpec,
    *,
    watermark: str | None,
    through: str,
    default_start: str = DEFAULT_START_MONTH,
) -> list[str]:
    """watermark の次から `through` までの period 列。空なら同期不要。"""
    if watermark is None:
        start = default_start if spec.cadence == "monthly" else f"{default_start}-01"
    else:
        start = _next_period(spec, watermark)
    if start > through:
        return []
    if spec.cadence == "monthly":
        return months(start, through)
    out: list[str] = []
    cursor = date.fromisoformat(start)
    stop = date.fromisoformat(through)
    while cursor <= stop:
        out.append(cursor.isoformat())
        cursor += timedelta(days=1)
    return out


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
    start: str | None = None,
    end: str | None = None,
    symbol: str = DEFAULT_SYMBOL,
    client: VisionClient | None = None,
    out_dir: Path | None = None,
    ledger: Path | None = None,
    verbose: bool = True,
    periods: Sequence[str] | None = None,
) -> dict:
    """期間または明示的な `periods` 列を取得する。

    retry budget は :class:`VisionClient` 側で尽きる。尽きた period は
    `retryable_error` として ledger に**明示して**から中断する。黙って飛ばすと、
    後で「取りに行かなかった period」と「本当に無い period」を区別できなくなる。
    """
    spec = DATASETS[dataset]
    owns_client = client is None
    client = client or VisionClient()
    ledger = ledger or ledger_path(dataset, symbol)
    if periods is None:
        if start is None or end is None:
            raise BinanceVisionError("periods か (start, end) のどちらかが必要")
        periods = periods_for(spec, start, end)
    counts: dict[str, int] = {}
    absent: list[str] = []
    mismatched: list[str] = []
    try:
        for i, period in enumerate(periods, 1):
            try:
                record = download_period(client, spec, period, symbol, out_dir)
            except BinanceVisionError as exc:
                _append_ledger(
                    ledger,
                    {
                        "dataset": spec.name,
                        "symbol": symbol,
                        "period": period,
                        "status": "retryable_error",
                        "error": str(exc),
                    },
                )
                raise
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
    return {
        "dataset": dataset,
        "symbol": symbol,
        "counts": counts,
        "absent_periods": absent,
        "requested_periods": list(periods),
    }


def sync_incremental(
    dataset: str,
    symbol: str = DEFAULT_SYMBOL,
    client: VisionClient | None = None,
    out_dir: Path | None = None,
    ledger: Path | None = None,
    today: date | None = None,
    publication_lag_days: int = PUBLICATION_LAG_DAYS,
    default_start: str = DEFAULT_START_MONTH,
    verbose: bool = True,
) -> dict:
    """ledger の watermark から「最後の閉じた period」までだけを差分取得する。

    毎分・毎時の監視ではなく、**閉じたファイルの差分同期**なので日次/月次の軽い
    定期実行で足りる。既存 zip は再ダウンロードしない(immutable・冪等)。
    """
    spec = DATASETS[dataset]
    ledger = ledger or ledger_path(dataset, symbol)
    watermark = ledger_watermark(dataset, symbol, ledger)
    through = latest_closed_period(spec, today, publication_lag_days)
    pending = incremental_periods(
        spec, watermark=watermark, through=through, default_start=default_start
    )
    result = {
        "dataset": dataset,
        "symbol": symbol,
        "cadence": spec.cadence,
        "watermark": watermark,
        "through_latest_closed": through,
        "pending_periods": len(pending),
        "counts": {},
        "absent_periods": [],
        "requested_periods": [],
    }
    if not pending:
        return result
    downloaded = download_dataset(
        dataset,
        symbol=symbol,
        client=client,
        out_dir=out_dir,
        ledger=ledger,
        verbose=verbose,
        periods=pending,
    )
    result |= {
        "counts": downloaded["counts"],
        "absent_periods": downloaded["absent_periods"],
        "requested_periods": downloaded["requested_periods"],
    }
    result["watermark_after"] = ledger_watermark(dataset, symbol, ledger)
    return result


def availability_report(
    dataset: str, symbol: str = DEFAULT_SYMBOL, ledger: Path | None = None
) -> dict:
    """period ごとの最終 status を明示する(`saved`/`cached`/`absent`/`retryable_error`)。

    「取得できた」「公開されていない」「取りに行って失敗した」を混ぜない。
    """
    ledger = ledger or ledger_path(dataset, symbol)
    latest: dict[str, dict] = {}
    if Path(ledger).exists():
        for line in Path(ledger).read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            record = json.loads(line)
            period = record.get("period")
            if isinstance(period, str):
                latest[period] = record
    counts: dict[str, int] = {}
    for record in latest.values():
        status = str(record.get("status", "unknown"))
        counts[status] = counts.get(status, 0) + 1
    return {
        "dataset": dataset,
        "symbol": symbol,
        "ledger": Path(ledger).as_posix(),
        "counts": counts,
        "periods": {period: latest[period].get("status") for period in sorted(latest)},
        "absent_periods": sorted(p for p, r in latest.items() if r.get("status") == "absent"),
        "retryable_error_periods": sorted(
            p for p, r in latest.items() if r.get("status") == "retryable_error"
        ),
        "watermark": ledger_watermark(dataset, symbol, ledger),
    }


def source_digest(
    dataset: str,
    symbol: str = DEFAULT_SYMBOL,
    ledger: Path | None = None,
    through: str | None = None,
    since: str | None = None,
) -> dict:
    """ledger から **環境非依存の** 出所指紋を作る。

    parquet の sha256 は polars の版で変わりうるが、公開 zip の SHA-256 は変わらない。
    `digest` は「`<period> <sha256>` を period 昇順に並べた本文」の SHA-256。
    同じ期間を取得した誰の環境でも同じ値になる。

    差分同期で新しい period を足しても凍結済みの digest を動かさないため、
    `since` / `through` で period 窓を明示できる(既定は ledger 全体)。凍結記録は
    凍結時の窓を書いておき、同じ窓で再計算して照合する。
    """
    ledger = ledger or ledger_path(dataset, symbol)
    ledger = Path(ledger)
    if not ledger.exists():
        return {"present": False, "path": ledger.as_posix()}
    latest: dict[str, dict] = {}
    for line in ledger.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        period = record["period"]
        if since is not None and period < since:
            continue
        if through is not None and period > through:
            continue
        latest[period] = record  # 後の記録で上書き(再実行しても最新が正)
    present = {p: r for p, r in latest.items() if r.get("sha256")}
    body = "\n".join(f"{p} {present[p]['sha256']}" for p in sorted(present))
    return {
        "present": True,
        "since": since,
        "through": through,
        "periods_with_file": len(present),
        "periods_absent": sum(1 for r in latest.values() if r.get("status") == "absent"),
        "checksum_verified": sum(1 for r in present.values() if r.get("checksum_verified")),
        "digest": hashlib.sha256(body.encode()).hexdigest(),
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Binance Vision Tier 0 dump downloader")
    parser.add_argument("--start", default="2020-01", help="開始月 YYYY-MM")
    parser.add_argument("--end", default="2025-12", help="終了月 YYYY-MM(含む)")
    parser.add_argument("--symbol", default=DEFAULT_SYMBOL)
    parser.add_argument(
        "--datasets", nargs="*", default=list(DATASETS), choices=list(DATASETS)
    )
    parser.add_argument(
        "--through-latest-closed",
        action="store_true",
        help="ledger watermark から最後の閉じた period までを差分取得する(--start/--end は無視)",
    )
    parser.add_argument(
        "--publication-lag-days",
        type=int,
        default=PUBLICATION_LAG_DAYS,
        help="未確定 period を避けるための公開遅延の余裕日数",
    )
    parser.add_argument(
        "--report-only",
        action="store_true",
        help="取得せず、dataset ごとの availability report だけを出す",
    )
    parser.add_argument("--report", type=Path, help="availability report の JSON 出力先")
    args = parser.parse_args(argv)

    reports: dict[str, dict] = {}
    if args.report_only:
        for dataset in args.datasets:
            reports[dataset] = availability_report(dataset, args.symbol)
    else:
        with VisionClient() as client:
            for dataset in args.datasets:
                if args.through_latest_closed:
                    result = sync_incremental(
                        dataset,
                        args.symbol,
                        client=client,
                        publication_lag_days=args.publication_lag_days,
                    )
                    print(
                        f"{dataset}: watermark={result['watermark']} "
                        f"-> {result['through_latest_closed']} "
                        f"pending={result['pending_periods']}"
                    )
                else:
                    print(f"{dataset}: {args.start} .. {args.end}")
                    result = download_dataset(
                        dataset, args.start, args.end, args.symbol, client=client
                    )
                print(f"  -> {result['counts']}  absent={len(result['absent_periods'])}")
                reports[dataset] = availability_report(dataset, args.symbol)

    if args.report is not None:
        from mce.artifacts import atomic_write_json

        atomic_write_json(args.report, reports)
    elif args.report_only:
        print(json.dumps(reports, ensure_ascii=False, sort_keys=True, indent=2))
    retryable = sum(len(item["retryable_error_periods"]) for item in reports.values())
    return 1 if retryable else 0


if __name__ == "__main__":
    raise SystemExit(main())

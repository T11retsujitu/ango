"""データ取得 CLI。

    python -m mce.ingest ohlcv --days 30
    python -m mce.ingest funding
    python -m mce.ingest oi

いずれも冪等: 既存 normalized Parquet の最終 timestamp 以降(より古い側は
指定期間の開始まで)だけを取得し、重複はマージ時に排除される。
raw レイヤーには API レスポンスをそのまま JSONL (gzip) で残す。
"""

import argparse
import time
from datetime import datetime, timezone

import polars as pl

from mce import config, normalize, store
from mce.okx import OkxClient


def _now_ms() -> int:
    return int(time.time() * 1000)


def _iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()


def _fetch_backward(fetch_page, dataset: str, start_ms: int, resume_ms: int | None) -> list:
    """OKX の「after より古いものを新しい順に返す」ページングで現在→過去へ遡る。

    start_ms(取得したい期間の先頭)か、既存データの最終 ts(resume_ms)に
    到達したら止める。ページごとに raw へ追記保存する。
    """
    floor_ms = start_ms if resume_ms is None else max(start_ms, resume_ms)
    run_id = store.new_run_id()
    collected: list = []
    after: int | None = None
    while True:
        body, params = fetch_page(after)
        store.append_raw(
            config.RAW_DIR,
            dataset,
            run_id,
            {"fetched_at": datetime.now(timezone.utc).isoformat(), "params": params, "body": body},
        )
        rows = body["data"]
        if not rows:
            break
        collected.extend(rows)
        oldest = min(int(r[0] if isinstance(r, list) else r["fundingTime"]) for r in rows)
        if oldest <= floor_ms:
            break
        after = oldest
    return collected


def ingest_ohlcv(client: OkxClient, days: int) -> None:
    path = config.ohlcv_parquet()
    resume = store.max_ts_ms(path)
    start_ms = _now_ms() - days * 86_400_000
    if resume is not None:
        print(f"resume: 既存データの最終 ts = {_iso(resume)}")

    def page(after):
        params = {"instId": config.INST_ID, "bar": config.BAR, "after": after}
        return client.history_candles(config.INST_ID, config.BAR, after_ms=after), params

    rows = _fetch_backward(page, f"okx/ohlcv_{config.BAR}", start_ms, resume)
    df = normalize.normalize_candles(rows, config.INST_ID)
    df = df.filter(pl.col("ts") >= datetime.fromtimestamp(start_ms / 1000, tz=timezone.utc)) if not df.is_empty() else df
    added = store.merge_parquet(path, df, key_cols=["source", "symbol", "ts"])
    print(f"ohlcv: {len(rows)} 行取得, {added} 行追加 -> {path}")


def ingest_funding(client: OkxClient, days: int) -> None:
    path = config.funding_parquet()
    resume = store.max_ts_ms(path)
    start_ms = _now_ms() - days * 86_400_000

    def page(after):
        params = {"instId": config.INST_ID, "after": after}
        return client.funding_rate_history(config.INST_ID, after_ms=after), params

    rows = _fetch_backward(page, "okx/funding_rate", start_ms, resume)
    df = normalize.normalize_funding(rows, config.INST_ID)
    added = store.merge_parquet(path, df, key_cols=["source", "symbol", "ts"])
    print(f"funding: {len(rows)} 行取得, {added} 行追加 -> {path}")


def ingest_oi(client: OkxClient, days: int) -> None:
    path = config.open_interest_parquet()
    resume = store.max_ts_ms(path)
    start_ms = _now_ms() - days * 86_400_000

    # OI 履歴は end(それ以前を返す)でページングする
    run_id_dataset = "okx/open_interest_5m"

    def page(end):
        params = {"instId": config.INST_ID, "period": "5m", "end": end}
        return client.open_interest_history(config.INST_ID, period="5m", end_ms=end), params

    rows = _fetch_backward(page, run_id_dataset, start_ms, resume)
    df = normalize.normalize_open_interest(rows, config.INST_ID)
    added = store.merge_parquet(path, df, key_cols=["source", "symbol", "ts"])
    print(f"open_interest: {len(rows)} 行取得, {added} 行追加 -> {path}")


def main() -> None:
    parser = argparse.ArgumentParser(description="OKX public API からデータを取得する")
    parser.add_argument("dataset", choices=["ohlcv", "funding", "oi", "all"])
    parser.add_argument("--days", type=int, default=30, help="現在から遡る日数 (default: 30)")
    args = parser.parse_args()

    client = OkxClient()
    try:
        if args.dataset in ("ohlcv", "all"):
            ingest_ohlcv(client, args.days)
        if args.dataset in ("funding", "all"):
            ingest_funding(client, args.days)
        if args.dataset in ("oi", "all"):
            ingest_oi(client, args.days)
    finally:
        client.close()


if __name__ == "__main__":
    main()

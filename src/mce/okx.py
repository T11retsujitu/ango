"""OKX v5 public API クライアント(認証不要のエンドポイントのみ)。

タイムスタンプはすべて UNIX ミリ秒 (UTC)。OKX のページングは
「after=ts より古いレコードを新しい順に返す」方式。
"""

import time

import httpx

BASE_URL = "https://www.okx.com"

# public market data のレート制限は概ね 20req/2s (history-candles)。
# 安全側に倒してリクエスト間隔を空ける。
MIN_INTERVAL_SEC = 0.15
RETRY_STATUS = {429, 500, 502, 503, 504}
MAX_RETRIES = 5


class OkxError(RuntimeError):
    pass


class OkxClient:
    def __init__(self, base_url: str = BASE_URL):
        self._http = httpx.Client(base_url=base_url, timeout=20.0)
        self._last_request_at = 0.0

    def close(self) -> None:
        self._http.close()

    def _get(self, path: str, params: dict) -> dict:
        """レート制限を挟みつつ GET し、パース済み JSON body 全体を返す。"""
        for attempt in range(MAX_RETRIES):
            wait = MIN_INTERVAL_SEC - (time.monotonic() - self._last_request_at)
            if wait > 0:
                time.sleep(wait)
            self._last_request_at = time.monotonic()
            try:
                resp = self._http.get(path, params=params)
            except httpx.TransportError:
                time.sleep(2**attempt)
                continue
            if resp.status_code in RETRY_STATUS:
                time.sleep(2**attempt)
                continue
            resp.raise_for_status()
            body = resp.json()
            if body.get("code") != "0":
                raise OkxError(f"OKX API error {body.get('code')}: {body.get('msg')} ({path} {params})")
            return body
        raise OkxError(f"max retries exceeded: {path} {params}")

    def history_candles(self, inst_id: str, bar: str, after_ms: int | None = None, limit: int = 100) -> dict:
        """確定済みローソク足。上場以来の履歴まで遡れる(1リクエスト最大100本)。"""
        params: dict = {"instId": inst_id, "bar": bar, "limit": limit}
        if after_ms is not None:
            params["after"] = after_ms
        return self._get("/api/v5/market/history-candles", params)

    def funding_rate_history(self, inst_id: str, after_ms: int | None = None, limit: int = 100) -> dict:
        """Funding Rate 履歴。実測で直近約3ヶ月分のみ取得可能。"""
        params: dict = {"instId": inst_id, "limit": limit}
        if after_ms is not None:
            params["after"] = after_ms
        return self._get("/api/v5/public/funding-rate-history", params)

    def open_interest_history(self, inst_id: str, period: str = "5m", end_ms: int | None = None, limit: int = 100) -> dict:
        """Open Interest 履歴。実測で遡れるのは直近数週間〜数ヶ月程度。"""
        params: dict = {"instId": inst_id, "period": period, "limit": limit}
        if end_ms is not None:
            params["end"] = end_ms
        return self._get("/api/v5/rubik/stat/contracts/open-interest-history", params)

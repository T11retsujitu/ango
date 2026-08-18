"""H13 — BTCUSDT USD-M の taker commission を**認証付きで読むだけ**の道具。

    GET /fapi/v1/commissionRate?symbol=BTCUSDT   (signed USER_DATA)

**この道具ができることは上の1つだけである。**

- **署名付き GET のみ。** POST / PUT / DELETE の経路がそもそも存在しない。
- **注文を出さない。** 資金を動かさない。レバレッジを変えない。
- 許可された path は allowlist の1本のみ。危険な語を含む path は二重に拒否する。
- **資格情報を保存しない・表示しない。** secret はプロセス内で HMAC にしか使わない。
- API key は argv に載せない(curl の設定を stdin から渡す)。

記録するのは次の5項目だけである(**生の応答本文は保存しない**):

    raw response の SHA-256 / 取得 UTC 時刻 / symbol / maker rate / taker rate

**Binance ドキュメントの 4bps の例を実測値として使ってはならない。**
本モジュールが返すのは、認証された実応答から読んだ値だけである。

    uv run python -m mce.binance_commission --json experiments/phase8/h13_commission_rate_v1.json

資格情報は環境変数から読む(ファイルにも argv にも置かない):

    BINANCE_API_KEY / BINANCE_API_SECRET
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Final
from urllib.parse import urlencode

UTC = timezone.utc

BASE_URL: Final = "https://fapi.binance.com"

#: **これ以外の path は呼べない。**
ALLOWED_PATHS: Final = frozenset({"/fapi/v1/commissionRate"})

#: 二重の防御。path にこれらの語が含まれたら、allowlist を通っていても拒否する。
FORBIDDEN_PATH_FRAGMENTS: Final = (
    "order", "batchorder", "position", "leverage", "margin", "transfer",
    "withdraw", "deposit", "listenkey", "countdown", "adl", "multiassets",
)

#: 注文系のパラメータ名。1つでも来たら拒否する。
FORBIDDEN_PARAMS: Final = frozenset({
    "side", "quantity", "price", "type", "timeinforce", "reduceonly",
    "closeposition", "leverage", "amount", "quoteorderqty", "stopprice",
    "positionside", "newclientorderid", "activationprice", "callbackrate",
})

RECV_WINDOW_MS: Final = 5000

__all__ = [
    "ALLOWED_PATHS",
    "FORBIDDEN_PARAMS",
    "FORBIDDEN_PATH_FRAGMENTS",
    "CommissionRecord",
    "CredentialError",
    "Credentials",
    "sign",
    "signed_get",
    "fetch_commission_rate",
]


class CredentialError(RuntimeError):
    """資格情報が無い/不正。**例外文言に secret を含めない。**"""


class RequestNotPermitted(RuntimeError):
    """許可されていない要求。**送信する前に**止める。"""


@dataclass(frozen=True)
class Credentials:
    """資格情報の入れ物。**repr / str で必ず伏せる。**"""

    api_key: str
    api_secret: str

    def __repr__(self) -> str:  # pragma: no cover - 表示経路の保護
        return "Credentials(api_key='<redacted>', api_secret='<redacted>')"

    __str__ = __repr__

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "Credentials":
        env = os.environ if env is None else env  # type: ignore[assignment]
        key = env.get("BINANCE_API_KEY", "")
        secret = env.get("BINANCE_API_SECRET", "")
        if not key or not secret:
            raise CredentialError(
                "BINANCE_API_KEY / BINANCE_API_SECRET が設定されていない。"
                "**値をここに書かないこと。** 実行環境の環境変数として渡す。"
            )
        return cls(key, secret)


def sign(query: str, api_secret: str) -> str:
    """Binance の署名。query 文字列の HMAC-SHA256 を 16 進で返す。"""
    return hmac.new(api_secret.encode(), query.encode(), hashlib.sha256).hexdigest()


def _check_permitted(method: str, path: str, params: dict[str, object]) -> None:
    """**送信前**の検査。1つでも外れたら送らない。"""
    if method != "GET":
        raise RequestNotPermitted(f"GET 以外は実装していない: {method}")
    if path not in ALLOWED_PATHS:
        raise RequestNotPermitted(f"allowlist に無い path: {path}")
    lowered = path.lower()
    for frag in FORBIDDEN_PATH_FRAGMENTS:
        if frag in lowered:
            raise RequestNotPermitted(f"禁止語を含む path: {frag}")
    for name in params:
        if name.lower() in FORBIDDEN_PARAMS:
            raise RequestNotPermitted(f"注文系のパラメータは渡せない: {name}")


def signed_get(
    path: str,
    params: dict[str, object],
    creds: Credentials,
    *,
    base_url: str = BASE_URL,
    timeout: int = 20,
    runner=None,
) -> tuple[str, str]:
    """署名付き GET を1回だけ行い、`(生の本文, その SHA-256)` を返す。

    API key は **argv に載せない**。curl の設定を stdin から渡す。
    secret はプロセス外へ出ない(HMAC にしか使わない)。
    """
    _check_permitted("GET", path, params)
    payload = dict(params)
    payload["recvWindow"] = RECV_WINDOW_MS
    payload["timestamp"] = int(time.time() * 1000)
    query = urlencode(payload)
    signature = sign(query, creds.api_secret)
    url = f"{base_url}{path}?{query}&signature={signature}"
    # curl の設定を stdin で渡すため、URL も key も argv に現れない。
    config = f'url = "{url}"\nheader = "X-MBX-APIKEY: {creds.api_key}"\nsilent\n'
    run = runner or _run_curl
    body = run(config, timeout)
    return body, hashlib.sha256(body.encode()).hexdigest()


def _run_curl(config: str, timeout: int) -> str:  # pragma: no cover - ネットワーク依存
    proc = subprocess.run(
        ["curl", "--config", "-", "--max-time", str(timeout)],
        input=config, capture_output=True, text=True,
    )
    if not proc.stdout.strip():
        raise RuntimeError("空応答(資格情報は表示しない)")
    return proc.stdout


@dataclass(frozen=True)
class CommissionRecord:
    """H13 の記録。**この5項目以外を持たない。**"""

    symbol: str
    maker_commission_rate: float
    taker_commission_rate: float
    response_sha256: str
    retrieved_at_utc: str

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "maker_commission_rate": self.maker_commission_rate,
            "taker_commission_rate": self.taker_commission_rate,
            "response_sha256": self.response_sha256,
            "retrieved_at_utc": self.retrieved_at_utc,
        }


def parse_commission(body: str, digest: str, *, now: str | None = None) -> CommissionRecord:
    """応答から5項目だけを取り出す。**本文は保持しない。**"""
    data = json.loads(body)
    if "code" in data and "msg" in data:
        raise RuntimeError(f"Binance がエラーを返した: code={data['code']}")
    return CommissionRecord(
        symbol=data["symbol"],
        maker_commission_rate=float(data["makerCommissionRate"]),
        taker_commission_rate=float(data["takerCommissionRate"]),
        response_sha256=digest,
        retrieved_at_utc=now or datetime.now(UTC).isoformat(),
    )


def fetch_commission_rate(
    symbol: str = "BTCUSDT", creds: Credentials | None = None, **kw
) -> CommissionRecord:
    """**唯一の対外動作。** 署名付き GET を1回して5項目を返す。"""
    creds = creds or Credentials.from_env()
    body, digest = signed_get("/fapi/v1/commissionRate", {"symbol": symbol}, creds, **kw)
    return parse_commission(body, digest)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--symbol", default="BTCUSDT")
    ap.add_argument("--json", type=Path, default=None)
    args = ap.parse_args()
    try:
        record = fetch_commission_rate(args.symbol)
    except CredentialError as exc:
        raise SystemExit(f"H13 未解決: {exc}")
    payload = record.to_dict()
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                             encoding="utf-8")
    # **資格情報も生の本文も出さない。**
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

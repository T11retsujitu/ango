"""Phase 8 — Binance USD-M **公開 REST** の funding 決済(markPrice 付き)。

    uv run python -m mce.binance_rest --start 2020-01 --end 2025-12

protocol §4.1 の `FUND` は「Vision `monthly/fundingRate` + 公式 REST(`markPrice` 付き)」
である。Vision dump には **markPrice 列が無い**(実測。
[funding_rate_dump_schema_v1](../../docs/phase8/funding_rate_dump_schema_v1.md) §2.1)。
§8.1 の `cash_flow(s) = q · MarkPrice(s) · f(s)` に要る決済時点の mark は、
この経路からしか一次情報として取れない。

**この道具ができる外部操作は1つだけである**:

    GET /fapi/v1/fundingRate      (公開・**認証不要**)

- **allowlist は上の1本のみ。** それ以外の path は送信前に拒否する。
- **書き込みメソッドが存在しない。** POST / PUT / DELETE の経路をモジュールが持たない。
- **認証しない。** API key も署名も使わない。USER_DATA へ触れる経路が無い。
- **注文を出さない。** 資金を動かさない。レバレッジを変えない。

**Vision の canonical funding rate を REST で黙って置換しない。**
REST 系列は**別ファイル**に保存し(§3)、Vision を canonical として**照合するだけ**である
(§4)。照合は timestamp の完全一致を前提にしない。

**封印**: `ts >= FINAL_OOS_START (2026-01-01)` は**要求もしない・保存もしない**。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import time
from bisect import bisect_left, bisect_right
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Final

import httpx
import polars as pl

from mce import config
from mce.backtest.splits import FINAL_OOS_START
from mce.binance_vision import DEFAULT_SYMBOL, MARKET_TYPE, SOURCE, months

UTC = timezone.utc

REST_HOST: Final = "https://fapi.binance.com"
FUNDING_RATE_PATH: Final = "/fapi/v1/fundingRate"
#: **送信を許す path はこれだけ。** 増やすときは本文書と安全性テストを同時に直す。
ALLOWED_PATHS: Final = frozenset({FUNDING_RATE_PATH})

#: 認証 / 注文 / USER_DATA を示唆する query。**送信前に拒否する。**
FORBIDDEN_PARAMS: Final = frozenset({
    "apikey", "api_key", "x-mbx-apikey", "signature", "timestamp", "recvwindow",
    "side", "quantity", "price", "type", "neworderresptype", "newclientorderid",
    "positionside", "reduceonly", "closeposition", "leverage", "margintype",
    "listenkey", "orderid", "origclientorderid", "activationprice", "callbackrate",
})

#: 公式仕様(2026-08-20 取得): limit は最大 1000・既定 100。startTime / endTime は
#: **両端 inclusive**。件数が limit を超えると `startTime + limit` で切り詰められる。
MAX_LIMIT: Final = 1000
#: rate limit は 500 req / 5min / IP を GET /fapi/v1/fundingInfo と共有する。
#: **過度な並列アクセスをしない。** 逐次 + 最小間隔だけを使う。
MIN_INTERVAL_SEC: Final = 0.35
MAX_PAGES: Final = 1000  # 暴走ループの背骨。6年分でも 7 ページ程度である

#: 封印。**この時刻以降は要求もしない。**
SEAL_CUTOFF_MS: Final = int(FINAL_OOS_START.timestamp() * 1000)


class BinanceRestError(RuntimeError):
    pass


class RequestNotPermitted(BinanceRestError):
    """allowlist 外の path、または認証 / 注文を示唆する param。"""


class PagingAnomaly(BinanceRestError):
    """無進行・順序逆転・重複・同一ページ再取得。**黙って続行しない。**"""


@dataclass(frozen=True)
class PageProvenance:
    """1 ページ分の出所。**生レスポンス本文は保存しない**(SHA-256 だけ残す)。"""

    page_index: int
    requested_start_ms: int
    requested_end_ms: int
    limit: int
    http_status: int
    response_sha256: str
    response_bytes: int
    retrieved_at_utc: str
    row_count: int
    first_funding_time: int | None
    last_funding_time: int | None


Transport = Callable[[str, dict], tuple[int, bytes]]


def _httpx_transport(timeout: float = 30.0) -> Transport:
    client = httpx.Client(base_url=REST_HOST, timeout=timeout, follow_redirects=False)
    last_at = [0.0]

    def send(path: str, params: dict) -> tuple[int, bytes]:
        wait = MIN_INTERVAL_SEC - (time.monotonic() - last_at[0])
        if wait > 0:
            time.sleep(wait)
        last_at[0] = time.monotonic()
        # **GET しか呼ばない。** client は module 内のこの1箇所からしか使わない。
        resp = client.get(path, params=params)
        return resp.status_code, resp.content

    return send


def public_get(path: str, params: dict, transport: Transport | None = None) -> tuple[int, bytes]:
    """公開 GET。**allowlist と禁止 param を送信前に検査する。**"""
    if path not in ALLOWED_PATHS:
        raise RequestNotPermitted(f"許可されていない path: {path!r}")
    offending = sorted(k for k in params if k.lower() in FORBIDDEN_PARAMS)
    if offending:
        raise RequestNotPermitted(f"認証 / 注文を示唆する param: {offending}")
    send = transport or _httpx_transport()
    return send(path, params)


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def fetch_page(
    symbol: str,
    start_ms: int,
    end_ms: int,
    *,
    limit: int = MAX_LIMIT,
    page_index: int = 0,
    transport: Transport | None = None,
) -> tuple[list[dict], PageProvenance]:
    """1 ページ取得する。戻り値は (行, 出所)。"""
    if limit > MAX_LIMIT:
        raise BinanceRestError(f"limit は最大 {MAX_LIMIT}: {limit}")
    params = {"symbol": symbol, "startTime": start_ms, "endTime": end_ms, "limit": limit}
    retrieved_at = datetime.now(UTC).isoformat()
    status, body = public_get(FUNDING_RATE_PATH, params, transport)
    if status != 200:
        raise BinanceRestError(f"HTTP {status}: {body[:200]!r}")
    payload = json.loads(body.decode())
    if not isinstance(payload, list):
        raise BinanceRestError(f"list ではない応答: {str(payload)[:200]}")
    times = [int(row["fundingTime"]) for row in payload]
    provenance = PageProvenance(
        page_index=page_index,
        requested_start_ms=start_ms,
        requested_end_ms=end_ms,
        limit=limit,
        http_status=status,
        response_sha256=_sha256(body),
        response_bytes=len(body),
        retrieved_at_utc=retrieved_at,
        row_count=len(payload),
        first_funding_time=times[0] if times else None,
        last_funding_time=times[-1] if times else None,
    )
    return payload, provenance


def fetch_funding_rates(
    symbol: str = DEFAULT_SYMBOL,
    *,
    start_ms: int,
    end_ms: int,
    limit: int = MAX_LIMIT,
    transport: Transport | None = None,
    cutoff_ms: int = SEAL_CUTOFF_MS,
) -> tuple[list[dict], list[PageProvenance]]:
    """`[start_ms, end_ms]`(**両端 inclusive**)を頁送りで全件取得する。

    頁送りは **最終 `fundingTime` + 1ms** から次を要求する。公式仕様どおり
    `startTime` は inclusive なので、+1ms しないと境界の1件を必ず二重取得する。

    次のいずれかを検出したら **停止して送出する**(黙って続けない):

    - **同一ページの再取得**: 同じ `(startTime, endTime)` を2度要求した
    - **無進行**: 行があるのに最終 `fundingTime` が前ページから進まない
    - **順序逆転**: ページ内で `fundingTime` が昇順でない
    - **重複**: 既に見た `fundingTime` が再び現れた
    - **範囲外**: 要求範囲の外、または封印 cutoff 以降の行が返った
    """
    if end_ms >= cutoff_ms:
        raise BinanceRestError(
            f"封印域を要求している: end_ms={end_ms} >= cutoff={cutoff_ms}"
        )
    if start_ms > end_ms:
        raise BinanceRestError(f"start_ms > end_ms: {start_ms} > {end_ms}")

    rows: list[dict] = []
    pages: list[PageProvenance] = []
    seen_times: set[int] = set()
    seen_requests: set[tuple[int, int]] = set()
    cursor = start_ms
    previous_last: int | None = None

    for page_index in range(MAX_PAGES):
        request_key = (cursor, end_ms)
        if request_key in seen_requests:
            raise PagingAnomaly(f"同一ページを再取得しようとした: {request_key}")
        seen_requests.add(request_key)

        page, provenance = fetch_page(
            symbol, cursor, end_ms, limit=limit, page_index=page_index, transport=transport
        )
        pages.append(provenance)
        if not page:
            break

        times = [int(row["fundingTime"]) for row in page]
        if any(b <= a for a, b in zip(times, times[1:])):
            raise PagingAnomaly(f"fundingTime が昇順でない(page {page_index})")
        duplicated = seen_times.intersection(times)
        if duplicated:
            raise PagingAnomaly(f"重複した fundingTime: {sorted(duplicated)[:5]}")
        outside = [t for t in times if t < cursor or t > end_ms]
        if outside:
            raise PagingAnomaly(f"要求範囲外の fundingTime: {outside[:5]}")
        sealed = [t for t in times if t >= cutoff_ms]
        if sealed:
            raise PagingAnomaly(f"封印域の fundingTime が返った: {sealed[:5]}")
        for row in page:
            if row.get("symbol") != symbol:
                raise BinanceRestError(f"symbol 不一致: {row.get('symbol')!r}")
        last = times[-1]
        if previous_last is not None and last <= previous_last:
            raise PagingAnomaly(f"頁送りが進んでいない: {last} <= {previous_last}")

        seen_times.update(times)
        rows.extend(page)
        previous_last = last

        if len(page) < limit:
            break  # 最終ページ(仕様: 件数が limit を超えるときだけ切り詰められる)
        cursor = last + 1
        if cursor > end_ms:
            break
    else:
        raise PagingAnomaly(f"ページ数が上限 {MAX_PAGES} に達した")

    return rows, pages


# --------------------------------------------------------------------------
# 正規化(**Vision とは別ファイル**。canonical を置換しない)
# --------------------------------------------------------------------------

_TS = pl.Datetime(time_unit="ms", time_zone="UTC")


def _optional_price(cell: object) -> tuple[float | None, str]:
    """markPrice を (値, 分類) に。**欠測を補完しない。**

    分類: `"present"` / `"empty"`(空文字。実測で 2020 年に多数)/
    `"unparseable"` / `"non_positive"`。
    """
    if cell is None:
        return None, "empty"
    text = str(cell).strip()
    if not text:
        return None, "empty"
    try:
        value = float(text)
    except ValueError:
        return None, "unparseable"
    if value <= 0.0:
        # **落とさない。** 実際に返ってきた値であり、捏造ではない。数えて残す。
        return value, "non_positive"
    return value, "present"


def normalize_rest_funding(
    rows: list[dict],
    pages: list[PageProvenance],
    symbol: str = DEFAULT_SYMBOL,
    stats: dict | None = None,
) -> pl.DataFrame:
    """REST 行 → parquet 形式。**Vision の列名と衝突させない。**

    `funding_rate_rest` / `rest_funding_time` と名前を分けることで、
    照合前に取り違えることが型の水準で起きないようにする。
    """
    if not rows:
        return pl.DataFrame()

    # 行 → ページの対応は fundingTime の範囲で決まる(ページは互いに素)
    def owning_page(funding_time: int) -> PageProvenance | None:
        for provenance in pages:
            first, last = provenance.first_funding_time, provenance.last_funding_time
            if first is not None and last is not None and first <= funding_time <= last:
                return provenance
        return None

    marks: list[float | None] = []
    classes: list[str] = []
    for row in rows:
        value, kind = _optional_price(row.get("markPrice"))
        marks.append(value)
        classes.append(kind)
    if stats is not None:
        for kind in ("present", "empty", "unparseable", "non_positive"):
            stats[f"mark_price_{kind}_rows"] = sum(1 for c in classes if c == kind)

    times = [int(row["fundingTime"]) for row in rows]
    owners = [owning_page(t) for t in times]
    df = pl.DataFrame(
        {
            "rest_funding_time": times,
            "funding_rate_rest": [float(row["fundingRate"]) for row in rows],
            "mark_price": marks,
            "mark_price_status": classes,
            # 実在する追加フィールド。捨てずに持つ(Special は株式配当由来の追加 funding)
            "rate_type": [str(row.get("rateType", "")) for row in rows],
            "symbol": [symbol] * len(rows),
            "source": [SOURCE] * len(rows),
            "market_type": [MARKET_TYPE] * len(rows),
            "retrieved_at_utc": [o.retrieved_at_utc if o else None for o in owners],
            "response_sha256": [o.response_sha256 if o else None for o in owners],
            "page_index": [o.page_index if o else None for o in owners],
            "page_requested_start_ms": [o.requested_start_ms if o else None for o in owners],
            "page_requested_end_ms": [o.requested_end_ms if o else None for o in owners],
        },
        schema_overrides={"mark_price": pl.Float64},
    ).with_columns(pl.col("rest_funding_time").cast(_TS))
    return df.sort("rest_funding_time")


def apply_seal(df: pl.DataFrame, cutoff: datetime = FINAL_OOS_START) -> tuple[pl.DataFrame, int]:
    """封印域を**物理的に落とす**。戻り値は (残り, 落とした行数)。"""
    if df.is_empty():
        return df, 0
    kept = df.filter(pl.col("rest_funding_time") < cutoff)
    return kept, df.height - kept.height


# --------------------------------------------------------------------------
# 照合(canonical は **Vision**)
# --------------------------------------------------------------------------

#: 決済時刻の許容差。**probe で実測してから固定した値である。**
#: 2020-01 / 2025-12 の 186 決済で Vision `calc_time` と REST `fundingTime` の差は
#: 全件 **0 ms** だった。0 に固定すると「完全一致を仮定するな」に反するため、
#: Vision 側で実測されているサブ秒ジッタ(全 6,576 決済で 0〜47ms)を吸収する幅として
#: **1 秒**を採る。決済間隔(最小 1h)に対して十分小さく、
#: 隣の決済を取り違える余地が無い。
FUNDING_TIME_TOLERANCE_MS: Final = 1_000

MATCH_STATUSES: Final = (
    "matched",             # 1対1で、rate も一致した
    "rate_mismatch",       # 1対1だが rate が違う。**matched にしない**
    "ambiguous_multiple_rest",   # 許容差内に REST 候補が複数
    "ambiguous_shared_rest",     # 同じ REST 行に複数の Vision 決済が寄った
    "unmatched_vision",    # 許容差内に REST 候補が無い
)


def reconcile(
    vision: pl.DataFrame,
    rest: pl.DataFrame,
    tolerance_ms: int = FUNDING_TIME_TOLERANCE_MS,
) -> tuple[pl.DataFrame, dict]:
    """Vision(canonical)と REST を**一対一**で照合する。

    - **canonical は Vision 側**。REST の値で Vision を上書きしない。
    - **timestamp の完全一致を前提にしない。** `|Δ| <= tolerance_ms` で候補を採る。
    - 許容差内に候補が**複数あれば曖昧として拒否**する(近い方を選ばない)。
    - 1つの REST 行に**複数の Vision 決済が寄った場合も拒否**する(多対一)。
    - **rate が一致しなければ `matched` にしない。**
    - 一致しなかった行を**落とさない**。理由つきで残す。
    """
    if vision.is_empty():
        raise ValueError("Vision 系列が空である")
    vision_times = [int(t.timestamp() * 1000) for t in vision["ts"].to_list()]
    vision_rates = vision["funding_rate"].to_list()
    rest_times = [int(t.timestamp() * 1000) for t in rest["rest_funding_time"].to_list()] if not rest.is_empty() else []
    rest_rates = rest["funding_rate_rest"].to_list() if not rest.is_empty() else []
    rest_marks = rest["mark_price"].to_list() if not rest.is_empty() else []

    # 各 Vision 決済について許容差内の REST 候補を**全部**列挙する。
    # REST 側を時刻で整列してから二分探索で窓を切る(総当たりだと決済数の2乗になり、
    # 間隔が 1h へ切り替わった系列で現実的でなくなる)。**候補の取りこぼしはしない。**
    order = sorted(range(len(rest_times)), key=lambda j: rest_times[j])
    sorted_times = [rest_times[j] for j in order]
    candidates: list[list[int]] = []
    for ts in vision_times:
        left = bisect_left(sorted_times, ts - tolerance_ms)
        right = bisect_right(sorted_times, ts + tolerance_ms)
        candidates.append([order[k] for k in range(left, right)])

    # 多対一(1つの REST 行に複数の Vision 決済が寄った)を先に検出する
    claim_count: dict[int, int] = {}
    for hits in candidates:
        if len(hits) == 1:
            claim_count[hits[0]] = claim_count.get(hits[0], 0) + 1

    matched_rest: set[int] = set()
    out_rest_time: list[int | None] = []
    out_offset: list[int | None] = []
    out_rate_rest: list[float | None] = []
    out_mark: list[float | None] = []
    out_status: list[str] = []

    for i, hits in enumerate(candidates):
        if not hits:
            out_rest_time.append(None); out_offset.append(None)
            out_rate_rest.append(None); out_mark.append(None)
            out_status.append("unmatched_vision")
            continue
        if len(hits) > 1:
            out_rest_time.append(None); out_offset.append(None)
            out_rate_rest.append(None); out_mark.append(None)
            out_status.append("ambiguous_multiple_rest")
            continue
        j = hits[0]
        if claim_count.get(j, 0) > 1:
            out_rest_time.append(rest_times[j])
            out_offset.append(rest_times[j] - vision_times[i])
            out_rate_rest.append(rest_rates[j]); out_mark.append(rest_marks[j])
            out_status.append("ambiguous_shared_rest")
            continue
        out_rest_time.append(rest_times[j])
        out_offset.append(rest_times[j] - vision_times[i])
        out_rate_rest.append(rest_rates[j])
        out_mark.append(rest_marks[j])
        if rest_rates[j] == vision_rates[i]:
            out_status.append("matched")
            matched_rest.add(j)
        else:
            out_status.append("rate_mismatch")

    table = pl.DataFrame(
        {
            "ts": vision["ts"],
            "rest_funding_time": out_rest_time,
            "funding_time_offset_ms": out_offset,
            "funding_rate": vision_rates,
            "funding_rate_rest": out_rate_rest,
            "mark_price": out_mark,
            "match_status": out_status,
        },
        schema_overrides={
            "rest_funding_time": pl.Int64,
            "funding_time_offset_ms": pl.Int64,
            "funding_rate_rest": pl.Float64,
            "mark_price": pl.Float64,
        },
    ).with_columns(pl.col("rest_funding_time").cast(_TS))

    offsets = [o for o in out_offset if o is not None]
    offset_distribution: dict[str, int] = {}
    for o in offsets:
        offset_distribution[str(o)] = offset_distribution.get(str(o), 0) + 1
    summary = {
        "tolerance_ms": tolerance_ms,
        "vision_events": len(vision_times),
        "rest_events": len(rest_times),
        "matched_one_to_one": sum(1 for s in out_status if s == "matched"),
        "unmatched_vision": sum(1 for s in out_status if s == "unmatched_vision"),
        "unmatched_rest": len(rest_times) - len(matched_rest),
        "rate_mismatch": sum(1 for s in out_status if s == "rate_mismatch"),
        "ambiguous_multiple_rest": sum(1 for s in out_status if s == "ambiguous_multiple_rest"),
        "ambiguous_shared_rest": sum(1 for s in out_status if s == "ambiguous_shared_rest"),
        "offset_distribution_ms": dict(sorted(offset_distribution.items(), key=lambda kv: int(kv[0]))),
        "max_abs_offset_ms": max((abs(o) for o in offsets), default=0),
    }
    return table, summary


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def month_range_ms(start: str, end: str) -> tuple[int, int]:
    """"YYYY-MM" 区間を `[start_ms, end_ms]`(両端 inclusive)へ。

    `end_ms` は**翌月の頭の 1ms 前**。REST の `endTime` は inclusive なので、
    翌月頭ちょうどにすると隣の月の1件を必ず巻き込む(実測で確認した)。
    """
    listed = months(start, end)
    first = listed[0]
    last = listed[-1]
    sy, sm = (int(x) for x in first.split("-"))
    ey, em = (int(x) for x in last.split("-"))
    ny, nm = (ey + 1, 1) if em == 12 else (ey, em + 1)
    start_ms = int(datetime(sy, sm, 1, tzinfo=UTC).timestamp() * 1000)
    end_ms = int(datetime(ny, nm, 1, tzinfo=UTC).timestamp() * 1000) - 1
    return start_ms, end_ms


def fetch_to_parquet(
    start: str,
    end: str,
    symbol: str = DEFAULT_SYMBOL,
    out_path: Path | None = None,
    transport: Transport | None = None,
) -> dict:
    out_path = out_path or config.binance_funding_rate_rest_parquet(symbol)
    start_ms, end_ms = month_range_ms(start, end)
    rows, pages = fetch_funding_rates(
        symbol, start_ms=start_ms, end_ms=end_ms, transport=transport
    )
    stats: dict = {}
    df = normalize_rest_funding(rows, pages, symbol, stats)
    df, sealed = apply_seal(df)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_suffix(".parquet.tmp")
    df.write_parquet(tmp)
    tmp.replace(out_path)
    return {
        "symbol": symbol,
        "requested_start_ms": start_ms,
        "requested_end_ms": end_ms,
        "pages": [asdict(p) for p in pages],
        "rows": df.height,
        "sealed_rows_dropped": sealed,
        "path": out_path.as_posix(),
        **stats,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--start", default="2020-01", help="開始月 YYYY-MM")
    ap.add_argument("--end", default="2025-12", help="終了月 YYYY-MM(含む)")
    ap.add_argument("--symbol", default=DEFAULT_SYMBOL)
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args()
    result = fetch_to_parquet(args.start, args.end, args.symbol, args.out)
    print(
        f"funding_rate_rest: {args.start}..{args.end} pages={len(result['pages'])} "
        f"rows={result['rows']} sealed_dropped={result['sealed_rows_dropped']} "
        f"mark_present={result.get('mark_price_present_rows', 0)} "
        f"mark_empty={result.get('mark_price_empty_rows', 0)} -> {result['path']}"
    )


if __name__ == "__main__":
    main()

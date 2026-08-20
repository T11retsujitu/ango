"""Phase 8 の入力データ配管(公式 REST の funding markPrice)の適合テスト。

**ネットワークを一切使わない。** transport を差し替えて送信を封じる。
検査するのは「できないこと」と「黙って通してはいけないこと」である。

**取引系列を組み立てない。** rho / シグナル / 清算発生 / return / PnL を
一切計算しない。
"""

import inspect
import json
from datetime import datetime, timezone

import polars as pl
import pytest

from mce import binance_rest as br, config
from mce.backtest.splits import FINAL_OOS_START
from mce.binance_rest import (
    ALLOWED_PATHS,
    FORBIDDEN_PARAMS,
    FUNDING_RATE_PATH,
    FUNDING_TIME_TOLERANCE_MS,
    MAX_LIMIT,
    PagingAnomaly,
    RequestNotPermitted,
    fetch_funding_rates,
    normalize_rest_funding,
    public_get,
    reconcile,
)

UTC = timezone.utc
T0 = 1577836800000  # 2020-01-01T00:00:00Z
H8 = 8 * 3600 * 1000


def event(ts: int, rate: str = "0.00010000", mark: str = "10000.0", symbol: str = "BTCUSDT") -> dict:
    return {"symbol": symbol, "fundingTime": ts, "fundingRate": rate,
            "markPrice": mark, "rateType": "Regular"}


class FakeApi:
    """`startTime`/`endTime` を**両端 inclusive**、`limit` で切り詰める実物の挙動を模す。"""

    def __init__(self, events: list[dict]):
        self.events = sorted(events, key=lambda e: e["fundingTime"])
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, path: str, params: dict) -> tuple[int, bytes]:
        self.calls.append((path, dict(params)))
        lo, hi = params["startTime"], params["endTime"]
        hits = [e for e in self.events if lo <= e["fundingTime"] <= hi]
        hits = hits[: params["limit"]]
        return 200, json.dumps(hits).encode()


def constant_api(payload: list[dict]):
    calls: list[dict] = []

    def send(path: str, params: dict) -> tuple[int, bytes]:
        calls.append(dict(params))
        return 200, json.dumps(payload).encode()

    send.calls = calls  # type: ignore[attr-defined]
    return send


# --------------------------------------------------------------------------
# できないこと
# --------------------------------------------------------------------------


def test_only_the_public_funding_rate_path_is_allowed():
    assert ALLOWED_PATHS == frozenset({"/fapi/v1/fundingRate"})


@pytest.mark.parametrize("path", [
    "/fapi/v1/order", "/fapi/v1/batchOrders", "/fapi/v1/commissionRate",
    "/fapi/v2/positionRisk", "/fapi/v1/listenKey", "/fapi/v1/leverage",
])
def test_other_paths_are_refused_before_sending(path):
    sent = []
    with pytest.raises(RequestNotPermitted):
        public_get(path, {"symbol": "BTCUSDT"}, lambda p, q: sent.append(p) or (200, b"[]"))
    assert sent == [], "拒否したのに送信している"


@pytest.mark.parametrize("param", [
    "apiKey", "signature", "timestamp", "recvWindow", "side", "quantity",
    "price", "positionSide", "reduceOnly", "leverage", "listenKey",
])
def test_authentication_and_order_parameters_are_refused(param):
    sent = []
    with pytest.raises(RequestNotPermitted):
        public_get(FUNDING_RATE_PATH, {"symbol": "BTCUSDT", param: "x"},
                   lambda p, q: sent.append(p) or (200, b"[]"))
    assert sent == []


def test_forbidden_param_check_is_case_insensitive():
    assert "apikey" in FORBIDDEN_PARAMS
    with pytest.raises(RequestNotPermitted):
        public_get(FUNDING_RATE_PATH, {"APIKEY": "x"}, lambda p, q: (200, b"[]"))


def test_module_has_no_write_method_or_credential_path():
    """**GET 以外の外部操作がモジュールに存在しない。**"""
    source = inspect.getsource(br)
    for forbidden in (".post(", ".put(", ".delete(", ".patch(", "hmac", "X-MBX-APIKEY",
                      "BINANCE_API_KEY", "BINANCE_API_SECRET"):
        assert forbidden not in source, forbidden
    assert source.count(".get(") >= 1
    # httpx client を触るのは transport の1箇所だけ
    assert source.count("client.get(") == 1


def test_sealed_range_is_never_requested():
    """`ts >= 2026-01-01` は**要求もしない**。"""
    api = FakeApi([])
    cutoff_ms = int(FINAL_OOS_START.timestamp() * 1000)
    with pytest.raises(br.BinanceRestError):
        fetch_funding_rates(start_ms=T0, end_ms=cutoff_ms, transport=api)
    assert api.calls == [], "封印域を要求している"


def test_sealed_rows_are_refused_even_if_the_server_returns_them():
    cutoff_ms = int(FINAL_OOS_START.timestamp() * 1000)
    api = constant_api([event(cutoff_ms)])
    with pytest.raises(PagingAnomaly):
        fetch_funding_rates(start_ms=T0, end_ms=cutoff_ms - 1, transport=api)


def test_seal_drops_rows_at_or_after_cutoff():
    cutoff_ms = int(FINAL_OOS_START.timestamp() * 1000)
    rows = [event(cutoff_ms - H8), event(cutoff_ms)]
    _, pages = None, []
    df = normalize_rest_funding(rows, pages)
    kept, dropped = br.apply_seal(df)
    assert dropped == 1
    assert kept.height == 1
    assert kept["rest_funding_time"].max() < FINAL_OOS_START


# --------------------------------------------------------------------------
# ページング
# --------------------------------------------------------------------------


def test_paging_walks_a_thousand_row_pages_without_duplicates():
    events = [event(T0 + i * H8) for i in range(2500)]
    api = FakeApi(events)
    rows, pages = fetch_funding_rates(start_ms=T0, end_ms=T0 + 2500 * H8, transport=api)
    assert len(rows) == 2500
    assert len(pages) == 3, [p.row_count for p in pages]
    assert [p.row_count for p in pages] == [1000, 1000, 500]
    times = [r["fundingTime"] for r in rows]
    assert times == sorted(times)
    assert len(set(times)) == len(times), "重複がある"
    assert all(p.limit == MAX_LIMIT for p in pages)


def test_next_page_starts_one_ms_after_the_last_funding_time():
    """`startTime` は inclusive なので +1ms しないと境界が二重になる。"""
    events = [event(T0 + i * H8) for i in range(1500)]
    api = FakeApi(events)
    fetch_funding_rates(start_ms=T0, end_ms=T0 + 1500 * H8, transport=api)
    first_start = api.calls[0][1]["startTime"]
    second_start = api.calls[1][1]["startTime"]
    assert first_start == T0
    assert second_start == T0 + 999 * H8 + 1, "境界の +1ms が無い"


def test_inclusive_boundary_does_not_duplicate_the_edge_event():
    events = [event(T0 + i * H8) for i in range(1001)]
    api = FakeApi(events)
    rows, _ = fetch_funding_rates(start_ms=T0, end_ms=T0 + 1000 * H8, transport=api)
    times = [r["fundingTime"] for r in rows]
    assert len(times) == len(set(times)) == 1001


def test_a_server_that_keeps_returning_the_same_page_is_refused():
    """同じページを返し続ける相手に対して**進み続けない**。

    停止理由は「重複」が先に立つ(頁送りは last+1ms から要求するので、
    同じ行が返れば必ず既出になる)。無進行の検査はその後ろの防波堤である。
    """
    api = constant_api([event(T0 + i * H8) for i in range(MAX_LIMIT)])
    with pytest.raises(PagingAnomaly, match="重複|進んでいない"):
        fetch_funding_rates(start_ms=T0, end_ms=T0 + 5000 * H8, transport=api)
    assert len(api.calls) <= 2, "異常を検出したのに要求を続けている"


def test_no_progress_guard_exists_behind_the_duplicate_check():
    """無進行の停止条件そのものが実装されていること(到達しにくい防波堤)。"""
    source = inspect.getsource(br.fetch_funding_rates)
    assert "頁送りが進んでいない" in source
    assert "previous_last" in source


def test_descending_page_is_refused():
    api = constant_api([event(T0 + H8), event(T0)])
    with pytest.raises(PagingAnomaly, match="昇順"):
        fetch_funding_rates(start_ms=T0, end_ms=T0 + 10 * H8, transport=api)


def test_duplicate_funding_time_inside_a_page_is_refused():
    """ページ内の重複は**厳密昇順**の検査で捕まる(等しい隣接は昇順でない)。"""
    api = constant_api([event(T0), event(T0)])
    with pytest.raises(PagingAnomaly, match="昇順"):
        fetch_funding_rates(start_ms=T0, end_ms=T0 + 10 * H8, transport=api)


def test_rows_outside_the_requested_range_are_refused():
    api = constant_api([event(T0 - H8)])
    with pytest.raises(PagingAnomaly, match="範囲外"):
        fetch_funding_rates(start_ms=T0, end_ms=T0 + 10 * H8, transport=api)


def test_foreign_symbol_rows_are_refused():
    api = constant_api([event(T0, symbol="ETHUSDT")])
    with pytest.raises(br.BinanceRestError, match="symbol"):
        fetch_funding_rates(start_ms=T0, end_ms=T0 + 10 * H8, transport=api)


def test_limit_above_the_documented_maximum_is_refused():
    with pytest.raises(br.BinanceRestError):
        br.fetch_page("BTCUSDT", T0, T0 + H8, limit=MAX_LIMIT + 1,
                      transport=constant_api([]))


def test_page_provenance_records_range_hash_and_time():
    api = FakeApi([event(T0)])
    rows, pages = fetch_funding_rates(start_ms=T0, end_ms=T0 + H8, transport=api)
    page = pages[0]
    assert page.requested_start_ms == T0 and page.requested_end_ms == T0 + H8
    assert page.http_status == 200
    assert len(page.response_sha256) == 64
    assert datetime.fromisoformat(page.retrieved_at_utc).tzinfo is not None
    assert page.row_count == 1 and page.first_funding_time == T0


# --------------------------------------------------------------------------
# 正規化
# --------------------------------------------------------------------------


def test_rest_columns_do_not_collide_with_the_vision_series():
    _, pages = FakeApi([]), []
    df = normalize_rest_funding([event(T0)], pages)
    assert "rest_funding_time" in df.columns and "funding_rate_rest" in df.columns
    # Vision の canonical 列名を持たない = 取り違えが型の水準で起きない
    assert "ts" not in df.columns
    assert "funding_rate" not in df.columns
    assert "funding_interval_hours" not in df.columns


def test_missing_mark_price_is_null_and_counted_not_filled():
    """空文字の markPrice を**補完しない**(実測: 2020 年は全件空)。"""
    stats: dict = {}
    df = normalize_rest_funding(
        [event(T0, mark=""), event(T0 + H8, mark="10000.5")], [], stats=stats
    )
    assert df["mark_price"].to_list() == [None, 10000.5]
    assert stats["mark_price_empty_rows"] == 1
    assert stats["mark_price_present_rows"] == 1
    assert df["mark_price"].null_count() == 1


def test_unparseable_and_non_positive_mark_prices_are_classified():
    stats: dict = {}
    df = normalize_rest_funding(
        [event(T0, mark="abc"), event(T0 + H8, mark="0"), event(T0 + 2 * H8, mark="-5")],
        [], stats=stats,
    )
    assert stats["mark_price_unparseable_rows"] == 1
    assert stats["mark_price_non_positive_rows"] == 2
    # 非正値は**実際に返ってきた値**なので落とさない
    assert df["mark_price"].to_list() == [None, 0.0, -5.0]
    assert df["mark_price_status"].to_list() == ["unparseable", "non_positive", "non_positive"]


def test_rate_type_is_kept():
    df = normalize_rest_funding([event(T0)], [])
    assert df["rate_type"].to_list() == ["Regular"]


DATA_COLUMNS = [
    "rest_funding_time", "funding_rate_rest", "mark_price", "mark_price_status",
    "rate_type", "symbol", "source", "market_type",
]


def test_refetch_is_idempotent_in_data_while_provenance_records_the_new_retrieval(tmp_path):
    """再実行しても**データは1行も動かない**。取得時刻だけが更新される。

    `retrieved_at_utc` は「いつ取ったか」の記録なので、再取得したら新しい時刻に
    なるのが正しい。冪等なのはデータであって、出所の時刻印ではない。
    """
    events = [event(T0 + i * H8, rate=f"0.0000{i}", mark="" if i < 2 else f"{100 + i}.0")
              for i in range(5)]
    out = tmp_path / "funding_rate_rest_BTCUSDT.parquet"
    first = br.fetch_to_parquet("2020-01", "2020-01", out_path=out, transport=FakeApi(events))
    frame_a = pl.read_parquet(out)
    second = br.fetch_to_parquet("2020-01", "2020-01", out_path=out, transport=FakeApi(events))
    frame_b = pl.read_parquet(out)

    assert first["rows"] == second["rows"] == 5
    assert frame_a.select(DATA_COLUMNS).equals(frame_b.select(DATA_COLUMNS))
    assert frame_a["response_sha256"].to_list() == frame_b["response_sha256"].to_list()
    assert frame_a["mark_price"].null_count() == 2, "欠測が埋まっている"


# --------------------------------------------------------------------------
# 照合(canonical は Vision)
# --------------------------------------------------------------------------


def vision_frame(events: list[tuple[int, float]]) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "ts": [t for t, _ in events],
            "funding_rate": [r for _, r in events],
        }
    ).with_columns(pl.col("ts").cast(pl.Datetime(time_unit="ms", time_zone="UTC")))


def rest_frame(events: list[tuple[int, float, float | None]]) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "rest_funding_time": [t for t, _, _ in events],
            "funding_rate_rest": [r for _, r, _ in events],
            "mark_price": [m for _, _, m in events],
        },
        schema_overrides={"mark_price": pl.Float64},
    ).with_columns(pl.col("rest_funding_time").cast(pl.Datetime(time_unit="ms", time_zone="UTC")))


def test_reconcile_does_not_require_an_exact_timestamp_match():
    """許容差内のずれは matched。**完全一致を要求しない。**"""
    vision = vision_frame([(T0, 0.0001)])
    rest = rest_frame([(T0 + 7, 0.0001, 100.0)])
    table, summary = reconcile(vision, rest)
    assert table["match_status"].to_list() == ["matched"]
    assert table["funding_time_offset_ms"].to_list() == [7]
    assert summary["matched_one_to_one"] == 1
    assert summary["max_abs_offset_ms"] == 7


def test_reconcile_rejects_multiple_candidates_within_the_tolerance():
    """近い方を選ばない。**曖昧なら拒否する。**"""
    vision = vision_frame([(T0, 0.0001)])
    rest = rest_frame([(T0 - 5, 0.0001, 100.0), (T0 + 5, 0.0001, 101.0)])
    table, summary = reconcile(vision, rest)
    assert table["match_status"].to_list() == ["ambiguous_multiple_rest"]
    assert summary["matched_one_to_one"] == 0
    assert table["mark_price"].to_list() == [None], "曖昧なのに mark を採用している"


def test_reconcile_rejects_many_vision_events_sharing_one_rest_row():
    vision = vision_frame([(T0, 0.0001), (T0 + 100, 0.0001)])
    rest = rest_frame([(T0 + 50, 0.0001, 100.0)])
    table, summary = reconcile(vision, rest)
    assert table["match_status"].to_list() == ["ambiguous_shared_rest"] * 2
    assert summary["matched_one_to_one"] == 0
    assert summary["ambiguous_shared_rest"] == 2


def test_reconcile_does_not_call_a_rate_mismatch_matched():
    vision = vision_frame([(T0, 0.0001)])
    rest = rest_frame([(T0, 0.0002, 100.0)])
    table, summary = reconcile(vision, rest)
    assert table["match_status"].to_list() == ["rate_mismatch"]
    assert summary["matched_one_to_one"] == 0
    assert summary["rate_mismatch"] == 1
    # canonical の rate は Vision のまま
    assert table["funding_rate"].to_list() == [0.0001]


def test_reconcile_keeps_unmatched_rows_on_both_sides():
    vision = vision_frame([(T0, 0.0001), (T0 + H8, 0.0002)])
    rest = rest_frame([(T0, 0.0001, 100.0), (T0 + 3 * H8, 0.0003, 102.0)])
    table, summary = reconcile(vision, rest)
    assert table.height == 2, "Vision 行を落としている"
    assert table["match_status"].to_list() == ["matched", "unmatched_vision"]
    assert summary["unmatched_vision"] == 1
    assert summary["unmatched_rest"] == 1


def test_reconcile_never_overwrites_the_canonical_vision_rate():
    vision = vision_frame([(T0, 0.0001)])
    rest = rest_frame([(T0, 0.0009, 100.0)])
    table, _ = reconcile(vision, rest)
    assert table["funding_rate"].to_list() == [0.0001]
    assert table["funding_rate_rest"].to_list() == [0.0009]
    assert "funding_rate" in table.columns and "funding_rate_rest" in table.columns


def test_reconcile_does_not_fill_a_missing_mark_price():
    vision = vision_frame([(T0, 0.0001), (T0 + H8, 0.0002)])
    rest = rest_frame([(T0, 0.0001, None), (T0 + H8, 0.0002, 100.0)])
    table, _ = reconcile(vision, rest)
    assert table["mark_price"].to_list() == [None, 100.0]


def test_tolerance_is_far_below_the_shortest_possible_settlement_interval():
    """許容差は最短の決済間隔(1h)より桁違いに小さい = 隣を掴めない。"""
    assert FUNDING_TIME_TOLERANCE_MS == 1_000
    assert FUNDING_TIME_TOLERANCE_MS * 2 < 3600 * 1000


# --------------------------------------------------------------------------
# 実データ(取得済みのときだけ)
# --------------------------------------------------------------------------

REST_PATH = config.binance_funding_rate_rest_parquet()
VISION_PATH = config.binance_funding_rate_parquet()


@pytest.mark.skipif(not REST_PATH.exists(), reason="REST funding 未取得")
def test_real_rest_series_is_separate_and_sealed():
    rest = pl.read_parquet(REST_PATH)
    assert REST_PATH != VISION_PATH
    assert rest["rest_funding_time"].max() < FINAL_OOS_START
    times = [int(t.timestamp() * 1000) for t in rest["rest_funding_time"].to_list()]
    assert times == sorted(times) and len(times) == len(set(times))
    assert rest["market_type"].unique().to_list() == ["perp_linear"]
    assert rest["response_sha256"].null_count() == 0, "出所の無い行がある"


@pytest.mark.skipif(
    not (REST_PATH.exists() and VISION_PATH.exists()), reason="funding 未取得"
)
def test_real_vision_series_is_unchanged_by_the_rest_fetch():
    """canonical の列も行数も REST 取得で動いていない。"""
    vision = pl.read_parquet(VISION_PATH)
    assert set(vision.columns) == {
        "ts", "funding_rate", "funding_interval_hours", "funding_interval_ms",
        "funding_interval_hours_declared", "symbol", "source", "market_type",
    }
    assert "mark_price" not in vision.columns
    assert vision["ts"].max() < FINAL_OOS_START

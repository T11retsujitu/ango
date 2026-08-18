"""Aave 日次レート**入力系列**の検査。

**取引 artifact を検査しない。** rho も損益も存在しない。
系列本体は版管理外(`data/`)にあるため、無ければ artifact 検査は skip する。
"""

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from mce import phase8_prereg as P  # noqa: F401
from mce.aave_series import manifest, merge, to_record
from mce.aave_rates import ReserveListReading, ReserveReading, DailyObservation

UTC = timezone.utc
REPO = Path(__file__).resolve().parents[1]
SERIES = REPO / "data" / "phase8" / "aave_daily_rate_v1.jsonl"
MANIFEST = REPO / "data" / "manifests" / "aave_daily_rate_v1.json"


def _obs(day: str, mean, **kw) -> DailyObservation:
    lst = ReserveListReading("getReservesList()", ("0xaa", "0xbb"), 2, "f" * 64)
    comp = tuple(
        ReserveReading(s, "0x" + "0" * 40, 10**25, 0.01, 15, "e" * 64, None, 13)
        for s in P.RATE_ASSETS
    )
    base = dict(
        date_utc=day, target_ts=f"{day}T00:00:00+00:00", generation="aave_v3_core",
        network=P.RATE_MARKET_NETWORK, chain_id=1, market=P.RATE_MARKET_INSTANCE,
        block_number=1, block_timestamp=2, block_hash="0x0", endpoint="test",
        access_route=P.RATE_ACCESS_ROUTE, reserve_list=lst, members_present=P.RATE_ASSETS,
        missing_reserves=(), components=comp, mean_apr=mean, integrity_error=None,
        source_fidelity=P.RATE_SOURCE_FIDELITY, retrieved_at_utc="now",
    )
    base.update(kw)
    return DailyObservation(**base)


def test_to_record_keeps_the_list_hash_but_not_the_address_array():
    """アドレス列は保存しない(ブロックから再取得できる)。hash と要素数は残す。"""
    row = to_record(_obs("2024-06-01", 0.01))
    assert "addresses" not in row["reserve_list"]
    assert row["reserve_list"]["response_sha256"] == "f" * 64
    assert row["reserve_list"]["count"] == 2
    assert row["reserve_list"]["signature"] == "getReservesList()"


def test_to_record_does_not_alter_any_value():
    obs = _obs("2024-06-01", 0.51669)
    row = to_record(obs)
    assert row["mean_apr"] == 0.51669
    assert [c["apr_decimal"] for c in row["components"]] == [0.01, 0.01, 0.01]


def test_merge_rejects_a_calendar_gap(tmp_path):
    a = tmp_path / "a.jsonl"
    a.write_text("\n".join(json.dumps(to_record(_obs(d, 0.01)))
                           for d in ("2024-06-01", "2024-06-03")) + "\n", encoding="utf-8")
    with pytest.raises(SystemExit):
        merge([a], tmp_path / "out.jsonl")


def test_merge_rejects_conflicting_duplicates(tmp_path):
    a = tmp_path / "a.jsonl"
    b = tmp_path / "b.jsonl"
    a.write_text(json.dumps(to_record(_obs("2024-06-01", 0.01))) + "\n", encoding="utf-8")
    b.write_text(json.dumps(to_record(_obs("2024-06-01", 0.02))) + "\n", encoding="utf-8")
    with pytest.raises(SystemExit):
        merge([a, b], tmp_path / "out.jsonl")


def test_merge_orders_by_date_and_dedupes_identical_rows(tmp_path):
    days = ["2024-06-01", "2024-06-02", "2024-06-03"]
    a = tmp_path / "a.jsonl"
    b = tmp_path / "b.jsonl"
    a.write_text("\n".join(json.dumps(to_record(_obs(d, 0.01))) for d in days[::-1]) + "\n",
                 encoding="utf-8")
    b.write_text(json.dumps(to_record(_obs(days[1], 0.01))) + "\n", encoding="utf-8")
    rows = merge([b, a], tmp_path / "out.jsonl")
    assert [r["date_utc"] for r in rows] == days


def test_manifest_records_no_series_values_only_summary(tmp_path):
    out = tmp_path / "s.jsonl"
    rows = [to_record(_obs("2024-06-01", 0.01)),
            to_record(_obs("2024-06-02", None, missing_reserves=("USDT",), note="不在"))]
    out.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    man = manifest(rows, out)
    assert man["days_total"] == 2 and man["days_complete"] == 1 and man["days_missing"] == 1
    assert man["missing_days"][0]["missing_reserves"] == ["USDT"]
    assert man["completeness_rule"] == P.RATE_COMPLETENESS_RULE
    assert man["value_treatment"] == "no_filter_no_clip_no_smoothing_no_winsorization"
    assert len(man["sha256"]) == 64
    assert "mean_apr" not in json.dumps(man["by_generation"])


# --------------------------------------------------------------------------
# 実系列の検査(存在すれば)
# --------------------------------------------------------------------------

series_only = pytest.mark.skipif(
    not SERIES.exists(), reason="系列が未生成(版管理外の data/ にある)"
)


def _rows() -> list[dict]:
    return [json.loads(x) for x in SERIES.read_text(encoding="utf-8").splitlines() if x]


@series_only
def test_series_days_are_contiguous_and_unique():
    rows = _rows()
    days = [date.fromisoformat(r["date_utc"]) for r in rows]
    assert len(set(days)) == len(days)
    assert days == sorted(days)
    assert days[-1] - days[0] == timedelta(days=len(days) - 1)


@series_only
def test_series_never_forward_fills_across_a_missing_day():
    """欠測日は null のまま。前日値を運ばない。"""
    for r in _rows():
        if r["mean_apr"] is None:
            assert r["missing_reserves"] or r["integrity_error"] or r["note"]
        else:
            assert r["missing_reserves"] == []
            assert r["integrity_error"] is None


@series_only
def test_series_complete_days_have_all_three_members_and_no_zero_struct():
    for r in _rows():
        if r["mean_apr"] is None:
            continue
        assert sorted(r["members_present"]) == sorted(P.RATE_ASSETS), r["date_utc"]
        assert r["uninitialised_reserves"] == [], r["date_utc"]
        aprs = [c["apr_decimal"] for c in r["components"]]
        assert len(aprs) == 3 and all(a is not None for a in aprs)
        assert r["mean_apr"] == pytest.approx(sum(aprs) / 3)


@series_only
def test_series_generations_follow_the_frozen_splices_and_never_reach_v4():
    for r in _rows():
        ts = datetime.fromisoformat(r["target_ts"])
        expected = None
        for name, start, end in P.RATE_MARKET_SPLICES:
            if ts >= start and (end is None or ts < end):
                expected = name
        assert r["generation"] == expected, r["date_utc"]
        assert r["generation"] != "aave_v4"


@series_only
def test_series_blocks_never_exceed_their_target_and_are_monotone():
    prev = 0
    for r in _rows():
        if r["block_number"] is None:
            continue
        assert r["block_timestamp"] <= datetime.fromisoformat(r["target_ts"]).timestamp()
        assert r["block_number"] > prev, r["date_utc"]
        prev = r["block_number"]


@series_only
def test_series_carries_the_required_chain_provenance():
    for r in _rows():
        if r["block_number"] is None:
            continue
        assert r["chain_id"] == 1
        for field in P.RATE_PROVENANCE_REQUIRED:
            assert r[field] is not None, (r["date_utc"], field)
        assert r["access_route"] == P.RATE_ACCESS_ROUTE


@series_only
def test_series_v3_launch_gap_is_missing_not_zero():
    """§30.2 の期待帰結が実系列にも現れること(日付規則ではなく membership 由来)。"""
    rows = {r["date_utc"]: r for r in _rows()}
    for d in ("2023-01-27", "2023-01-28", "2023-02-13"):
        assert rows[d]["mean_apr"] is None, d
        assert rows[d]["generation"] == "aave_v3_core"
    assert rows["2023-02-14"]["mean_apr"] is not None
    assert rows["2023-01-26"]["generation"] == "aave_v2"


@series_only
def test_series_keeps_extreme_but_valid_launch_era_values():
    rows = {r["date_utc"]: r for r in _rows()}
    usdt = [c for c in rows["2020-12-08"]["components"] if c["symbol"] == "USDT"][0]
    assert usdt["apr_decimal"] == pytest.approx(0.51669, abs=1e-4)
    assert rows["2020-12-08"]["mean_apr"] == pytest.approx(0.212078, abs=1e-5)


@series_only
def test_manifest_matches_the_series_on_disk():
    assert MANIFEST.exists(), "manifest が無い"
    man = json.loads(MANIFEST.read_text(encoding="utf-8"))
    import hashlib
    assert man["sha256"] == hashlib.sha256(SERIES.read_bytes()).hexdigest()
    rows = _rows()
    assert man["days_total"] == len(rows)
    assert man["days_complete"] == sum(1 for r in rows if r["mean_apr"] is not None)
    # 系列は **v1.8.4 の下で再構成され、完了として受理された**。
    # 後続の改訂で作り直さないので、manifest の版は v1.8.4 のまま正しい。
    assert man["protocol_version"] == "v1.8.4"


# --------------------------------------------------------------------------
# transport の失敗を「レートが無い日」として記録しない
#
# §30.2 の null は **protocol state** についての言明である。取得パイプラインの
# 失敗を同じ null として載せると経済的な記録を偽ることになる。
# --------------------------------------------------------------------------

from mce.aave_series import classify  # noqa: E402


def _row(**kw) -> dict:
    base = {
        "mean_apr": None, "integrity_error": None, "generation": "aave_v3_core",
        "block_number": 100,
        "reserve_list": {"signature": "getReservesList()", "count": 7,
                         "response_sha256": "a" * 64, "error": None},
        "components": [{"symbol": s, "error": None} for s in P.RATE_ASSETS],
    }
    base.update(kw)
    return base


def test_classify_complete_and_integrity():
    assert classify(_row(mean_apr=0.03)) == "complete"
    assert classify(_row(integrity_error="不一致")) == "integrity_error"


def test_classify_protocol_absence_is_a_real_missing_day():
    assert classify(_row(generation=None, block_number=None)) == "missing_by_protocol"
    assert classify(_row()) == "missing_by_protocol"  # member 不足
    assert classify(_row(reserve_list={"signature": "x", "count": None,
                                       "response_sha256": "b" * 64,
                                       "error": "空応答(このブロックに reserve list が存在しない)"})
                    ) == "missing_by_protocol"
    comps = [{"symbol": "USDT", "error": "空応答(この世代/ブロックに reserve が存在しない)"},
             {"symbol": "USDC", "error": None}, {"symbol": "DAI", "error": None}]
    assert classify(_row(components=comps)) == "missing_by_protocol"


def test_classify_transport_failure_is_not_a_missing_day():
    """取得の失敗は観測ではない。"""
    assert classify(_row(block_number=None)) == "transport_failure"
    assert classify(_row(reserve_list=None)) == "transport_failure"
    assert classify(_row(reserve_list={"signature": "x", "count": None,
                                       "response_sha256": None,
                                       "error": "eth_call: JSON でない応答(https://x)"})
                    ) == "transport_failure"
    comps = [{"symbol": "USDT", "error": "eth_call: 空応答(https://x)"},
             {"symbol": "USDC", "error": None}, {"symbol": "DAI", "error": None}]
    assert classify(_row(components=comps)) == "transport_failure"


@series_only
def test_series_contains_no_transport_failures():
    """系列に取得失敗の行が1つも残っていないこと。"""
    bad = [r["date_utc"] for r in _rows() if classify(r) == "transport_failure"]
    assert bad == [], f"取得失敗が系列に混入している: {bad[:5]}"


@series_only
def test_manifest_reports_zero_transport_failures():
    man = json.loads(MANIFEST.read_text(encoding="utf-8"))
    assert man["days_transport_failure"] == 0

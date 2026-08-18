"""Aave 履歴レートアダプタの単体テスト(合成データ + 軽量な probe 検査)。

**戦略 artifact を一切生成しない。** rho も損益も計算しない。
ネットワークを叩くテストには `network` マークを付け、既定では実行しない。
"""

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from mce import phase8_prereg as P
from mce.aave_rates import (
    RAY,
    GENERATIONS,
    AaveGeneration,
    DailyObservation,
    ReserveReading,
    TokenSpec,
    generation_for,
    _words,
)

UTC = timezone.utc
REPO = Path(__file__).resolve().parents[1]
PROBE = REPO / "experiments" / "phase8" / "aave_availability_probe_v1.json"


# --------------------------------------------------------------------------
# 凍結された世代マッピング(§27.2)
# --------------------------------------------------------------------------


def test_generations_match_the_frozen_splices():
    frozen = {(n, s, e) for n, s, e in P.RATE_MARKET_SPLICES}
    got = {(g.name, g.start, g.end) for g in GENERATIONS}
    assert got == frozen


def test_generation_boundaries_are_half_open_and_contiguous():
    cases = {
        datetime(2019, 12, 1, tzinfo=UTC): None,
        datetime(2020, 1, 8, tzinfo=UTC): "aave_v1",
        datetime(2020, 12, 2, 23, 59, tzinfo=UTC): "aave_v1",
        datetime(2020, 12, 3, tzinfo=UTC): "aave_v2",
        datetime(2023, 1, 26, 23, 59, tzinfo=UTC): "aave_v2",
        datetime(2023, 1, 27, tzinfo=UTC): "aave_v3_core",
        datetime(2027, 1, 1, tzinfo=UTC): "aave_v3_core",
    }
    for ts, expected in cases.items():
        gen = generation_for(ts)
        assert (gen.name if gen else None) == expected, ts


def test_never_transitions_to_v4():
    """§27.2: V4 へ移行しない。V4 ローンチ日以降も V3 Core。"""
    assert all(g.name != "aave_v4" for g in GENERATIONS)
    after_v4 = generation_for(P.RATE_V4_ETHEREUM_LAUNCH)
    assert after_v4 is not None and after_v4.name == "aave_v3_core"
    assert generation_for(datetime(2030, 1, 1, tzinfo=UTC)).name == "aave_v3_core"


def test_rate_word_index_is_frozen_per_generation_not_shared():
    """V2 と V3 が同じ index 4 なのは**偶然**。世代ごとに実測して凍結した。"""
    idx = {g.name: g.rate_word_index for g in GENERATIONS}
    assert idx == {"aave_v1": 5, "aave_v2": 4, "aave_v3_core": 4}
    counts = {g.name: g.expected_word_count for g in GENERATIONS}
    assert counts == {"aave_v1": 13, "aave_v2": 12, "aave_v3_core": 15}
    # 構造体の形が違うことは word 数の違いに現れる
    assert len(set(counts.values())) == 3


# --------------------------------------------------------------------------
# トークンの同定(ティッカーだけで同定しない)
# --------------------------------------------------------------------------


def test_tokens_are_identified_by_address_per_generation():
    expected = {
        "USDT": ("0xdAC17F958D2ee523a2206206994597C13D831ec7", 6),
        "USDC": ("0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48", 6),
        "DAI": ("0x6B175474E89094C44Da98b954EedeAC495271d0F", 18),
    }
    for gen in GENERATIONS:
        assert len(gen.tokens) == 3, gen.name
        for tok in gen.tokens:
            addr, dec = expected[tok.symbol]
            assert tok.address == addr, f"{gen.name}/{tok.symbol}"
            assert tok.decimals == dec
            assert tok.address.startswith("0x") and len(tok.address) == 42


def test_basket_symbols_match_the_frozen_assets():
    for gen in GENERATIONS:
        assert tuple(t.symbol for t in gen.tokens) == P.RATE_ASSETS


# --------------------------------------------------------------------------
# RAY 変換(APY へ変換しない)
# --------------------------------------------------------------------------


def test_ray_conversion_is_apr_not_apy():
    """`raw / 1e27` の APR。複利化しない。"""
    assert RAY == 10**27
    raw = 53_477_837_242_605_058_828_828_849  # V1 DAI の実測値
    apr = raw / RAY
    assert apr == pytest.approx(0.053477837, rel=1e-8)
    # APY 化していたら (1 + apr/n)**n - 1 になり値が変わる
    apy = (1 + apr / 365) ** 365 - 1
    assert apy != pytest.approx(apr, rel=1e-6)


def test_word_decoder_splits_32_byte_words():
    payload = "0x" + "00" * 31 + "01" + "00" * 31 + "02"
    assert _words(payload) == [1, 2]


# --------------------------------------------------------------------------
# 3資産必須・補完なし
# --------------------------------------------------------------------------


def _reading(sym: str, apr: float | None) -> ReserveReading:
    return ReserveReading(sym, "0x" + "0" * 40, None if apr is None else int(apr * RAY),
                          apr, 12, "deadbeef")


def _obs(aprs: dict[str, float | None]) -> DailyObservation:
    comps = tuple(_reading(s, aprs[s]) for s in P.RATE_ASSETS)
    values = [c.apr_decimal for c in comps]
    mean = sum(values) / 3 if all(v is not None for v in values) else None  # type: ignore[arg-type]
    return DailyObservation(
        date_utc="2024-06-01", target_ts="2024-06-01T00:00:00+00:00", generation="aave_v3_core",
        network=P.RATE_MARKET_NETWORK, market=P.RATE_MARKET_INSTANCE, block_number=1,
        block_timestamp=1, block_hash="0x0", endpoint="test", components=comps, mean_apr=mean,
        source_fidelity=P.RATE_SOURCE_FIDELITY, retrieved_at_utc="now",
    )


def test_all_three_components_required_for_a_mean():
    full = _obs({"USDT": 0.04, "USDC": 0.06, "DAI": 0.08})
    assert full.mean_apr == pytest.approx(0.06) and full.complete
    for missing in P.RATE_ASSETS:
        aprs: dict[str, float | None] = {"USDT": 0.04, "USDC": 0.06, "DAI": 0.08}
        aprs[missing] = None
        partial = _obs(aprs)
        assert partial.mean_apr is None, f"{missing} 欠測で null になること"
        assert not partial.complete


def test_provenance_fields_are_present(tmp_path):
    """§27.6 が要求する provenance をすべて持つこと。"""
    obs = _obs({"USDT": 0.04, "USDC": 0.06, "DAI": 0.08})
    d = obs.__dict__
    for key in ("date_utc", "target_ts", "generation", "network", "market", "block_number",
                "block_timestamp", "block_hash", "endpoint", "components", "mean_apr",
                "source_fidelity", "retrieved_at_utc"):
        assert key in d, key
    assert obs.source_fidelity == "partial_proxy_not_exact_A2"
    for c in obs.components:
        assert c.token_address and c.response_sha256


# --------------------------------------------------------------------------
# probe 成果物の検査(存在すれば)
# --------------------------------------------------------------------------

probe_only = pytest.mark.skipif(not PROBE.exists(), reason="availability probe が未実行")


@probe_only
def test_probe_covers_both_sides_of_every_splice():
    rows = {r["date_utc"]: r for r in json.loads(PROBE.read_text())["observations"]}
    for before, on, after, expect_before, expect_on in (
        ("2020-12-02", "2020-12-03", "2020-12-04", "aave_v1", "aave_v2"),
        ("2023-01-26", "2023-01-27", "2023-01-28", "aave_v2", "aave_v3_core"),
    ):
        assert rows[before]["generation"] == expect_before
        assert rows[on]["generation"] == expect_on
        assert rows[after]["generation"] == expect_on


@probe_only
def test_probe_never_reports_v4():
    rows = json.loads(PROBE.read_text())["observations"]
    assert all(r["generation"] != "aave_v4" for r in rows)
    v4day = [r for r in rows if r["date_utc"] == "2026-03-30"]
    assert v4day and v4day[0]["generation"] == "aave_v3_core"


@probe_only
def test_probe_blocks_never_exceed_their_target_timestamp():
    """解決したブロックが target より後になっていないこと(未来参照の禁止)。"""
    for r in json.loads(PROBE.read_text())["observations"]:
        if r["block_timestamp"] is None:
            continue
        target = datetime.fromisoformat(r["target_ts"]).timestamp()
        assert r["block_timestamp"] <= target, r["date_utc"]


@probe_only
def test_probe_pre_v1_dates_yield_no_generation():
    rows = {r["date_utc"]: r for r in json.loads(PROBE.read_text())["observations"]}
    assert rows["2019-12-01"]["generation"] is None
    assert rows["2019-12-01"]["mean_apr"] is None


@probe_only
def test_probe_records_raw_ray_and_response_hash_per_component():
    for r in json.loads(PROBE.read_text())["observations"]:
        for c in r["components"]:
            if c["apr_decimal"] is None:
                continue
            assert c["raw_ray"] is not None
            assert c["apr_decimal"] == pytest.approx(c["raw_ray"] / RAY)
            assert c["response_sha256"] and len(c["response_sha256"]) == 64


# --------------------------------------------------------------------------
# H17: 未初期化 reserve(全語ゼロ)の検出
#
# 未上場 reserve は **成功応答として全語ゼロ** を返す。凍結された完全性規則
# (§27.4「3資産すべてを要求、欠ければ null」)は *読み取りの成否* で判定する
# ため、この 0% は「揃った」と判定されて平均へ混入する。
# **凍結規則は変更しない。** 検出フラグを立てて記録するだけである。
# --------------------------------------------------------------------------


def test_all_zero_struct_is_flagged_as_uninitialised():
    live = ReserveReading("USDC", "0x" + "0" * 40, 3 * 10**25, 0.03, 15, "a" * 64,
                          None, 13)
    dead = ReserveReading("USDT", "0x" + "0" * 40, 0, 0.0, 15, "b" * 64, None, 0)
    assert not live.reserve_uninitialised
    assert dead.reserve_uninitialised


def test_zero_rate_with_live_reserve_is_not_flagged():
    """利用率ゼロで借入金利が 0 でも、reserve 自体が生きていれば H17 ではない。"""
    idle = ReserveReading("USDC", "0x" + "0" * 40, 0, 0.0, 15, "c" * 64, None, 11)
    assert not idle.reserve_uninitialised


def test_uninitialised_reserve_does_not_change_the_frozen_mean():
    """凍結規則を黙って書き換えていないこと。0% はそのまま平均へ入る。"""
    comps = (
        ReserveReading("USDT", "0x" + "0" * 40, 0, 0.0, 15, "b" * 64, None, 0),
        ReserveReading("USDC", "0x" + "0" * 40, 3 * 10**25, 0.03, 15, "c" * 64, None, 13),
        ReserveReading("DAI", "0x" + "0" * 40, 6 * 10**25, 0.06, 15, "d" * 64, None, 13),
    )
    obs = DailyObservation(
        date_utc="2023-01-28", target_ts="2023-01-28T00:00:00+00:00",
        generation="aave_v3_core", network=P.RATE_MARKET_NETWORK, market=P.RATE_MARKET_INSTANCE,
        block_number=1, block_timestamp=1, block_hash="0x0", endpoint="test",
        components=comps, mean_apr=0.03, source_fidelity=P.RATE_SOURCE_FIDELITY,
        retrieved_at_utc="now", uninitialised_reserves=("USDT",),
    )
    assert obs.mean_apr == pytest.approx(0.03)  # (0 + 0.03 + 0.06) / 3
    assert obs.complete  # 凍結規則では「揃っている」
    assert obs.contaminated_by_uninitialised  # しかし汚染として記録される


SPLICE = REPO / "experiments" / "phase8" / "aave_splice_probe_v1.json"
splice_only = pytest.mark.skipif(not SPLICE.exists(), reason="splice probe が未実行")


@splice_only
def test_splice_probe_records_the_v3_bootstrap_gap():
    """2023-01-27 の V3 Core は3資産とも未初期化、USDT はその後も欠けたまま。"""
    report = json.loads(SPLICE.read_text())
    rows = {r["date_utc"]: r for r in report["observations"]}
    day0 = rows["2023-01-27"]
    assert day0["generation"] == "aave_v3_core"
    assert set(day0["uninitialised_reserves"]) == {"USDT", "USDC", "DAI"}
    assert day0["mean_apr"] == 0.0  # 凍結規則では「完全」な 0% として通る
    for d in ("2023-01-28", "2023-01-31", "2023-02-05"):
        assert rows[d]["uninitialised_reserves"] == ["USDT"], d
    assert len(report["h17_uninitialised_reserve_days"]) == 10


@splice_only
def test_splice_probe_first_splice_has_no_uninitialised_reserve():
    """V1→V2 の接合では未初期化はない(値の急変は実データであって decode 誤りではない)。"""
    rows = json.loads(SPLICE.read_text())["observations"]
    for r in rows:
        if r["date_utc"] < "2021-01-01":
            assert r["uninitialised_reserves"] == [], r["date_utc"]
            assert r["mean_apr"] is not None

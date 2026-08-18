"""Aave 履歴レートアダプタの単体テスト(合成データ + 軽量な probe 検査)。

**戦略 artifact を一切生成しない。** rho も損益も計算しない。
ネットワークを叩くテストには `network` マークを付け、既定では実行しない。
"""

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from mce import phase8_prereg as P
from mce import aave_rates
from mce.aave_rates import (
    ETHEREUM_MAINNET_CHAIN_ID,
    RAY,
    GENERATIONS,
    AaveGeneration,
    DailyObservation,
    ReserveListReading,
    ReserveReading,
    daily_observation,
    generation_for,
    _decode_address_array,
    _words,
)

UTC = timezone.utc
REPO = Path(__file__).resolve().parents[1]
PROBE = REPO / "experiments" / "phase8" / "aave_availability_probe_v1.json"
SPLICE = REPO / "experiments" / "phase8" / "aave_splice_probe_v1.json"


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


def test_address_array_decoder_reads_offset_and_length():
    payload = "0x" + f"{32:064x}" + f"{2:064x}" + f"{0xaa:064x}" + f"{0xbb:064x}"
    assert _decode_address_array(payload) == ("0x" + "0" * 38 + "aa", "0x" + "0" * 38 + "bb")


# --------------------------------------------------------------------------
# v1.8.4 §30 の適合テスト
#
# ネットワークを使わない **合成 RPC** で `daily_observation()` を端から端まで
# 駆動する。dataclass を手で組むのではなく、実際の呼び出し経路を検査する。
# --------------------------------------------------------------------------

DAY = datetime(2024, 6, 1, tzinfo=UTC)
HEAD = 1000
ANSWER_BLOCK = 500  # ts(ANSWER_BLOCK) == target ちょうど
BLOCK_TIME = 12


def _ts_of(n: int) -> int:
    return int(DAY.timestamp()) - (ANSWER_BLOCK - n) * BLOCK_TIME


def _encode_address_array(addrs) -> str:
    body = f"{32:064x}" + f"{len(addrs):064x}"
    body += "".join(f"{int(a, 16):064x}" for a in addrs)
    return "0x" + body


def _encode_struct(gen: AaveGeneration, rate_ray: int | None) -> str:
    """`rate_ray is None` なら**全語ゼロ**(=未上場 reserve の実挙動)。"""
    words = [0] * gen.expected_word_count
    if rate_ray is not None:
        words[1] = 10**27  # liquidityIndex ≈ 1.0(初期化済みの印)
        words[gen.rate_word_index] = rate_ray
    return "0x" + "".join(f"{w:064x}" for w in words)


class FakeChain:
    """合成 RPC。**eth_call がどのブロックで呼ばれたか**を記録する。"""

    def __init__(self, gen: AaveGeneration, listed, structs, *, chain_id=1):
        self.gen = gen
        self.listed = [a.lower() for a in listed]
        self.structs = {k.lower(): v for k, v in structs.items()}
        self.chain_id = chain_id
        self.calls: list[tuple[str, str]] = []  # (何を読んだか, block hex)

    def __call__(self, endpoint, method, params, timeout=40):
        if method == "eth_chainId":
            res = hex(self.chain_id)
        elif method == "eth_blockNumber":
            res = hex(HEAD)
        elif method == "eth_getBlockByNumber":
            n = int(params[0], 16)
            res = {"timestamp": hex(_ts_of(n)), "hash": "0x%064x" % n}
        elif method == "eth_call":
            call, blk = params[0], params[1]
            data = call["data"]
            if data == self.gen.reserve_list_selector:
                self.calls.append(("reserve_list", blk))
                res = _encode_address_array(self.listed) if self.listed else "0x"
            else:
                token = "0x" + data[-40:]
                self.calls.append((token.lower(), blk))
                res = self.structs.get(token.lower(), "0x")
        else:  # pragma: no cover
            raise AssertionError(method)
        return {"jsonrpc": "2.0", "id": 1, "result": res}, json.dumps({"result": res})


V3 = GENERATIONS[2]
USDT, USDC, DAI = (t.address for t in V3.tokens)


def _run(monkeypatch, fake: FakeChain) -> DailyObservation:
    monkeypatch.setattr(aave_rates, "_rpc", fake)
    return daily_observation("fake://", DAY, chain_id=ETHEREUM_MAINNET_CHAIN_ID)


def _healthy(rates=(0.04, 0.06, 0.08)) -> dict:
    return {a: _encode_struct(V3, int(r * RAY)) for a, r in zip((USDT, USDC, DAI), rates)}


def test_conformance_uninitialised_reserve_cannot_enter_the_basket_as_zero(monkeypatch):
    """(1) 未上場 reserve は 0% として basket に入れない。"""
    structs = _healthy()
    structs[USDT.lower()] = _encode_struct(V3, None)  # 全語ゼロ = 未上場
    fake = FakeChain(V3, [USDC, DAI], structs)  # USDT は list に不在
    obs = _run(monkeypatch, fake)
    assert obs.mean_apr is None, "0% が混入して平均が出てしまっている"
    assert obs.missing_reserves == ("USDT",)
    assert not obs.complete
    # 「0.04667 = (0 + 0.06 + 0.08)/3」のような値が出ていないこと
    assert obs.basket_size == 0


def test_conformance_genuine_zero_rate_on_a_listed_reserve_stays_valid(monkeypatch):
    """(2) 初期化済み reserve の**本物の 0% 借入金利**は有効な観測として残る。"""
    structs = _healthy((0.0, 0.06, 0.09))  # USDT が真に 0%(liquidityIndex は非ゼロ)
    fake = FakeChain(V3, [USDT, USDC, DAI], structs)
    obs = _run(monkeypatch, fake)
    assert obs.complete
    assert obs.mean_apr == pytest.approx(0.05)
    assert obs.missing_reserves == ()
    assert obs.uninitialised_reserves == ()  # 全語ゼロではない
    assert obs.integrity_error is None
    usdt = [c for c in obs.components if c.symbol == "USDT"][0]
    assert usdt.apr_decimal == 0.0 and usdt.zero_struct_state == "initialised"


def test_conformance_membership_is_read_at_the_same_block_as_the_rate(monkeypatch):
    """(3) membership は rate 読み取りと**同一の履歴ブロック**で検査される。"""
    fake = FakeChain(V3, [USDT, USDC, DAI], _healthy())
    obs = _run(monkeypatch, fake)
    assert obs.block_number == ANSWER_BLOCK
    assert obs.block_timestamp == int(DAY.timestamp())
    blocks = {blk for _, blk in fake.calls}
    assert blocks == {hex(ANSWER_BLOCK)}, f"ブロックが揃っていない: {fake.calls}"
    kinds = [k for k, _ in fake.calls]
    assert kinds[0] == "reserve_list"  # membership を先に確定させる
    assert set(kinds[1:]) == {USDT.lower(), USDC.lower(), DAI.lower()}


def test_conformance_one_missing_asset_nulls_the_whole_daily_basket(monkeypatch):
    """(4) 3資産のうち1つでも欠ければ、その日の basket 全体が null。"""
    for absent in (USDT, USDC, DAI):
        listed = [a for a in (USDT, USDC, DAI) if a != absent]
        structs = _healthy()
        structs[absent.lower()] = _encode_struct(V3, None)
        obs = _run(monkeypatch, FakeChain(V3, listed, structs))
        assert obs.mean_apr is None, absent
        assert len(obs.missing_reserves) == 1
        assert obs.note and "0 で代替しない" in obs.note


def test_conformance_v2_v3_splice_is_still_2023_01_27():
    """(5) V2→V3 の接合日は動かさない。"""
    assert P.RATE_MARKET_SPLICES[1][2] == datetime(2023, 1, 27, tzinfo=UTC)
    assert P.RATE_MARKET_SPLICES[2][1] == datetime(2023, 1, 27, tzinfo=UTC)
    assert generation_for(datetime(2023, 1, 26, 23, 59, 59, tzinfo=UTC)).name == "aave_v2"
    assert generation_for(datetime(2023, 1, 27, tzinfo=UTC)).name == "aave_v3_core"
    assert P.RATE_SPLICE_DATES_MOVABLE is False
    assert P.RATE_GENERATION_EXTENSION_ALLOWED is False


def test_conformance_no_two_asset_fallback_exists(monkeypatch):
    """(6) 2資産へ縮退する経路が存在しない。"""
    assert P.RATE_TWO_ASSET_FALLBACK_ALLOWED is False
    assert P.RATE_BASKET_REQUIRE_ALL is True
    for listed in ([USDT, USDC], [USDC, DAI], [USDT, DAI]):
        structs = _healthy()
        for a in (USDT, USDC, DAI):
            if a not in listed:
                structs[a.lower()] = _encode_struct(V3, None)
        obs = _run(monkeypatch, FakeChain(V3, listed, structs))
        assert obs.mean_apr is None
        assert obs.basket_size == 0, "2資産で平均を作ってしまっている"


def test_conformance_zero_struct_and_membership_disagreement_blocks_output(monkeypatch):
    """(7) 全語ゼロ診断と membership が食い違ったら値を出さない。"""
    # 食い違い A: list は member と言うが構造体は全語ゼロ
    structs = _healthy()
    structs[DAI.lower()] = _encode_struct(V3, None)
    obs = _run(monkeypatch, FakeChain(V3, [USDT, USDC, DAI], structs))
    assert obs.mean_apr is None
    assert obs.integrity_error and "DAI" in obs.integrity_error
    assert "全語ゼロ" in obs.integrity_error

    # 食い違い B: list は非 member と言うが構造体は初期化済み
    obs2 = _run(monkeypatch, FakeChain(V3, [USDT, USDC], _healthy()))
    assert obs2.mean_apr is None
    assert obs2.integrity_error and "DAI" in obs2.integrity_error
    assert "初期化済み" in obs2.integrity_error


def test_disagreement_does_not_pick_a_side(monkeypatch):
    """食い違い時に membership 側にも全語ゼロ側にも寄せていないこと。"""
    structs = _healthy()
    structs[DAI.lower()] = _encode_struct(V3, None)
    obs = _run(monkeypatch, FakeChain(V3, [USDT, USDC, DAI], structs))
    assert obs.mean_apr is None            # membership 側に寄せて 0 を入れていない
    assert obs.missing_reserves == ()      # 全語ゼロ側に寄せて欠測扱いにもしていない
    assert obs.integrity_error is not None


# --------------------------------------------------------------------------
# D1: source of truth と transport の分離
# --------------------------------------------------------------------------


def test_source_of_truth_is_contract_state_not_the_rpc_provider():
    assert P.RATE_SOURCE_OF_TRUTH == "aave_contract_state_on_ethereum_mainnet"
    assert P.RATE_ACCESS_ROUTE == "archive_rpc_eth_call"
    assert P.RATE_ACCESS_PROVIDER_ROLE == "transport_not_economic_source"
    assert P.RATE_CHAIN_ID == ETHEREUM_MAINNET_CHAIN_ID == 1


def test_non_mainnet_chain_id_is_rejected(monkeypatch):
    fake = FakeChain(V3, [USDT, USDC, DAI], _healthy(), chain_id=137)
    monkeypatch.setattr(aave_rates, "_rpc", fake)
    obs = daily_observation("fake://", DAY, chain_id=137)
    assert obs.mean_apr is None
    assert obs.integrity_error and "chain id" in obs.integrity_error


def test_every_observation_retains_the_required_chain_provenance(monkeypatch):
    obs = _run(monkeypatch, FakeChain(V3, [USDT, USDC, DAI], _healthy()))
    for field in P.RATE_PROVENANCE_REQUIRED:
        assert getattr(obs, field) is not None, field
    assert obs.chain_id == 1
    assert obs.block_hash.startswith("0x") and len(obs.block_hash) == 66
    assert obs.reserve_list.response_sha256 and len(obs.reserve_list.response_sha256) == 64
    for c in obs.components:
        assert c.token_address and c.response_sha256


def test_block_resolution_hint_does_not_change_the_answer(monkeypatch):
    """`hint` は純粋な高速化。答えを変えない。"""
    fake = FakeChain(V3, [USDT, USDC, DAI], _healthy())
    monkeypatch.setattr(aave_rates, "_rpc", fake)
    without = daily_observation("fake://", DAY, chain_id=1)
    for h in (2, 300, ANSWER_BLOCK, 900, HEAD):
        with_hint = daily_observation("fake://", DAY, hint=h, chain_id=1)
        assert with_hint.block_number == without.block_number == ANSWER_BLOCK, h
        assert with_hint.mean_apr == without.mean_apr


# --------------------------------------------------------------------------
# O1: launch 期の有効値を加工しない
# --------------------------------------------------------------------------


def test_o1_extreme_but_valid_launch_era_rates_are_not_altered(monkeypatch):
    """51.669% のような値も clip / winsorize / smooth しない。"""
    assert P.RATE_VALUE_TREATMENT == "no_filter_no_clip_no_smoothing_no_winsorization"
    obs = _run(monkeypatch, FakeChain(V3, [USDT, USDC, DAI], _healthy((0.51669, 0.03391, 0.08563))))
    got = {c.symbol: c.apr_decimal for c in obs.components}
    assert got["USDT"] == pytest.approx(0.51669)
    assert obs.mean_apr == pytest.approx((0.51669 + 0.03391 + 0.08563) / 3)


def test_no_interpolation_or_forward_fill_is_declared_or_implemented():
    assert P.RATE_INTERPOLATION == "none"
    assert P.RATE_FORWARD_FILL_ALLOWED is False
    assert P.RATE_ZERO_SUBSTITUTION_ALLOWED is False
    src = (REPO / "src" / "mce" / "aave_rates.py").read_text(encoding="utf-8")
    for banned in ("ffill", "fillna", "interpolate", "rolling", "clip("):
        assert banned not in src, banned


def test_reserve_list_primitive_is_frozen_per_generation():
    """V1 だけ primitive 名が違う。共通名で呼べると仮定しない。"""
    got = {g.name: g.reserve_list_signature for g in GENERATIONS}
    assert got == dict(P.RATE_RESERVE_LIST_PRIMITIVE)
    assert dict(GENERATIONS[0].__dict__)["reserve_list_selector"] == "0x0902f1ac"
    assert GENERATIONS[1].reserve_list_selector == "0xd1946dbc"
    assert GENERATIONS[2].reserve_list_selector == "0xd1946dbc"
    assert GENERATIONS[0].reserve_list_selector != GENERATIONS[1].reserve_list_selector


def test_zero_struct_detector_is_only_a_cross_check():
    assert P.RATE_ZERO_STRUCT_DIAGNOSTIC == "independent_cross_check_only"
    assert P.RATE_COMPLETENESS_RULE == (
        "initialized_reserve_list_membership_at_observation_block"
    )
    assert P.RATE_MEMBERSHIP_BLOCK_RULE == "same_block_as_rate_read"
    assert P.RATE_INTEGRITY_DISAGREEMENT_ACTION == "emit_integrity_error_and_no_rate_value"


def test_zero_struct_state_is_tri_valued():
    live = ReserveReading("USDC", "0x" + "0" * 40, 3 * 10**25, 0.03, 15, "a" * 64, None, 13)
    dead = ReserveReading("USDT", "0x" + "0" * 40, 0, 0.0, 15, "b" * 64, None, 0)
    blank = ReserveReading("DAI", "0x" + "0" * 40, None, None, 0, "c" * 64, "空応答", 0)
    assert live.zero_struct_state == "initialised" and not live.reserve_uninitialised
    assert dead.zero_struct_state == "uninitialised" and dead.reserve_uninitialised
    assert blank.zero_struct_state == "unreadable" and not blank.reserve_uninitialised


def test_reserve_list_membership_is_case_insensitive_on_address():
    lst = ReserveListReading("getReservesList()", (USDT.lower(), DAI.lower()), 2, "d" * 64)
    by_symbol = {t.symbol: t for t in V3.tokens}
    assert lst.contains(by_symbol["USDT"])   # 凍結側は mixed case、list 側は小文字
    assert lst.contains(by_symbol["DAI"])
    assert not lst.contains(by_symbol["USDC"])


# --------------------------------------------------------------------------
# probe 成果物の検査(存在すれば)
# --------------------------------------------------------------------------

probe_only = pytest.mark.skipif(not PROBE.exists(), reason="availability probe が未実行")
splice_only = pytest.mark.skipif(not SPLICE.exists(), reason="splice probe が未実行")


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


@probe_only
def test_probe_records_chain_and_block_provenance():
    report = json.loads(PROBE.read_text())
    assert report["chain_id"] == 1
    assert report["source_of_truth"] == P.RATE_SOURCE_OF_TRUTH
    assert report["access_route"] == P.RATE_ACCESS_ROUTE
    for r in report["observations"]:
        if r["block_number"] is None:
            continue
        for field in P.RATE_PROVENANCE_REQUIRED:
            assert r[field] is not None, (r["date_utc"], field)


@probe_only
def test_probe_has_no_integrity_errors():
    """実データ上で membership と全語ゼロ診断が食い違っていないこと。"""
    assert json.loads(PROBE.read_text())["integrity_errors"] == []


@splice_only
def test_splice_probe_v3_launch_days_are_missing_not_zero():
    """§30.2 の期待帰結: V3 launch 期は **0% ではなく欠測**になる。"""
    report = json.loads(SPLICE.read_text())
    rows = {r["date_utc"]: r for r in report["observations"]}
    day0 = rows["2023-01-27"]
    assert day0["generation"] == "aave_v3_core"
    assert day0["mean_apr"] is None
    assert set(day0["missing_reserves"]) == {"USDT", "USDC", "DAI"}
    for d in ("2023-01-28", "2023-01-31", "2023-02-05"):
        assert rows[d]["mean_apr"] is None, d
        assert rows[d]["missing_reserves"] == ["USDT"], d
    assert report["integrity_errors"] == []
    # 欠測は **membership から導出**されており、日付規則ではない
    assert P.RATE_LAUNCH_GAP_DERIVATION == "derived_from_historical_reserve_membership"


@splice_only
def test_splice_probe_keeps_valid_launch_era_values_unaltered():
    """O1: V1→V2 接合直後の極端だが有効な値を落としていないこと。"""
    rows = {r["date_utc"]: r for r in json.loads(SPLICE.read_text())["observations"]}
    assert rows["2020-12-08"]["mean_apr"] == pytest.approx(0.212078, abs=1e-5)
    usdt = [c for c in rows["2020-12-08"]["components"] if c["symbol"] == "USDT"][0]
    assert usdt["apr_decimal"] == pytest.approx(0.51669, abs=1e-4)
    for d in ("2020-11-30", "2020-12-03", "2020-12-11"):
        assert rows[d]["mean_apr"] is not None, d

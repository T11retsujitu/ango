"""H13 の資格情報ユーティリティの安全性テスト。

**ネットワークを一切使わない。** 実際の送信は runner を差し替えて封じる。
検査するのは「できないこと」が本当にできないかである。
"""

import hashlib
import hmac
import json
from urllib.parse import parse_qs, urlparse

import pytest

from mce import binance_commission as bc
from mce.binance_commission import (
    ALLOWED_PATHS,
    FORBIDDEN_PARAMS,
    CommissionRecord,
    CredentialError,
    Credentials,
    RequestNotPermitted,
    fetch_commission_rate,
    parse_commission,
    sign,
    signed_get,
)

CREDS = Credentials("KEY-abc123", "SECRET-xyz789")
BODY = json.dumps({"symbol": "BTCUSDT", "makerCommissionRate": "0.000200",
                   "takerCommissionRate": "0.000500"})


class Recorder:
    """送信の代わりに curl 設定を捕まえる。**実際には何も送らない。**"""

    def __init__(self, body: str = BODY):
        self.body = body
        self.configs: list[str] = []

    def __call__(self, config: str, timeout: int) -> str:
        self.configs.append(config)
        return self.body

    @property
    def url(self) -> str:
        line = [x for x in self.configs[-1].splitlines() if x.startswith("url = ")][0]
        return line[len("url = "):].strip('"')


# --------------------------------------------------------------------------
# できないこと
# --------------------------------------------------------------------------


def test_only_one_path_is_allowed():
    assert ALLOWED_PATHS == frozenset({"/fapi/v1/commissionRate"})


@pytest.mark.parametrize("path", [
    "/fapi/v1/order", "/fapi/v1/batchOrders", "/fapi/v2/positionRisk",
    "/fapi/v1/leverage", "/sapi/v1/futures/transfer", "/fapi/v1/listenKey",
    "/fapi/v1/allOpenOrders", "/fapi/v1/marginType",
])
def test_dangerous_paths_are_refused_before_sending(path):
    rec = Recorder()
    with pytest.raises(RequestNotPermitted):
        signed_get(path, {"symbol": "BTCUSDT"}, CREDS, runner=rec)
    assert rec.configs == [], "拒否したのに送信しようとしている"


@pytest.mark.parametrize("param", sorted(FORBIDDEN_PARAMS))
def test_order_parameters_are_refused(param):
    rec = Recorder()
    with pytest.raises(RequestNotPermitted):
        signed_get("/fapi/v1/commissionRate", {param: "x"}, CREDS, runner=rec)
    assert rec.configs == []


def test_no_write_method_exists_in_the_module():
    """POST / PUT / DELETE の経路がそもそも無いこと。"""
    src = (bc.__file__ and open(bc.__file__, encoding="utf-8").read()) or ""
    for verb in ('"POST"', '"PUT"', '"DELETE"', "-X POST", "--request POST", "data-urlencode"):
        assert verb not in src, verb
    assert not any(n.upper() in ("POST", "PUT", "DELETE") for n in dir(bc))


def test_case_insensitive_forbidden_params():
    rec = Recorder()
    with pytest.raises(RequestNotPermitted):
        signed_get("/fapi/v1/commissionRate", {"SIDE": "BUY"}, CREDS, runner=rec)
    assert rec.configs == []


# --------------------------------------------------------------------------
# 資格情報を漏らさない
# --------------------------------------------------------------------------


def test_credentials_are_redacted_in_repr_and_str():
    assert "SECRET-xyz789" not in repr(CREDS)
    assert "KEY-abc123" not in repr(CREDS)
    assert "SECRET-xyz789" not in str(CREDS)
    assert "<redacted>" in repr(CREDS)


def test_api_key_is_not_placed_in_argv():
    """key は curl の stdin 設定で渡す。argv に載せない。"""
    rec = Recorder()
    signed_get("/fapi/v1/commissionRate", {"symbol": "BTCUSDT"}, CREDS, runner=rec)
    cfg = rec.configs[0]
    assert 'header = "X-MBX-APIKEY: KEY-abc123"' in cfg
    src = open(bc.__file__, encoding="utf-8").read()
    assert '"--config", "-"' in src, "curl 設定を stdin から渡していない"
    assert "X-MBX-APIKEY" not in src.split("def _run_curl")[1], "argv に header を積んでいる"


def test_secret_never_appears_in_the_request():
    rec = Recorder()
    signed_get("/fapi/v1/commissionRate", {"symbol": "BTCUSDT"}, CREDS, runner=rec)
    assert "SECRET-xyz789" not in rec.configs[0]


def test_record_contains_only_the_five_permitted_fields():
    record = parse_commission(BODY, "d" * 64, now="2026-08-18T00:00:00+00:00")
    assert set(record.to_dict()) == {
        "symbol", "maker_commission_rate", "taker_commission_rate",
        "response_sha256", "retrieved_at_utc",
    }
    dumped = json.dumps(record.to_dict())
    assert "KEY-" not in dumped and "SECRET-" not in dumped
    # **生の応答本文を保持していない**
    assert "makerCommissionRate" not in dumped


def test_missing_credentials_raise_without_echoing_anything():
    with pytest.raises(CredentialError) as exc:
        Credentials.from_env({})
    assert "BINANCE_API_KEY" in str(exc.value)
    assert "=" not in str(exc.value).split("環境変数")[0]


# --------------------------------------------------------------------------
# 署名と読み取り
# --------------------------------------------------------------------------


def test_signature_is_plain_hmac_sha256_of_the_query():
    query = "symbol=BTCUSDT&recvWindow=5000&timestamp=1700000000000"
    expected = hmac.new(b"SECRET-xyz789", query.encode(), hashlib.sha256).hexdigest()
    assert sign(query, "SECRET-xyz789") == expected


def test_hmac_sha256_primitive_matches_rfc4231_case2():
    """RFC 4231 test case 2。署名プリミティブそのものを固定する。"""
    got = hmac.new(b"Jefe", b"what do ya want for nothing?", hashlib.sha256).hexdigest()
    assert got == "5bdcc146bf60754e6a042426089575c75a003f089d2739839dec58b964ec3843"


def test_request_is_a_signed_get_with_recv_window_and_timestamp():
    rec = Recorder()
    signed_get("/fapi/v1/commissionRate", {"symbol": "BTCUSDT"}, CREDS, runner=rec)
    parsed = urlparse(rec.url)
    assert parsed.path == "/fapi/v1/commissionRate"
    q = parse_qs(parsed.query)
    assert q["symbol"] == ["BTCUSDT"]
    assert q["recvWindow"] == ["5000"]
    assert int(q["timestamp"][0]) > 1_600_000_000_000
    assert len(q["signature"][0]) == 64


def test_signature_covers_every_query_parameter():
    """署名対象が実際に送る query と一致していること。"""
    rec = Recorder()
    signed_get("/fapi/v1/commissionRate", {"symbol": "BTCUSDT"}, CREDS, runner=rec)
    query, _, sig = rec.url.split("?", 1)[1].rpartition("&signature=")
    assert sign(query, "SECRET-xyz789") == sig


def test_parse_reads_the_rates_and_hashes_the_body():
    record = parse_commission(BODY, "a" * 64, now="2026-08-18T00:00:00+00:00")
    assert record.symbol == "BTCUSDT"
    assert record.maker_commission_rate == 0.0002
    assert record.taker_commission_rate == 0.0005
    assert record.response_sha256 == "a" * 64


def test_binance_error_payload_is_not_parsed_as_a_rate():
    with pytest.raises(RuntimeError):
        parse_commission(json.dumps({"code": -2015, "msg": "Invalid API-key"}), "b" * 64)


def test_fetch_uses_exactly_one_request():
    rec = Recorder()
    record = fetch_commission_rate("BTCUSDT", CREDS, runner=rec)
    assert len(rec.configs) == 1
    assert isinstance(record, CommissionRecord)
    assert record.taker_commission_rate == 0.0005


def test_documented_four_bps_example_is_not_used_as_a_measured_value():
    """Binance ドキュメントの 4bps を実測値として埋め込んでいないこと。"""
    src = open(bc.__file__, encoding="utf-8").read()
    for literal in ("0.0004", "0.00040", "4e-4", "4.0"):
        assert literal not in src, literal

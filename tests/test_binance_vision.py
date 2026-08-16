"""Binance Vision downloader(ネットワークへは出ない。client を差し替える)。"""

import hashlib
import json
from pathlib import Path

import pytest

from mce import binance_vision as bv


class FakeClient:
    """path → (status, body) の辞書で応答する VisionClient 代替。"""

    def __init__(self, responses: dict[str, tuple[int, bytes]]):
        self.responses = responses
        self.requests: list[str] = []

    def get(self, path: str) -> tuple[int, bytes]:
        self.requests.append(path)
        return self.responses.get(path, (404, b""))


def _checksum_body(payload: bytes, filename: str) -> bytes:
    return f"{hashlib.sha256(payload).hexdigest()}  {filename}\n".encode()


def test_months_and_days_enumeration():
    assert bv.months("2020-11", "2021-02") == ["2020-11", "2020-12", "2021-01", "2021-02"]
    assert bv.months("2021-01", "2021-01") == ["2021-01"]
    d = bv.days("2020-01", "2020-02")
    assert d[0] == "2020-01-01" and d[-1] == "2020-02-29" and len(d) == 60  # 閏年


def test_parse_checksum_rejects_garbage():
    assert bv.parse_checksum("a" * 64 + "  file.zip") == "a" * 64
    with pytest.raises(bv.BinanceVisionError):
        bv.parse_checksum("not-a-hash file.zip")


def test_download_period_saves_and_verifies(tmp_path: Path):
    spec = bv.DATASETS["klines_5m"]
    rel = spec.relative_path("BTCUSDT", "2021-01")
    payload = b"zip-bytes"
    client = FakeClient(
        {
            rel: (200, payload),
            rel + ".CHECKSUM": (200, _checksum_body(payload, "BTCUSDT-5m-2021-01.zip")),
        }
    )
    record = bv.download_period(client, spec, "2021-01", out_dir=tmp_path)
    assert record["status"] == "saved"
    assert record["checksum_verified"] is True
    saved = tmp_path / "BTCUSDT-5m-2021-01.zip"
    assert saved.read_bytes() == payload
    assert (tmp_path / "BTCUSDT-5m-2021-01.zip.CHECKSUM").exists()
    assert not list(tmp_path.glob("*.partial"))

    # 2回目は再取得しない(冪等・immutable)
    again = bv.download_period(client, spec, "2021-01", out_dir=tmp_path)
    assert again["status"] == "cached"


def test_download_period_absent_is_not_an_error(tmp_path: Path):
    spec = bv.DATASETS["metrics_5m"]
    record = bv.download_period(FakeClient({}), spec, "2020-01-01", out_dir=tmp_path)
    assert record["status"] == "absent"
    assert record["http_status"] == 404
    assert not list(tmp_path.iterdir())


def test_download_period_checksum_mismatch_is_not_saved(tmp_path: Path):
    spec = bv.DATASETS["klines_5m"]
    rel = spec.relative_path("BTCUSDT", "2021-01")
    client = FakeClient(
        {
            rel: (200, b"actual"),
            rel + ".CHECKSUM": (200, _checksum_body(b"expected", "x.zip")),
        }
    )
    record = bv.download_period(client, spec, "2021-01", out_dir=tmp_path)
    assert record["status"] == "checksum_mismatch"
    assert not list(tmp_path.iterdir())


def test_existing_file_with_wrong_checksum_raises(tmp_path: Path):
    spec = bv.DATASETS["klines_5m"]
    (tmp_path / "BTCUSDT-5m-2021-01.zip").write_bytes(b"stale")
    rel = spec.relative_path("BTCUSDT", "2021-01")
    client = FakeClient({rel + ".CHECKSUM": (200, _checksum_body(b"fresh", "x.zip"))})
    with pytest.raises(bv.BinanceVisionError):
        bv.download_period(client, spec, "2021-01", out_dir=tmp_path)


def test_source_digest_is_environment_independent(tmp_path: Path):
    """公開 zip の sha256 由来なので、誰の環境で取っても同じ値になる。"""
    ledger = tmp_path / "ledger.jsonl"
    rows = [
        {"period": "2021-01-02", "status": "saved", "sha256": "b" * 64, "checksum_verified": True},
        {"period": "2021-01-01", "status": "saved", "sha256": "a" * 64, "checksum_verified": True},
        {"period": "2021-01-03", "status": "absent"},
    ]
    ledger.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    digest = bv.source_digest("metrics_5m", ledger=ledger)
    assert digest["periods_with_file"] == 2
    assert digest["periods_absent"] == 1
    assert digest["checksum_verified"] == 2

    # 記録順を入れ替えても、再実行で同じ period が追記されても値は変わらない
    shuffled = tmp_path / "shuffled.jsonl"
    shuffled.write_text(
        "\n".join(json.dumps(r) for r in [rows[1], rows[2], rows[0], rows[0]]) + "\n",
        encoding="utf-8",
    )
    assert bv.source_digest("metrics_5m", ledger=shuffled)["digest"] == digest["digest"]


def test_source_digest_absent_ledger(tmp_path: Path):
    assert bv.source_digest("metrics_5m", ledger=tmp_path / "none.jsonl")["present"] is False


def test_download_dataset_writes_ledger_and_counts(tmp_path: Path):
    spec = bv.DATASETS["metrics_5m"]
    payload = b"day"
    responses = {}
    for day in ("2021-01-01", "2021-01-02"):
        rel = spec.relative_path("BTCUSDT", day)
        responses[rel] = (200, payload)
        responses[rel + ".CHECKSUM"] = (200, _checksum_body(payload, "x.zip"))
    # 2021-01-03 は未公開(404)
    client = FakeClient(responses)
    ledger = tmp_path / "ledger.jsonl"
    result = bv.download_dataset(
        "metrics_5m",
        "2021-01",
        "2021-01",
        client=client,
        out_dir=tmp_path / "raw",
        ledger=ledger,
        verbose=False,
    )
    assert result["counts"]["saved"] == 2
    assert result["counts"]["absent"] == 29  # 1月は31日
    assert result["absent_periods"][0] == "2021-01-03"
    records = [json.loads(line) for line in ledger.read_text().splitlines()]
    assert len(records) == 31
    assert {r["status"] for r in records} == {"saved", "absent"}

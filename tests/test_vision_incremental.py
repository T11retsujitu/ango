"""Binance Vision の差分取得(watermark → 最後の閉じた period)。ネットワークへは出ない。"""

import hashlib
import json
from datetime import date
from pathlib import Path

import pytest

from mce import binance_vision as bv


class FakeClient:
    def __init__(self, responses: dict[str, tuple[int, bytes]]):
        self.responses = responses
        self.requests: list[str] = []

    def get(self, path: str) -> tuple[int, bytes]:
        self.requests.append(path)
        return self.responses.get(path, (404, b""))


class ExhaustedClient:
    """retry budget を使い切った VisionClient を模す。"""

    def get(self, path: str) -> tuple[int, bytes]:
        raise bv.BinanceVisionError(f"max retries exceeded: {path}")


def _responses(spec, periods, symbol="BTCUSDT") -> dict[str, tuple[int, bytes]]:
    out: dict[str, tuple[int, bytes]] = {}
    for period in periods:
        rel = spec.relative_path(symbol, period)
        payload = f"zip-{period}".encode()
        filename = rel.rsplit("/", 1)[-1]
        checksum = f"{hashlib.sha256(payload).hexdigest()}  {filename}\n".encode()
        out[rel] = (200, payload)
        out[rel + ".CHECKSUM"] = (200, checksum)
    return out


# --- 公開遅延と period 列挙 -------------------------------------------------


def test_latest_closed_period_avoids_the_open_day_and_month():
    monthly = bv.DATASETS["klines_5m"]
    daily = bv.DATASETS["metrics_5m"]
    today = date(2026, 8, 18)

    # 当月はまだ閉じていない。lag 2 日を引いた 08-16 の属する 8 月も未確定。
    assert bv.latest_closed_period(monthly, today, 2) == "2026-07"
    assert bv.latest_closed_period(daily, today, 2) == "2026-08-16"
    assert bv.latest_closed_period(daily, today, 0) == "2026-08-18"


def test_latest_closed_month_rolls_back_across_the_year_boundary():
    monthly = bv.DATASETS["klines_5m"]
    assert bv.latest_closed_period(monthly, date(2026, 1, 3), 2) == "2025-12"
    assert bv.latest_closed_period(monthly, date(2026, 1, 1), 2) == "2025-11"


def test_latest_closed_period_rejects_a_negative_lag():
    with pytest.raises(ValueError):
        bv.latest_closed_period(bv.DATASETS["klines_5m"], date(2026, 8, 18), -1)


def test_incremental_periods_start_after_the_watermark():
    monthly = bv.DATASETS["klines_5m"]
    assert bv.incremental_periods(monthly, watermark="2026-05", through="2026-07") == [
        "2026-06",
        "2026-07",
    ]
    assert bv.incremental_periods(monthly, watermark="2026-07", through="2026-07") == []
    assert bv.incremental_periods(monthly, watermark="2026-08", through="2026-07") == []

    daily = bv.DATASETS["metrics_5m"]
    assert bv.incremental_periods(daily, watermark="2026-08-14", through="2026-08-16") == [
        "2026-08-15",
        "2026-08-16",
    ]


def test_incremental_periods_without_watermark_start_at_the_default():
    monthly = bv.DATASETS["klines_5m"]
    assert bv.incremental_periods(
        monthly, watermark=None, through="2020-03", default_start="2020-01"
    ) == ["2020-01", "2020-02", "2020-03"]
    daily = bv.DATASETS["metrics_5m"]
    assert bv.incremental_periods(
        daily, watermark=None, through="2020-01-03", default_start="2020-01"
    ) == ["2020-01-01", "2020-01-02", "2020-01-03"]


# --- watermark --------------------------------------------------------------


def test_watermark_ignores_absent_and_mismatch_records(tmp_path: Path):
    ledger = tmp_path / "download_ledger.jsonl"
    rows = [
        {"period": "2026-05", "status": "saved", "sha256": "a" * 64},
        {"period": "2026-06", "status": "cached", "sha256": "b" * 64},
        # 公開が遅れているだけの period で watermark を進めてはいけない。
        {"period": "2026-07", "status": "absent"},
        {"period": "2026-08", "status": "checksum_mismatch"},
    ]
    ledger.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    assert bv.ledger_watermark("klines_5m", ledger=ledger) == "2026-06"


def test_watermark_is_none_without_a_ledger(tmp_path: Path):
    assert bv.ledger_watermark("klines_5m", ledger=tmp_path / "missing.jsonl") is None


# --- 差分同期 ---------------------------------------------------------------


def test_sync_incremental_fetches_only_the_new_closed_periods(tmp_path: Path):
    spec = bv.DATASETS["klines_5m"]
    ledger = tmp_path / "download_ledger.jsonl"
    ledger.write_text(
        json.dumps({"period": "2026-05", "status": "saved", "sha256": "a" * 64}) + "\n",
        encoding="utf-8",
    )
    client = FakeClient(_responses(spec, ["2026-06", "2026-07"]))

    result = bv.sync_incremental(
        "klines_5m",
        client=client,
        out_dir=tmp_path / "raw",
        ledger=ledger,
        today=date(2026, 8, 18),
        verbose=False,
    )

    assert result["watermark"] == "2026-05"
    assert result["through_latest_closed"] == "2026-07"
    assert result["requested_periods"] == ["2026-06", "2026-07"]
    assert result["counts"] == {"saved": 2}
    assert result["watermark_after"] == "2026-07"
    # 未確定の 2026-08 は取りに行かない。
    assert not any("2026-08" in path for path in client.requests)
    assert sorted(p.name for p in (tmp_path / "raw").glob("*.zip")) == [
        "BTCUSDT-5m-2026-06.zip",
        "BTCUSDT-5m-2026-07.zip",
    ]


def test_sync_incremental_is_idempotent_and_does_not_refetch(tmp_path: Path):
    spec = bv.DATASETS["klines_5m"]
    ledger = tmp_path / "download_ledger.jsonl"
    client = FakeClient(_responses(spec, bv.months("2026-06", "2026-07")))
    ledger.write_text(
        json.dumps({"period": "2026-05", "status": "saved", "sha256": "a" * 64}) + "\n",
        encoding="utf-8",
    )

    bv.sync_incremental(
        "klines_5m",
        client=client,
        out_dir=tmp_path / "raw",
        ledger=ledger,
        today=date(2026, 8, 18),
        verbose=False,
    )
    second = bv.sync_incremental(
        "klines_5m",
        client=client,
        out_dir=tmp_path / "raw",
        ledger=ledger,
        today=date(2026, 8, 18),
        verbose=False,
    )
    assert second["pending_periods"] == 0
    assert second["requested_periods"] == []


def test_absent_period_does_not_advance_the_watermark(tmp_path: Path):
    spec = bv.DATASETS["klines_5m"]
    ledger = tmp_path / "download_ledger.jsonl"
    ledger.write_text(
        json.dumps({"period": "2026-05", "status": "saved", "sha256": "a" * 64}) + "\n",
        encoding="utf-8",
    )
    # 2026-06 は未公開(404)、2026-07 だけ存在する。
    client = FakeClient(_responses(spec, ["2026-07"]))

    result = bv.sync_incremental(
        "klines_5m",
        client=client,
        out_dir=tmp_path / "raw",
        ledger=ledger,
        today=date(2026, 8, 18),
        verbose=False,
    )
    assert result["absent_periods"] == ["2026-06"]
    assert result["watermark_after"] == "2026-07"

    report = bv.availability_report("klines_5m", ledger=ledger)
    assert report["periods"]["2026-06"] == "absent"
    assert report["absent_periods"] == ["2026-06"]
    assert report["counts"]["absent"] == 1
    # 事前 seed した 2026-05 と、新たに取得した 2026-07。
    assert report["counts"]["saved"] == 2


def test_retry_budget_exhaustion_is_recorded_then_raised(tmp_path: Path):
    ledger = tmp_path / "download_ledger.jsonl"
    with pytest.raises(bv.BinanceVisionError):
        bv.download_dataset(
            "klines_5m",
            client=ExhaustedClient(),
            out_dir=tmp_path / "raw",
            ledger=ledger,
            verbose=False,
            periods=["2026-06"],
        )
    report = bv.availability_report("klines_5m", ledger=ledger)
    assert report["retryable_error_periods"] == ["2026-06"]
    # 「取りに行って失敗した」を absent と混ぜない。
    assert report["absent_periods"] == []
    assert report["watermark"] is None


def test_download_dataset_requires_a_period_range_or_explicit_list(tmp_path: Path):
    with pytest.raises(bv.BinanceVisionError):
        bv.download_dataset("klines_5m", ledger=tmp_path / "l.jsonl", verbose=False)


# --- source digest の窓 ------------------------------------------------------


def test_source_digest_window_is_stable_when_new_periods_arrive(tmp_path: Path):
    ledger = tmp_path / "download_ledger.jsonl"
    frozen_rows = [
        {"period": "2026-05", "status": "saved", "sha256": "a" * 64, "checksum_verified": True},
        {"period": "2026-06", "status": "saved", "sha256": "b" * 64, "checksum_verified": True},
    ]
    ledger.write_text("\n".join(json.dumps(row) for row in frozen_rows) + "\n", encoding="utf-8")
    frozen = bv.source_digest("klines_5m", ledger=ledger, through="2026-06")

    with ledger.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"period": "2026-07", "status": "saved", "sha256": "c" * 64}) + "\n")

    # 窓を明示すれば凍結時の digest は動かない。窓なしなら当然変わる。
    assert bv.source_digest("klines_5m", ledger=ledger, through="2026-06") == frozen
    assert bv.source_digest("klines_5m", ledger=ledger)["digest"] != frozen["digest"]
    assert bv.source_digest("klines_5m", ledger=ledger)["periods_with_file"] == 3


def test_source_digest_since_bound_selects_a_sub_window(tmp_path: Path):
    ledger = tmp_path / "download_ledger.jsonl"
    rows = [
        {"period": "2026-05", "status": "saved", "sha256": "a" * 64},
        {"period": "2026-06", "status": "saved", "sha256": "b" * 64},
    ]
    ledger.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    windowed = bv.source_digest("klines_5m", ledger=ledger, since="2026-06")
    assert windowed["periods_with_file"] == 1
    assert windowed["since"] == "2026-06"


def test_availability_report_on_an_empty_ledger(tmp_path: Path):
    report = bv.availability_report("klines_5m", ledger=tmp_path / "none.jsonl")
    assert report["counts"] == {}
    assert report["periods"] == {}
    assert report["watermark"] is None

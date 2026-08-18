"""P1 / P2 プローブの分類テスト。**ネットワークを使わない。**

最重要: **経路が塞がれていることを source の所見に化けさせない。**
"""

import json
from pathlib import Path

import polars as pl
import pytest

from mce import mark_gap_probe as mgp
from mce.mark_gap_probe import (
    CLASS_BLOCKED,
    CLASS_REPAIR,
    CLASS_UNOBSERVABLE,
    RestUnreachable,
    gap_targets,
    probe_interval,
    stale_targets,
)
from mce.normalize_binance import BAR_MS

REPO = Path(__file__).resolve().parents[1]
ARTIFACT = REPO / "experiments" / "phase8" / "mark_gap_probe_v1.json"

T0 = 1698796800000  # 2023-11-01T00:00:00Z


def _vision(times, ohlc=(1.0, 2.0, 0.5, 1.5)) -> dict:
    return {t: ohlc for t in times}


def _kline(t, o=1.0, h=2.0, low=0.5, c=1.5):
    return [t, f"{o}", f"{h}", f"{low}", f"{c}", "0", t + BAR_MS - 1, "0", 300, "0", "0", "0"]


def _fake_get(body: str):
    def inner(url, timeout=40):
        return body, "d" * 64
    return inner


# --------------------------------------------------------------------------
# 経路の遮断を source の所見にしない
# --------------------------------------------------------------------------


@pytest.mark.parametrize("body", [
    '{"code":0,"msg":"Service unavailable from a restricted location according to '
    "'b. Eligibility' in https://www.binance.com/en/terms.\"}",
    "<html><head><title>302 Found</title></head></html>",
])
def test_egress_block_is_not_classified_as_unobservable(monkeypatch, body):
    monkeypatch.setattr(mgp, "_get", _fake_get(body))
    res = probe_interval("gap", [T0], _vision([T0 - BAR_MS, T0 + BAR_MS]))
    assert res.classification == CLASS_BLOCKED
    assert res.classification != CLASS_UNOBSERVABLE
    assert res.http_note
    assert res.gap_bars_recovered == 0


def test_unreachable_rest_is_not_classified_as_unobservable(monkeypatch):
    def boom(url, timeout=40):
        raise RestUnreachable("空応答")
    monkeypatch.setattr(mgp, "_get", boom)
    res = probe_interval("gap", [T0], _vision([T0 - BAR_MS]))
    assert res.classification == CLASS_BLOCKED
    assert res.response_sha256 is None


def test_error_payload_from_the_exchange_is_blocked_not_unobservable(monkeypatch):
    monkeypatch.setattr(mgp, "_get", _fake_get('{"code":-1121,"msg":"Invalid symbol."}'))
    res = probe_interval("gap", [T0], _vision([T0 - BAR_MS]))
    assert res.classification == CLASS_BLOCKED


# --------------------------------------------------------------------------
# 実際に応答があったときの分類
# --------------------------------------------------------------------------


def test_full_recovery_with_exact_overlap_is_a_repair_candidate(monkeypatch):
    target = [T0]
    overlap = [T0 - BAR_MS, T0 + BAR_MS]
    body = json.dumps([_kline(t) for t in overlap + target])
    monkeypatch.setattr(mgp, "_get", _fake_get(body))
    res = probe_interval("gap", target, _vision(overlap))
    assert res.classification == CLASS_REPAIR
    assert res.gap_bars_recovered == 1
    assert res.overlap_rows_compared == 2 and res.overlap_rows_exact == 2
    assert res.overlap_max_abs_diff == 0.0
    assert res.response_sha256 == "d" * 64
    assert res.retrieved_at_utc


def test_partial_recovery_is_unobservable(monkeypatch):
    target = [T0, T0 + BAR_MS]
    overlap = [T0 - BAR_MS]
    body = json.dumps([_kline(t) for t in overlap + [T0]])  # 1本しか戻らない
    monkeypatch.setattr(mgp, "_get", _fake_get(body))
    res = probe_interval("gap", target, _vision(overlap))
    assert res.classification == CLASS_UNOBSERVABLE
    assert res.gap_bars_recovered == 1


def test_overlap_disagreement_is_unobservable(monkeypatch):
    target = [T0]
    overlap = [T0 - BAR_MS]
    body = json.dumps([_kline(T0 - BAR_MS, h=99.0), _kline(T0)])
    monkeypatch.setattr(mgp, "_get", _fake_get(body))
    res = probe_interval("gap", target, _vision(overlap))
    assert res.classification == CLASS_UNOBSERVABLE
    assert res.overlap_rows_compared == 1 and res.overlap_rows_exact == 0
    assert res.overlap_max_abs_diff == pytest.approx(97.0)


def test_recovery_without_any_overlap_control_is_not_a_repair_candidate(monkeypatch):
    """対照窓が1本も突き合わせられないなら、復元できても repair とはしない。"""
    body = json.dumps([_kline(T0)])
    monkeypatch.setattr(mgp, "_get", _fake_get(body))
    res = probe_interval("gap", [T0], _vision([T0 - BAR_MS]))
    assert res.gap_bars_recovered == 1
    assert res.overlap_rows_compared == 0
    assert res.classification == CLASS_UNOBSERVABLE


def test_requested_interval_includes_the_pre_and_post_control_window(monkeypatch):
    monkeypatch.setattr(mgp, "_get", _fake_get("[]"))
    res = probe_interval("gap", [T0], _vision([]), overlap_bars=3)
    assert res.requested_start_ms == T0 - 3 * BAR_MS
    assert res.requested_end_ms == T0 + 3 * BAR_MS


# --------------------------------------------------------------------------
# 対象の抽出
# --------------------------------------------------------------------------


def _frame(times, samples=None):
    samples = samples or [300] * len(times)
    return pl.DataFrame({"ts": times, "mark_samples": samples}).with_columns(
        pl.col("ts").cast(pl.Datetime(time_unit="ms", time_zone="UTC"))
    )


def test_gap_targets_enumerates_every_missing_bar():
    runs = gap_targets(_frame([T0, T0 + BAR_MS, T0 + 4 * BAR_MS]))
    assert runs == [[T0 + 2 * BAR_MS, T0 + 3 * BAR_MS]]


def test_stale_targets_groups_contiguous_zero_sample_bars():
    times = [T0 + i * BAR_MS for i in range(5)]
    runs = stale_targets(_frame(times, samples=[300, 0, 0, 300, 0]))
    assert runs == [[times[1], times[2]], [times[4]]]


# --------------------------------------------------------------------------
# 成果物
# --------------------------------------------------------------------------

artifact_only = pytest.mark.skipif(not ARTIFACT.exists(), reason="probe 未実行")


@artifact_only
def test_artifact_declares_it_did_not_merge_or_synthesize():
    report = json.loads(ARTIFACT.read_text())
    assert report["merged_into_canonical_dataset"] is False
    joined = " ".join(report["refusals"])
    assert "no interpolation" in joined
    assert "index/premium" in joined
    assert "carry-forward" in joined
    assert "no rho" in report["purpose"] and "no PnL" in report["purpose"]


@artifact_only
def test_artifact_covers_every_gap_and_every_stale_run():
    report = json.loads(ARTIFACT.read_text())
    kinds = [i["kind"] for i in report["intervals"]]
    assert kinds.count("gap") == 8       # inventory の gap_runs と一致
    assert kinds.count("stale") == 4     # 停止バーの連続塊
    for i in report["intervals"]:
        assert i["target_open_times"], i["requested_start_utc"]
        assert i["retrieved_at_utc"]
        assert i["requested_start_ms"] < min(i["target_open_times"])
        assert i["requested_end_ms"] > max(i["target_open_times"])


@artifact_only
def test_artifact_records_every_required_field():
    for i in json.loads(ARTIFACT.read_text())["intervals"]:
        for key in (
            "requested_start_utc", "requested_end_utc", "returned_open_times",
            "gap_bars_recovered", "overlap_rows_compared", "overlap_rows_exact",
            "overlap_max_abs_diff", "response_sha256", "retrieved_at_utc",
            "classification",
        ):
            assert key in i, key


@artifact_only
def test_artifact_does_not_claim_unobservability_while_egress_is_blocked():
    """遮断されている区間を source の欠測として記録していないこと。"""
    report = json.loads(ARTIFACT.read_text())
    for i in report["intervals"]:
        if i["http_note"] and "restriction" in i["http_note"]:
            assert i["classification"] == CLASS_BLOCKED, i["requested_start_utc"]

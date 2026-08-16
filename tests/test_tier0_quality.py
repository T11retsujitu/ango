"""Tier 0 品質レポート(ゲート判定・ラベル非参照の構造的保証)。"""

import ast
from datetime import datetime, timedelta, timezone
from pathlib import Path

import polars as pl

from mce import tier0_quality as tq

UTC = timezone.utc
START = datetime(2024, 1, 1, tzinfo=UTC)


def _ts_frame(offsets_minutes: list[int]) -> pl.DataFrame:
    return pl.DataFrame({"ts": [START + timedelta(minutes=m) for m in offsets_minutes]}).with_columns(
        pl.col("ts").dt.cast_time_unit("ms")
    )


def test_timestamp_report_counts_gap_and_missing():
    df = _ts_frame([0, 5, 10, 30])  # 15,20,25 が欠測
    report = tq.timestamp_report(df)
    assert report["rows"] == 4
    assert report["expected_rows_on_grid"] == 7
    assert report["missing_rows"] == 3
    assert report["gap_count"] == 1
    assert report["longest_gap_minutes"] == 20
    assert report["largest_gaps"][0]["gap_minutes"] == 20
    assert report["off_grid_rows"] == 0
    assert report["strictly_increasing"] is True
    assert report["sealed_rows_present"] == 0


def test_timestamp_report_detects_off_grid_and_sealed_rows():
    df = _ts_frame([0, 7])  # 7分後 = 5分グリッド外
    assert tq.timestamp_report(df)["off_grid_rows"] == 1
    sealed = pl.DataFrame(
        {"ts": [datetime(2025, 12, 31, 23, 55, tzinfo=UTC), datetime(2026, 1, 1, tzinfo=UTC)]}
    ).with_columns(pl.col("ts").dt.cast_time_unit("ms"))
    assert tq.timestamp_report(sealed)["sealed_rows_present"] == 1


def _klines(taker: float = 4.0, high: float = 101.0) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "ts": [START, START + timedelta(minutes=5)],
            "open": [100.0, 100.0],
            "high": [high, 101.0],
            "low": [99.0, 99.0],
            "close": [100.0, 100.0],
            "volume": [10.0, 10.0],
            "volume_quote": [1000.0, 1000.0],
            "trades": [5, 5],
            "taker_buy_volume": [taker, 4.0],
            "taker_buy_quote": [400.0, 400.0],
        }
    ).with_columns(pl.col("ts").dt.cast_time_unit("ms"))


def test_klines_value_report_flags_impossible_taker_volume():
    ok = tq.klines_value_report(_klines())
    assert ok["taker_buy_gt_volume"] == 0
    assert ok["taker_buy_ratio_median"] == 0.4
    broken = tq.klines_value_report(_klines(taker=99.0))
    assert broken["taker_buy_gt_volume"] == 1


def test_klines_value_report_flags_ohlc_inconsistency():
    broken = tq.klines_value_report(_klines(high=99.5))  # high < max(open, close)
    assert broken["high_lt_max_open_close"] == 1


def test_okx_consistency_report_on_aligned_frames():
    ts = [START + timedelta(minutes=5 * i) for i in range(4)]
    bn = pl.DataFrame(
        {"ts": ts, "close": [100.0, 101.0, 102.0, 103.0], "volume": [10.0] * 4}
    ).with_columns(pl.col("ts").dt.cast_time_unit("ms"))
    okx = pl.DataFrame(
        {"ts": ts[:3], "close": [100.05, 101.05, 102.05], "volume": [5.0] * 3}
    ).with_columns(pl.col("ts").dt.cast_time_unit("ms"))
    report = tq.okx_consistency_report(bn, okx)
    assert report["overlap_rows"] == 3
    assert report["binance_only_rows"] == 1
    assert report["okx_only_rows"] == 0
    assert report["close_abs_diff_bps_median"] < 10
    assert report["volume_ratio_median"] == 2.0


def test_gates_fail_when_sealed_rows_present():
    report = {
        "datasets": {
            "klines_5m": {
                "present": True,
                "timestamps": {
                    "off_grid_rows": 0,
                    "strictly_increasing": True,
                    "time_zone": "UTC",
                    "sealed_rows_present": 3,
                },
                "values": {
                    "null_counts": {},
                    "taker_buy_gt_volume": 0,
                    "taker_buy_quote_gt_volume_quote": 0,
                    "high_lt_max_open_close": 0,
                    "low_gt_min_open_close": 0,
                },
                "raw_accounting": {"conflicting_duplicates": 2, "unresolved_conflicts": 0},
            }
        }
    }
    gates = tq.evaluate_gates(report)
    assert gates["klines_5m:no_null_key_columns"] is True
    # 食い違う重複があっても、所有ファイル規則で解決できていれば通過する
    assert gates["klines_5m:no_unresolved_conflicts"] is True
    assert gates["klines_5m:no_sealed_rows"] is False
    assert gates["all_passed"] is False


def test_quality_module_never_touches_labels():
    """pre-registration 前に効果量を覗かないための構造的保証。

    docstring 中の言及ではなく、実際の import と API 呼び出しを検査する。
    """
    source = Path(tq.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
            imported.update(f"{node.module}.{a.name}" for a in node.names)
    assert not any("labels" in name for name in imported)
    calls = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert "labels_parquet" not in calls
    assert "fwd_return" not in source


def test_off_grid_gate_applies_to_bars_but_not_snapshots():
    """バーは grid 上必須。snapshot(metrics)は上流が数秒ずれることが実在する。"""
    base = {
        "present": True,
        "timestamps": {
            "off_grid_rows": 143,
            "strictly_increasing": True,
            "time_zone": "UTC",
            "sealed_rows_present": 0,
        },
        "values": {"null_counts": {}},
        "raw_accounting": {"unresolved_conflicts": 0},
    }
    gates = tq.evaluate_gates({"datasets": {"metrics_5m": base, "premium_index_5m": base}})
    assert "metrics_5m:on_5m_grid" not in gates
    assert gates["premium_index_5m:on_5m_grid"] is False


def test_metrics_join_gate_detects_off_grid_leak():
    report = {
        "datasets": {},
        "features": {
            "rows": 10,
            "forward_looking_columns": [],
            "tier0_coverage": {"open_interest": {"non_null": 8}},
            "metrics_joinable_rows": 8,
        },
    }
    assert tq.evaluate_gates(report)["features:metrics_join_is_exact"] is True
    report["features"]["tier0_coverage"]["open_interest"]["non_null"] = 9  # grid 外が紛れた
    assert tq.evaluate_gates(report)["features:metrics_join_is_exact"] is False

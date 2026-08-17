"""Binance Vision dump の正規化(CSV → 共通スキーマ)。"""

import zipfile
from datetime import datetime, timezone
from pathlib import Path

import polars as pl
import pytest

from mce import normalize_binance as nb

UTC = timezone.utc
BAR_MS = 5 * 60 * 1000
T0 = 1698796800000  # 2023-11-01T00:00:00Z

KLINE_HEADER = (
    "open_time,open,high,low,close,volume,close_time,quote_volume,count,"
    "taker_buy_volume,taker_buy_quote_volume,ignore"
)
METRIC_HEADER = (
    "create_time,symbol,sum_open_interest,sum_open_interest_value,"
    "count_toptrader_long_short_ratio,sum_toptrader_long_short_ratio,"
    "count_long_short_ratio,sum_taker_long_short_vol_ratio"
)


def kline_row(open_ms: int, close: float = 100.0, volume: float = 10.0, taker: float = 4.0) -> str:
    return (
        f"{open_ms},{close},{close + 1},{close - 1},{close},{volume},{open_ms + BAR_MS - 1},"
        f"{volume * close},{7},{taker},{taker * close},0"
    )


def metric_row(ts: str, oi: float = 1000.0) -> str:
    return f"{ts},BTCUSDT,{oi},{oi * 100},1.1,1.2,1.3,0.9"


def write_zip(path: Path, lines: list[str]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as z:
        z.writestr(path.stem + ".csv", "\n".join(lines) + "\n")
    return path


def test_read_zip_rows_handles_header_and_headerless(tmp_path: Path):
    with_header = write_zip(tmp_path / "a.zip", [KLINE_HEADER, kline_row(T0)])
    without = write_zip(tmp_path / "b.zip", [kline_row(T0)])
    assert len(nb.read_zip_rows(with_header, 12)) == 1
    assert len(nb.read_zip_rows(without, 12)) == 1


def test_read_zip_rows_does_not_eat_metrics_first_row(tmp_path: Path):
    """metrics は1列目がデータでも非数値(日時文字列)。header 名で判定する。"""
    headerless = write_zip(tmp_path / "m1.zip", [metric_row("2023-11-01 00:00:00")])
    assert len(nb.read_zip_rows(headerless, 8)) == 1
    with_header = write_zip(tmp_path / "m2.zip", [METRIC_HEADER, metric_row("2023-11-01 00:00:00")])
    assert len(nb.read_zip_rows(with_header, 8)) == 1


def test_read_zip_rows_rejects_wrong_column_count(tmp_path: Path):
    bad = write_zip(tmp_path / "c.zip", ["1,2,3"])
    with pytest.raises(nb.BinanceNormalizationError):
        nb.read_zip_rows(bad, 12)


def test_epoch_ms_accepts_microsecond_dumps():
    assert nb._epoch_ms(str(T0)) == T0
    assert nb._epoch_ms(str(T0 * 1000)) == T0  # µs 版
    assert nb._epoch_ms(str(T0 * 1000 + 999)) == T0  # close_time の µs 版


def test_normalize_klines_keeps_flow_columns_and_units():
    df = nb.normalize_klines([kline_row(T0).split(",")])
    row = df.row(0, named=True)
    assert row["ts"] == datetime(2023, 11, 1, tzinfo=UTC)
    assert row["taker_buy_volume"] == 4.0 and row["volume"] == 10.0
    assert row["trades"] == 7
    assert row["source"] == "binance" and row["market_type"] == "perp_linear"


def test_normalize_klines_rejects_bad_close_time():
    bad = kline_row(T0).split(",")
    bad[6] = str(T0 + BAR_MS)  # +5m-1ms でない
    with pytest.raises(nb.BinanceNormalizationError):
        nb.normalize_klines([bad])


def test_normalize_metrics_parses_utc_and_checks_symbol():
    df = nb.normalize_metrics([metric_row("2023-11-01 00:05:00").split(",")])
    assert df.row(0, named=True)["ts"] == datetime(2023, 11, 1, 0, 5, tzinfo=UTC)
    assert df.schema["ts"].time_zone == "UTC"
    with pytest.raises(nb.BinanceNormalizationError):
        nb.normalize_metrics([metric_row("2023-11-01 00:05:00").replace("BTCUSDT", "ETHUSDT").split(",")])


def test_normalize_premium_index_drops_zero_volume_columns():
    df = nb.normalize_premium_index([kline_row(T0, close=0.0003).split(",")])
    assert set(df.columns) >= {"premium_open", "premium_close", "premium_samples"}
    assert "volume" not in df.columns and "taker_buy_volume" not in df.columns


def test_screening_cutoff_drops_sealed_rows():
    df = nb.normalize_klines(
        [
            kline_row(int(datetime(2025, 12, 31, 23, 55, tzinfo=UTC).timestamp() * 1000)).split(","),
            kline_row(int(datetime(2026, 1, 1, tzinfo=UTC).timestamp() * 1000)).split(","),
        ]
    )
    kept, dropped = nb.apply_screening_cutoff(df)
    assert kept.height == 1 and dropped == 1
    assert kept["ts"].max() < datetime(2026, 1, 1, tzinfo=UTC)


def test_normalize_dataset_dedupes_and_is_idempotent(tmp_path: Path):
    raw = tmp_path / "raw"
    # 2020年の metrics dump のように、完全同一行が2回入っているファイル
    write_zip(
        raw / "BTCUSDT-metrics-2023-11-01.zip",
        [METRIC_HEADER, metric_row("2023-11-01 00:00:00"), metric_row("2023-11-01 00:00:00"), metric_row("2023-11-01 00:05:00")],
    )
    out = tmp_path / "metrics.parquet"
    first = nb.normalize_dataset("metrics_5m", source_dir=raw, out_path=out)
    assert first["raw_rows"] == 3
    assert first["rows"] == 2
    assert first["duplicate_rows_dropped"] == 1
    assert first["conflicting_duplicates"] == 0

    second = nb.normalize_dataset("metrics_5m", source_dir=raw, out_path=out)
    assert second["rows"] == 2 and second["rows_added"] == 0  # 冪等


def test_normalize_dataset_reports_conflicting_duplicates(tmp_path: Path):
    raw = tmp_path / "raw"
    write_zip(
        raw / "BTCUSDT-metrics-2023-11-01.zip",
        [METRIC_HEADER, metric_row("2023-11-01 00:00:00", oi=1000.0), metric_row("2023-11-01 00:00:00", oi=2000.0)],
    )
    result = nb.normalize_dataset("metrics_5m", source_dir=raw, out_path=tmp_path / "m.parquet")
    assert result["conflicting_duplicates"] == 1  # 同一 ts で値が違う
    # 同一ファイル内の食い違いは所有ファイル規則で決められない → 未解決として報告
    assert result["conflicts_resolved_by_owning_file"] == 0
    assert result["unresolved_conflicts"] == 1


def test_boundary_conflict_is_resolved_by_owning_file(tmp_path: Path):
    """00:00 の行が前日ファイルにも入っている実データの形。日付が一致する側を採る。"""
    raw = tmp_path / "raw"
    write_zip(
        raw / "BTCUSDT-metrics-2023-10-31.zip",
        [METRIC_HEADER, metric_row("2023-10-31 23:55:00"), metric_row("2023-11-01 00:00:00", oi=2000.0)],
    )
    write_zip(
        raw / "BTCUSDT-metrics-2023-11-01.zip",
        [METRIC_HEADER, metric_row("2023-11-01 00:00:00", oi=1000.0)],
    )
    out = tmp_path / "m.parquet"
    result = nb.normalize_dataset("metrics_5m", source_dir=raw, out_path=out)
    assert result["conflicting_duplicates"] == 1
    assert result["conflicts_resolved_by_owning_file"] == 1
    assert result["unresolved_conflicts"] == 0
    kept = pl.read_parquet(out).filter(pl.col("ts") == datetime(2023, 11, 1, tzinfo=UTC))
    assert kept.row(0, named=True)["open_interest"] == 1000.0  # 11-01 ファイルの値


def test_scan_dataset_does_not_write(tmp_path: Path):
    raw = tmp_path / "raw"
    write_zip(raw / "BTCUSDT-5m-2023-11.zip", [KLINE_HEADER, kline_row(T0)])
    out = tmp_path / "klines.parquet"
    df, accounting = nb.scan_dataset("klines_5m", source_dir=raw)
    assert isinstance(df, pl.DataFrame) and df.height == 1
    assert accounting["files"] == 1 and accounting["raw_rows"] == 1
    assert not out.exists()

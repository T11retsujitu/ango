"""Phase 8 の入力データ配管(F1: mark price / F2: spot klines)の適合テスト。

**取引系列を組み立てない。** rho / シグナル / 清算発生 / return / PnL を
一切計算しない。ここで検査するのは取り込みの正しさだけである。
"""

import hashlib
import json
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import polars as pl
import pytest

from mce import config, normalize_binance as nb
from mce.binance_vision import (
    DATASETS,
    MARKET_PATH,
    MARKET_TYPE,
    SOURCE,
    SPOT_MARKET_PATH,
    SPOT_MARKET_TYPE,
    ledger_path,
    raw_dir,
    source_digest,
)
from mce.backtest.splits import FINAL_OOS_START

UTC = timezone.utc
BAR_MS = 5 * 60 * 1000
T0 = 1698796800000  # 2023-11-01T00:00:00Z
REPO = Path(__file__).resolve().parents[1]

PHASE7_DATASETS = ("klines_5m", "premium_index_5m", "metrics_5m")
PHASE8_DATASETS = ("mark_price_5m", "spot_klines_5m")


# --------------------------------------------------------------------------
# Phase 7 の不変条件 — 追加が既存を動かしていないこと
# --------------------------------------------------------------------------


def test_phase7_dataset_specs_are_byte_identical():
    """Phase 7 の3系列の path / cadence / market_type を1文字も変えていない。"""
    frozen = {
        "klines_5m": (
            "monthly",
            "data/futures/um/monthly/klines/{sym}/5m/{sym}-5m-{period}.zip",
            "perp_linear",
        ),
        "premium_index_5m": (
            "monthly",
            "data/futures/um/monthly/premiumIndexKlines/{sym}/5m/{sym}-5m-{period}.zip",
            "perp_linear",
        ),
        "metrics_5m": (
            "daily",
            "data/futures/um/daily/metrics/{sym}/{sym}-metrics-{period}.zip",
            "perp_linear",
        ),
    }
    for name, (cadence, template, market_type) in frozen.items():
        spec = DATASETS[name]
        assert spec.cadence == cadence, name
        assert spec.path_template == template, name
        assert spec.market_type == market_type, name


def test_phase7_normalized_paths_are_unchanged():
    assert config.binance_klines_parquet().name == "klines_BTCUSDT_5m.parquet"
    assert config.binance_premium_index_parquet().name == "premium_index_BTCUSDT_5m.parquet"
    assert config.binance_metrics_parquet().name == "metrics_BTCUSDT_5m.parquet"


def test_new_datasets_do_not_share_raw_dirs_or_ledgers():
    """dataset ごとに raw も ledger も分かれること(digest が混ざらない)。"""
    dirs = {d: raw_dir(d).as_posix() for d in PHASE7_DATASETS + PHASE8_DATASETS}
    assert len(set(dirs.values())) == len(dirs), dirs
    ledgers = {d: ledger_path(d).as_posix() for d in PHASE7_DATASETS + PHASE8_DATASETS}
    assert len(set(ledgers.values())) == len(ledgers), ledgers


def test_source_digest_is_per_dataset(tmp_path: Path):
    """ある dataset の ledger を足しても、別 dataset の digest は変わらない。"""
    a = tmp_path / "a.jsonl"
    a.write_text(json.dumps({"period": "2020-01", "sha256": "aa", "status": "saved"}) + "\n")
    before = source_digest("klines_5m", ledger=a)["digest"]
    b = tmp_path / "b.jsonl"
    b.write_text(json.dumps({"period": "2020-01", "sha256": "bb", "status": "saved"}) + "\n")
    source_digest("mark_price_5m", ledger=b)
    assert source_digest("klines_5m", ledger=a)["digest"] == before
    body = "2020-01 aa"
    assert before == hashlib.sha256(body.encode()).hexdigest()


# --------------------------------------------------------------------------
# F1 / F2 の登録
# --------------------------------------------------------------------------


def test_f1_mark_price_is_registered_under_the_futures_market():
    spec = DATASETS["mark_price_5m"]
    assert spec.cadence == "monthly"
    assert spec.market_type == MARKET_TYPE == "perp_linear"
    rel = spec.relative_path("BTCUSDT", "2021-06")
    assert rel.startswith(MARKET_PATH + "/")
    assert "markPriceKlines" in rel
    assert rel.endswith("/BTCUSDT/5m/BTCUSDT-5m-2021-06.zip")


def test_f2_spot_klines_is_registered_under_the_spot_market():
    spec = DATASETS["spot_klines_5m"]
    assert spec.cadence == "monthly"
    assert spec.market_type == SPOT_MARKET_TYPE == "spot"
    rel = spec.relative_path("BTCUSDT", "2021-06")
    assert rel.startswith(SPOT_MARKET_PATH + "/")
    assert MARKET_PATH not in rel, "spot なのに futures の path を指している"
    assert rel.endswith("/monthly/klines/BTCUSDT/5m/BTCUSDT-5m-2021-06.zip")


def test_phase8_normalized_outputs_are_separate_files():
    paths = {
        config.binance_klines_parquet().name,
        config.binance_mark_price_parquet().name,
        config.binance_spot_klines_parquet().name,
    }
    assert len(paths) == 3, paths
    assert config.binance_mark_price_parquet().name == "mark_price_BTCUSDT_5m.parquet"
    assert config.binance_spot_klines_parquet().name == "spot_klines_BTCUSDT_5m.parquet"


# --------------------------------------------------------------------------
# 合成 dump による正規化
# --------------------------------------------------------------------------

MARK_HEADER = (
    "open_time,open,high,low,close,volume,close_time,quote_volume,count,"
    "taker_buy_volume,taker_buy_quote_volume,ignore"
)


def mark_row(open_ms: int, close: float = 100.0, samples: int = 300, volume: float = 0.0) -> str:
    return (
        f"{open_ms},{close},{close + 2},{close - 3},{close},{volume},"
        f"{open_ms + BAR_MS - 1},0,{samples},0,0,0"
    )


def spot_row(open_ms: int, close: float = 100.0, volume: float = 5.0) -> str:
    return (
        f"{open_ms},{close},{close + 1},{close - 1},{close},{volume},{open_ms + BAR_MS - 1},"
        f"{volume * close},9,{volume / 2},{volume / 2 * close},0"
    )


def write_zip(path: Path, lines: list[str]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as z:
        z.writestr(path.stem + ".csv", "\n".join(lines) + "\n")
    return path


def test_mark_price_columns_cannot_be_confused_with_traded_prices():
    """**mark と約定価格を取り違えられない列名**であること。"""
    df = nb.normalize_mark_price([mark_row(T0).split(",")])
    assert set(df.columns) == {
        "ts", "mark_open", "mark_high", "mark_low", "mark_close", "mark_samples",
        "symbol", "source", "market_type",
    }
    for traded in ("open", "high", "low", "close", "volume", "trades"):
        assert traded not in df.columns, traded


def test_mark_price_counts_stale_bars_without_dropping_them():
    """サンプル数 0 のバーは落とさず、`mark_stale_bars` として数える。"""
    stats: dict = {}
    rows = [mark_row(T0, samples=300).split(","), mark_row(T0 + BAR_MS, samples=0).split(",")]
    df = nb.normalize_mark_price(rows, stats=stats)
    assert df.height == 2, "停止バーを落としている"
    assert stats["mark_stale_bars"] == 1


def test_mark_price_values_and_samples():
    df = nb.normalize_mark_price([mark_row(T0, close=27000.0, samples=300).split(",")])
    row = df.row(0, named=True)
    assert row["mark_open"] == 27000.0
    assert row["mark_high"] == 27002.0
    assert row["mark_low"] == 26997.0
    assert row["mark_close"] == 27000.0
    assert row["mark_samples"] == 300
    assert row["market_type"] == "perp_linear"
    assert row["source"] == SOURCE


def test_mark_price_rejects_nonzero_traded_volume():
    """mark は板の約定ではない。volume が 0 でなければ取り込まない。"""
    with pytest.raises(nb.BinanceNormalizationError):
        nb.normalize_mark_price([mark_row(T0, volume=1.5).split(",")])


def test_mark_price_rejects_bad_close_time():
    bad = mark_row(T0).split(",")
    bad[6] = str(T0 + BAR_MS)  # +5m-1ms でない
    with pytest.raises(nb.BinanceNormalizationError):
        nb.normalize_mark_price([bad])


def test_mark_price_accepts_microsecond_timestamps():
    """2025 年以降の µs 版でも ms へ揃うこと。"""
    us = mark_row(T0).split(",")
    us[0] = str(T0 * 1000)
    us[6] = str((T0 + BAR_MS - 1) * 1000 + 999)
    df = nb.normalize_mark_price([us])
    assert df.row(0, named=True)["ts"] == datetime(2023, 11, 1, tzinfo=UTC)


def test_spot_klines_carry_the_spot_market_type():
    df = nb.normalize_klines([spot_row(T0).split(",")], "BTCUSDT", SPOT_MARKET_TYPE)
    row = df.row(0, named=True)
    assert row["market_type"] == "spot"
    assert row["source"] == "binance"
    assert row["volume"] == 5.0 and row["trades"] == 9


def test_perp_klines_still_default_to_perp_linear():
    df = nb.normalize_klines([spot_row(T0).split(",")])
    assert df.row(0, named=True)["market_type"] == "perp_linear"


# --------------------------------------------------------------------------
# scan_dataset — 時刻規約・重複/欠測分類・封印継承
# --------------------------------------------------------------------------


def _scan(tmp_path: Path, dataset: str, files: dict[str, list[str]]):
    src = tmp_path / dataset
    for period, lines in files.items():
        write_zip(src / f"BTCUSDT-5m-{period}.zip", lines)
    return nb.scan_dataset(dataset, "BTCUSDT", source_dir=src)


def test_scan_reports_close_time_policy_per_dataset(tmp_path: Path):
    """`close_time` の意味論は dump ごとに違う。policy を会計へ出す。"""
    _, mark_acc = _scan(tmp_path, "mark_price_5m", {"2023-11": [mark_row(T0)]})
    _, spot_acc = _scan(tmp_path, "spot_klines_5m", {"2023-11": [spot_row(T0)]})
    assert mark_acc["close_time_policy"] == "exact"
    assert spot_acc["close_time_policy"] == "last_trade_time"
    assert DATASETS["klines_5m"].close_time_policy == "exact"
    assert DATASETS["premium_index_5m"].close_time_policy == "exact"


def test_exact_policy_still_rejects_a_wrong_close_time():
    """Phase 7 と F1 の挙動は変えていない。"""
    bad = mark_row(T0).split(",")
    bad[6] = str(T0 + 1000)
    with pytest.raises(nb.BinanceNormalizationError):
        nb.normalize_mark_price([bad])


def test_spot_policy_classifies_close_time_deviations_without_dropping():
    """spot の close_time は**最終約定時刻**。逸脱は落とさず分類して数える。"""
    stats: dict = {}
    short = spot_row(T0).split(",")
    short[6] = str(T0 + 32_286)                    # バー内で早い(実データにある形)
    before = spot_row(T0 + BAR_MS).split(",")
    before[6] = str(T0 + BAR_MS - 1_059_479)       # 空バーで open より前(実データにある形)
    normal = spot_row(T0 + 2 * BAR_MS).split(",")
    df = nb.normalize_klines(
        [short, before, normal], "BTCUSDT", SPOT_MARKET_TYPE,
        close_time_policy="last_trade_time", stats=stats,
    )
    assert df.height == 3, "逸脱行を落としている"
    assert stats["close_time_not_bar_end_rows"] == 2
    assert stats["close_time_before_open_rows"] == 1


def test_spot_policy_still_requires_the_five_minute_grid():
    """close_time を緩めた代わりに、open_time のグリッドを不変条件にする。"""
    off = spot_row(T0 + 137).split(",")
    with pytest.raises(nb.BinanceNormalizationError):
        nb.normalize_klines(
            [off], "BTCUSDT", SPOT_MARKET_TYPE, close_time_policy="last_trade_time"
        )


def test_unknown_close_time_policy_is_refused():
    with pytest.raises(nb.BinanceNormalizationError):
        nb.normalize_klines([spot_row(T0).split(",")], close_time_policy="whatever")


def test_scan_reports_market_type_and_timestamp_semantics(tmp_path: Path):
    df, acc = _scan(tmp_path, "mark_price_5m",
                    {"2023-11": [MARK_HEADER, mark_row(T0), mark_row(T0 + BAR_MS)]})
    assert acc["market_type"] == "perp_linear"
    assert acc["dataset"] == "mark_price_5m"
    assert acc["raw_rows"] == 2 and df.height == 2
    # open_time はバー開始時刻。行 t は [ts, ts+5m)
    assert df["ts"].to_list() == [
        datetime(2023, 11, 1, tzinfo=UTC), datetime(2023, 11, 1, 0, 5, tzinfo=UTC)
    ]


def test_scan_spot_reports_spot_market_type(tmp_path: Path):
    _, acc = _scan(tmp_path, "spot_klines_5m", {"2023-11": [spot_row(T0)]})
    assert acc["market_type"] == "spot"


def test_scan_classifies_exact_duplicates(tmp_path: Path):
    """同一 ts・同一値の完全重複は落とし、件数を報告する。"""
    df, acc = _scan(tmp_path, "mark_price_5m",
                    {"2023-11": [mark_row(T0), mark_row(T0)]})
    assert df.height == 1
    assert acc["duplicate_rows_dropped"] == 1
    assert acc["conflicting_duplicates"] == 0
    assert acc["unresolved_conflicts"] == 0


def test_scan_resolves_conflicting_duplicates_by_owning_file(tmp_path: Path):
    """同一 ts で値が違う重複は、その ts の期間を持つファイルを所有者にする。"""
    t_dec = 1701388800000  # 2023-12-01T00:00:00Z
    df, acc = _scan(tmp_path, "mark_price_5m", {
        "2023-11": [mark_row(t_dec, close=1.0)],   # 11月ファイルに紛れ込んだ12月の行
        "2023-12": [mark_row(t_dec, close=2.0)],   # 所有者
    })
    assert acc["conflicting_duplicates"] == 1
    assert acc["conflicts_resolved_by_owning_file"] == 1
    assert acc["unresolved_conflicts"] == 0
    assert df.height == 1
    assert df.row(0, named=True)["mark_close"] == 2.0


def test_scan_inherits_the_phase7_seal_cutoff(tmp_path: Path):
    """ts >= FINAL_OOS_START の行を落とし、落とした数を記録する。"""
    sealed = int(FINAL_OOS_START.timestamp() * 1000)
    df, acc = _scan(tmp_path, "spot_klines_5m", {
        "2025-12": [spot_row(sealed - BAR_MS)],
        "2026-01": [spot_row(sealed), spot_row(sealed + BAR_MS)],
    })
    assert acc["sealed_rows_dropped"] == 2
    assert acc["cutoff"] == FINAL_OOS_START.isoformat()
    assert df.height == 1


def test_scan_reports_missing_files_as_zero_rows(tmp_path: Path):
    """ファイルが無い期間は行 0。**補間しない。**"""
    df, acc = _scan(tmp_path, "spot_klines_5m", {})
    assert acc["files"] == 0 and acc["raw_rows"] == 0
    assert df.is_empty()


def test_scan_rejects_wrong_column_count(tmp_path: Path):
    with pytest.raises(nb.BinanceNormalizationError):
        _scan(tmp_path, "spot_klines_5m", {"2023-11": ["1,2,3"]})


# --------------------------------------------------------------------------
# 実データの検査(存在すれば)
# --------------------------------------------------------------------------

MARK = REPO / config.binance_mark_price_parquet()
SPOT = REPO / config.binance_spot_klines_parquet()
PERP = REPO / config.binance_klines_parquet()

mark_only = pytest.mark.skipif(not MARK.exists(), reason="mark price 未取得")
spot_only = pytest.mark.skipif(not SPOT.exists(), reason="spot klines 未取得")


@mark_only
def test_real_mark_price_is_perp_linear_and_has_no_traded_columns():
    df = pl.read_parquet(MARK)
    assert df["market_type"].unique().to_list() == ["perp_linear"]
    assert df["source"].unique().to_list() == ["binance"]
    assert "mark_high" in df.columns and "high" not in df.columns
    # mark_samples == 0 は**停止したバー**(前値の横引き)。実在するので落とさない。
    # 捏造ではないが品質が違うため、少数であることと計数されることを固定する。
    assert df["mark_samples"].max() == 300
    stale = df.filter(pl.col("mark_samples") == 0).height
    assert 0 < stale < df.height * 0.001, stale


@spot_only
def test_real_spot_klines_are_spot_and_keep_traded_columns():
    df = pl.read_parquet(SPOT)
    assert df["market_type"].unique().to_list() == ["spot"]
    assert {"open", "high", "low", "close", "volume", "trades"} <= set(df.columns)
    assert df["volume"].sum() > 0, "spot なのに出来高が全く無い"


@mark_only
@spot_only
def test_real_mark_and_spot_are_distinct_series():
    """mark と spot を取り違えていないこと(同じ ts で値が一致しない)。"""
    mark = pl.read_parquet(MARK).select(["ts", "mark_close"])
    spot = pl.read_parquet(SPOT).select(["ts", "close"])
    joined = mark.join(spot, on="ts", how="inner")
    assert joined.height > 10_000
    same = joined.filter(pl.col("mark_close") == pl.col("close")).height
    assert same / joined.height < 0.05, "mark と spot がほぼ同一 — 取り違えの疑い"


@mark_only
@spot_only
def test_real_series_respect_the_seal_and_the_bar_grid():
    for path, col in ((MARK, "mark_close"), (SPOT, "close")):
        df = pl.read_parquet(path).sort("ts")
        assert df["ts"].max() < FINAL_OOS_START, path.name
        assert df["ts"].is_duplicated().sum() == 0, path.name
        deltas = df["ts"].diff().drop_nulls().dt.total_milliseconds().unique().to_list()
        assert min(d for d in deltas) == BAR_MS, path.name
        assert df[col].min() > 0, path.name


# --------------------------------------------------------------------------
# inventory(出所・digest・時刻規約・重複/欠測分類)
# --------------------------------------------------------------------------

from mce.phase8_inputs import PHASE8_INPUTS, gap_report, inventory  # noqa: E402

INVENTORY = REPO / "data" / "manifests" / "phase8_inputs_v1.json"
inventory_only = pytest.mark.skipif(not INVENTORY.exists(), reason="inventory 未生成")


def test_phase8_inputs_registry_covers_exactly_f1_and_f2():
    assert set(PHASE8_INPUTS) == {"mark_price_5m", "spot_klines_5m"}


def test_gap_report_counts_missing_bars_without_filling_them():
    ts = [datetime(2024, 1, 1, 0, 0, tzinfo=UTC),
          datetime(2024, 1, 1, 0, 5, tzinfo=UTC),
          datetime(2024, 1, 1, 0, 25, tzinfo=UTC)]  # 3本欠測
    rep = gap_report(pl.DataFrame({"ts": ts}))
    assert rep["bars"] == 3 and rep["expected_bars"] == 6
    assert rep["missing_bars"] == 3
    assert rep["gap_runs"] == 1 and rep["largest_gap_bars"] == 3
    assert rep["gap_runs_detail"][0]["after_utc"].startswith("2024-01-01T00:05")


def test_gap_report_is_empty_for_a_contiguous_series():
    ts = [datetime(2024, 1, 1, 0, m, tzinfo=UTC) for m in (0, 5, 10)]
    rep = gap_report(pl.DataFrame({"ts": ts}))
    assert rep["missing_bars"] == 0 and rep["gap_runs"] == 0


@inventory_only
def test_inventory_records_provenance_and_dataset_specific_digest():
    report = json.loads(INVENTORY.read_text())
    by_name = {d["dataset"]: d for d in report["datasets"]}
    assert set(by_name) == {"mark_price_5m", "spot_klines_5m"}
    digests = set()
    for name, d in by_name.items():
        assert d["source"] == "binance"
        assert d["base_url"] == "https://data.binance.vision"
        assert d["source_digest"]["present"] is True
        assert d["source_digest"]["periods_with_file"] == 72
        assert d["source_digest"]["periods_absent"] == 0
        # 公開 CHECKSUM で全期間を検証していること
        assert d["source_digest"]["checksum_verified"] == 72, name
        digests.add(d["source_digest"]["digest"])
    assert len(digests) == 2, "dataset 固有の digest が同一になっている"


@inventory_only
def test_inventory_records_timestamp_semantics_and_the_seal():
    for d in json.loads(INVENTORY.read_text())["datasets"]:
        sem = d["timestamp_semantics"]
        assert sem["field"] == "open_time"
        assert sem["grid_ms"] == BAR_MS
        assert sem["timezone"] == "UTC"
        assert sem["seal_cutoff"] == FINAL_OOS_START.isoformat()


@inventory_only
def test_inventory_classifies_duplicates_and_missing_bars():
    by_name = {d["dataset"]: d for d in json.loads(INVENTORY.read_text())["datasets"]}
    for name, d in by_name.items():
        raw = d["raw_accounting"]
        assert raw["files"] == 72, name
        assert raw["unresolved_conflicts"] == 0, name
        assert raw["sealed_rows_dropped"] == 0, name  # dump は 2025-12 まで
        gaps = d["gaps"]
        # 欠測は全件を列挙してあること(要約で丸めない)
        assert len(gaps["gap_runs_detail"]) == gaps["gap_runs"]
        assert sum(r["missing_bars"] for r in gaps["gap_runs_detail"]) == gaps["missing_bars"]
    # F1 は日単位の dump 欠落を含む。F2 は取引停止由来の短いギャップのみ。
    assert by_name["mark_price_5m"]["gaps"]["largest_gap_bars"] >= 288
    assert by_name["spot_klines_5m"]["gaps"]["largest_gap_bars"] < 288


@inventory_only
def test_inventory_records_the_dataset_specific_quality_flags():
    by_name = {d["dataset"]: d for d in json.loads(INVENTORY.read_text())["datasets"]}
    mark = by_name["mark_price_5m"]["raw_accounting"]
    spot = by_name["spot_klines_5m"]["raw_accounting"]
    assert mark["mark_stale_bars"] > 0  # 前値横引きのバーは実在する
    assert "close_time_not_bar_end_rows" not in mark  # exact policy なので発生しない
    assert spot["close_time_not_bar_end_rows"] > 0
    assert spot["close_time_before_open_rows"] > 0
    assert "mark_stale_bars" not in spot


@inventory_only
def test_inventory_is_input_plumbing_only():
    """取引系列の成果物を含まないこと(構造で検査する)。"""
    report = json.loads(INVENTORY.read_text())
    assert set(report) == {"inventory", "purpose", "built_at_utc", "datasets"}
    assert "no rho" in report["purpose"] and "no PnL" in report["purpose"]
    allowed = {
        "dataset", "symbol", "source", "base_url", "market_type", "cadence",
        "path_template", "close_time_policy", "ledger", "source_digest",
        "timestamp_semantics", "normalized_path", "normalized_present",
        "parquet", "gaps", "raw_accounting",
    }
    for d in report["datasets"]:
        assert set(d) <= allowed, set(d) - allowed
    # parquet の列は入力の列だけ(損益・シグナル列が混ざっていない)
    for d in report["datasets"]:
        for col in d["parquet"]["columns"]:
            assert not any(
                bad in col for bad in ("pnl", "signal", "rho", "return", "fill")
            ), col

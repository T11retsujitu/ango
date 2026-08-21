"""Phase 8 の入力データ配管(F5: index price klines = protocol §4.1 の `IDX`)。

**取引系列を組み立てない。** rho / シグナル / 清算発生 / return / PnL を
一切計算しない。ここで検査するのは取り込みの正しさだけである。

合成 dump は**実測した schema** をそのまま使う(2020-01 / 2025-12 を probe して
確認した: 12 列の kline 形式、`close_time == open+5m-1ms`、volume 系は全行 0、
`count` は index サンプル数)。**header の有無は月から推測できない**
(72 か月の実測: あり 45 / なし 27、2022 前半で交互に現れる)。
"""

import hashlib
import json
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import polars as pl
import pytest

from mce import config, normalize_binance as nb
from mce.backtest.splits import FINAL_OOS_START
from mce.binance_vision import (
    DATASETS,
    MARKET_PATH,
    MARKET_TYPE,
    ledger_path,
    raw_dir,
    source_digest,
)

UTC = timezone.utc
BAR_MS = 5 * 60 * 1000
T0 = 1577836800000  # 2020-01-01T00:00:00Z
REPO = Path(__file__).resolve().parents[1]

IDX_HEADER = (
    "open_time,open,high,low,close,volume,close_time,quote_volume,count,"
    "taker_buy_volume,taker_buy_quote_volume,ignore"
)


def idx_row(open_ms: int, close: float = 7000.0, samples: int = 300,
            volume: float = 0.0, close_ms: int | None = None) -> str:
    return (
        f"{open_ms},{close},{close + 2},{close - 3},{close},{volume},"
        f"{close_ms if close_ms is not None else open_ms + BAR_MS - 1},"
        f"0,{samples},0,0,0"
    )


def write_zip(path: Path, lines: list[str]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as z:
        z.writestr(path.stem + ".csv", "\n".join(lines) + "\n")
    return path


def rows_of(lines: list[str]) -> list[list[str]]:
    return [line.split(",") for line in lines]


# --------------------------------------------------------------------------
# 既存 dataset の不変条件 — 追加が何も動かしていないこと
# --------------------------------------------------------------------------


def test_existing_dataset_specs_are_untouched():
    """Phase 7 の3系列と F1 / F2 / F4 の spec を1文字も変えていない。"""
    frozen = {
        "klines_5m": ("monthly", "data/futures/um/monthly/klines/{sym}/5m/{sym}-5m-{period}.zip",
                      "perp_linear", "exact", "bar_5m"),
        "premium_index_5m": ("monthly",
                             "data/futures/um/monthly/premiumIndexKlines/{sym}/5m/{sym}-5m-{period}.zip",
                             "perp_linear", "exact", "bar_5m"),
        "metrics_5m": ("daily", "data/futures/um/daily/metrics/{sym}/{sym}-metrics-{period}.zip",
                       "perp_linear", "exact", "bar_5m"),
        "mark_price_5m": ("monthly",
                          "data/futures/um/monthly/markPriceKlines/{sym}/5m/{sym}-5m-{period}.zip",
                          "perp_linear", "exact", "bar_5m"),
        "spot_klines_5m": ("monthly", "data/spot/monthly/klines/{sym}/5m/{sym}-5m-{period}.zip",
                           "spot", "last_trade_time", "bar_5m"),
        "funding_rate": ("monthly",
                         "data/futures/um/monthly/fundingRate/{sym}/{sym}-fundingRate-{period}.zip",
                         "perp_linear", "not_applicable", "event"),
    }
    for name, (cadence, template, market_type, policy, kind) in frozen.items():
        spec = DATASETS[name]
        assert spec.cadence == cadence, name
        assert spec.path_template == template, name
        assert spec.market_type == market_type, name
        assert spec.close_time_policy == policy, name
        assert spec.series_kind == kind, name


def test_index_ledger_does_not_disturb_other_digests(tmp_path: Path):
    """IDX の ledger を足しても、他 dataset の digest は動かない。"""
    other = tmp_path / "klines.jsonl"
    other.write_text(json.dumps({"period": "2020-01", "sha256": "aa", "status": "saved"}) + "\n")
    before = source_digest("klines_5m", ledger=other)["digest"]

    idx = tmp_path / "idx.jsonl"
    idx.write_text(json.dumps({"period": "2020-01", "sha256": "ii", "status": "saved"}) + "\n")
    idx_digest = source_digest("index_price_5m", ledger=idx)["digest"]

    assert source_digest("klines_5m", ledger=other)["digest"] == before
    assert before == hashlib.sha256(b"2020-01 aa").hexdigest()
    assert idx_digest != before


def test_index_has_its_own_raw_dir_ledger_and_parquet():
    others = ("klines_5m", "premium_index_5m", "metrics_5m", "mark_price_5m",
              "spot_klines_5m", "funding_rate")
    dirs = {d: raw_dir(d).as_posix() for d in (*others, "index_price_5m")}
    assert len(set(dirs.values())) == len(dirs), dirs
    ledgers = {d: ledger_path(d).as_posix() for d in (*others, "index_price_5m")}
    assert len(set(ledgers.values())) == len(ledgers), ledgers
    out = config.binance_index_price_parquet()
    assert out.name == "index_price_BTCUSDT_5m.parquet"
    for other in (config.binance_mark_price_parquet(), config.binance_klines_parquet(),
                  config.binance_premium_index_parquet(), config.binance_spot_klines_parquet()):
        assert out != other


def test_index_registry_is_separate_from_the_f1_f2_registry():
    from mce.phase8_inputs import PHASE8_INDEX_INPUTS, PHASE8_INPUTS

    assert set(PHASE8_INPUTS) == {"mark_price_5m", "spot_klines_5m"}
    assert set(PHASE8_INDEX_INPUTS) == {"index_price_5m"}
    assert not set(PHASE8_INPUTS) & set(PHASE8_INDEX_INPUTS)


# --------------------------------------------------------------------------
# dataset の登録(実測した path)
# --------------------------------------------------------------------------


def test_index_dataset_is_registered_under_the_futures_market():
    spec = DATASETS["index_price_5m"]
    assert spec.cadence == "monthly"
    assert spec.market_type == MARKET_TYPE == "perp_linear"
    assert spec.close_time_policy == "exact"
    assert spec.series_kind == "bar_5m"
    rel = spec.relative_path("BTCUSDT", "2021-06")
    assert rel.startswith(MARKET_PATH + "/")
    assert "indexPriceKlines" in rel
    assert rel.endswith("/indexPriceKlines/BTCUSDT/5m/BTCUSDT-5m-2021-06.zip")


# --------------------------------------------------------------------------
# 正規化 — 実測 schema
# --------------------------------------------------------------------------


def test_index_columns_cannot_be_confused_with_mark_or_traded_prices():
    """index / mark / 約定価格を**列名の水準で**区別する。"""
    df = nb.normalize_index_price(rows_of([idx_row(T0)]))
    assert set(df.columns) == {
        "ts", "index_open", "index_high", "index_low", "index_close",
        "index_samples", "symbol", "source", "market_type",
    }
    for foreign in ("open", "high", "low", "close", "volume", "trades",
                    "mark_open", "mark_close", "premium_open"):
        assert foreign not in df.columns, foreign


def test_index_values_and_samples():
    df = nb.normalize_index_price(rows_of([idx_row(T0, close=7195.36581933, samples=299)]))
    assert df["ts"].to_list() == [datetime(2020, 1, 1, tzinfo=UTC)]
    assert df["index_open"].to_list() == [7195.36581933]
    assert df["index_high"].to_list() == [7197.36581933]
    assert df["index_samples"].to_list() == [299]
    assert df["market_type"].to_list() == ["perp_linear"]


def test_index_rejects_nonzero_traded_volume():
    """index は板の約定ではない。0 でなければ**送出して止まる**。"""
    with pytest.raises(nb.BinanceNormalizationError, match="約定量ではない"):
        nb.normalize_index_price(rows_of([idx_row(T0, volume=1.5)]))


def test_index_rejects_a_wrong_close_time():
    """`close_time == open+5m-1ms` を実測で確認したので、崩れたら止める。"""
    with pytest.raises(nb.BinanceNormalizationError, match="close_time"):
        nb.normalize_index_price(rows_of([idx_row(T0, close_ms=T0 + BAR_MS)]))


def test_index_counts_stale_bars_without_dropping_them():
    """`count == 0` は前値横引き。**落とさず数える**(実測では 0 件だが規則は持つ)。"""
    stats: dict = {}
    df = nb.normalize_index_price(
        rows_of([idx_row(T0, samples=0), idx_row(T0 + BAR_MS, samples=300)]), stats=stats
    )
    assert df.height == 2, "stale バーを落としている"
    assert stats["index_stale_bars"] == 1


def test_index_accepts_headerless_and_headered_dumps(tmp_path: Path):
    """2020-01 は header 無し、2025-12 は header 付き(実測)。両方読める。"""
    headerless = write_zip(tmp_path / "BTCUSDT-5m-2020-01.zip", [idx_row(T0)])
    headered = write_zip(tmp_path / "BTCUSDT-5m-2025-12.zip", [IDX_HEADER, idx_row(T0)])
    assert len(nb.read_zip_rows(headerless, 12)) == 1
    assert len(nb.read_zip_rows(headered, 12)) == 1


def test_index_rejects_a_wrong_column_count(tmp_path: Path):
    bad = write_zip(tmp_path / "BTCUSDT-5m-2020-01.zip", ["1,2,3"])
    with pytest.raises(nb.BinanceNormalizationError, match="列数"):
        nb.read_zip_rows(bad, 12)


# --------------------------------------------------------------------------
# 封印 / 重複 / 欠測 / 冪等
# --------------------------------------------------------------------------


def test_per_target_cutoff_is_registered_and_defaults_are_unchanged():
    assert "index_price_5m" in nb.SEAL_CUTOFFS
    assert nb.seal_cutoff_for("index_price_5m") == FINAL_OOS_START
    for phase7 in ("klines_5m", "premium_index_5m", "metrics_5m"):
        assert nb.seal_cutoff_for(phase7) == FINAL_OOS_START, phase7


def test_seal_cutoff_drops_sealed_bars(tmp_path: Path):
    src = tmp_path / "raw"
    cutoff_ms = int(FINAL_OOS_START.timestamp() * 1000)
    write_zip(src / "BTCUSDT-5m-2025-12.zip",
              [IDX_HEADER, idx_row(cutoff_ms - BAR_MS), idx_row(cutoff_ms),
               idx_row(cutoff_ms + BAR_MS)])
    df, accounting = nb.scan_dataset("index_price_5m", source_dir=src)
    assert accounting["sealed_rows_dropped"] == 2
    assert df.height == 1
    assert df["ts"].max() < FINAL_OOS_START
    assert accounting["cutoff"] == FINAL_OOS_START.isoformat()


def test_overridable_cutoff_does_not_touch_the_phase7_default(tmp_path: Path):
    src = tmp_path / "raw"
    write_zip(src / "BTCUSDT-5m-2020-01.zip", [idx_row(T0), idx_row(T0 + BAR_MS)])
    earlier = datetime(2020, 1, 1, 0, 3, tzinfo=UTC)
    _, accounting = nb.scan_dataset("index_price_5m", source_dir=src, cutoff=earlier)
    assert accounting["sealed_rows_dropped"] == 1
    assert nb.seal_cutoff_for("klines_5m") == FINAL_OOS_START


def test_exact_duplicates_are_removed_and_counted(tmp_path: Path):
    src = tmp_path / "raw"
    write_zip(src / "BTCUSDT-5m-2020-01.zip",
              [idx_row(T0), idx_row(T0), idx_row(T0 + BAR_MS)])
    df, accounting = nb.scan_dataset("index_price_5m", source_dir=src)
    assert df.height == 2
    assert accounting["duplicate_rows_dropped"] == 1
    assert accounting["conflicting_duplicates"] == 0


def test_conflicting_duplicates_are_resolved_by_the_owning_file(tmp_path: Path):
    """月境界の行が前月ファイルにも入っている場合、その月のファイルを採る。"""
    src = tmp_path / "raw"
    feb = int(datetime(2020, 2, 1, tzinfo=UTC).timestamp() * 1000)
    write_zip(src / "BTCUSDT-5m-2020-01.zip", [idx_row(feb, close=1000.0)])
    write_zip(src / "BTCUSDT-5m-2020-02.zip", [idx_row(feb, close=2000.0)])
    df, accounting = nb.scan_dataset("index_price_5m", source_dir=src)
    assert df.height == 1
    assert df["index_open"].to_list() == [2000.0], "所有ファイルで解決していない"
    assert accounting["conflicting_duplicates"] == 1
    assert accounting["unresolved_conflicts"] == 0


def test_missing_bars_are_not_interpolated(tmp_path: Path):
    """欠測を埋めない。ギャップは**そのまま残る**。"""
    from mce.phase8_inputs import gap_report

    src = tmp_path / "raw"
    write_zip(src / "BTCUSDT-5m-2020-01.zip",
              [idx_row(T0), idx_row(T0 + 6 * BAR_MS)])  # 5 本欠測
    df, _ = nb.scan_dataset("index_price_5m", source_dir=src)
    assert df.height == 2, "存在しないバーが作られている"
    gaps = gap_report(df)
    assert gaps["missing_bars"] == 5
    assert gaps["gap_runs"] == 1
    assert gaps["gap_runs_detail"][0]["missing_bars"] == 5


def test_all_bars_sit_on_the_five_minute_grid(tmp_path: Path):
    src = tmp_path / "raw"
    write_zip(src / "BTCUSDT-5m-2020-01.zip", [idx_row(T0), idx_row(T0 + BAR_MS)])
    df, _ = nb.scan_dataset("index_price_5m", source_dir=src)
    for ts in df["ts"].to_list():
        assert int(ts.timestamp() * 1000) % BAR_MS == 0


def test_normalize_is_idempotent(tmp_path: Path):
    """同じ raw なら同じ parquet(再実行で行数も内容も動かない)。"""
    src = tmp_path / "raw"
    out = tmp_path / "index_price_BTCUSDT_5m.parquet"
    write_zip(src / "BTCUSDT-5m-2020-01.zip", [idx_row(T0), idx_row(T0 + BAR_MS)])
    first = nb.normalize_dataset("index_price_5m", source_dir=src, out_path=out)
    frame_a = pl.read_parquet(out)
    second = nb.normalize_dataset("index_price_5m", source_dir=src, out_path=out)
    frame_b = pl.read_parquet(out)
    assert first["rows"] == second["rows"] == 2
    assert second["rows_added"] == 0
    assert frame_a.equals(frame_b)


def test_adding_a_month_only_adds_its_rows(tmp_path: Path):
    src = tmp_path / "raw"
    out = tmp_path / "index_price_BTCUSDT_5m.parquet"
    write_zip(src / "BTCUSDT-5m-2020-01.zip", [idx_row(T0)])
    nb.normalize_dataset("index_price_5m", source_dir=src, out_path=out)
    feb = int(datetime(2020, 2, 1, tzinfo=UTC).timestamp() * 1000)
    write_zip(src / "BTCUSDT-5m-2020-02.zip", [idx_row(feb)])
    result = nb.normalize_dataset("index_price_5m", source_dir=src, out_path=out)
    assert result["rows"] == 2
    assert result["rows_added"] == 1


# --------------------------------------------------------------------------
# 実データ(取得済みのときだけ)
# --------------------------------------------------------------------------

IDX_PATH = config.binance_index_price_parquet()


@pytest.mark.skipif(not IDX_PATH.exists(), reason="index price 未取得")
def test_real_index_series_is_sealed_and_on_the_grid():
    df = pl.read_parquet(IDX_PATH)
    assert df["ts"].max() < FINAL_OOS_START
    assert df["market_type"].unique().to_list() == ["perp_linear"]
    assert df["source"].unique().to_list() == ["binance"]
    ts = [int(t.timestamp() * 1000) for t in df["ts"].to_list()]
    assert ts == sorted(ts) and len(ts) == len(set(ts))
    assert all(t % BAR_MS == 0 for t in ts)
    assert "mark_open" not in df.columns and "open" not in df.columns


@pytest.mark.skipif(
    not (IDX_PATH.exists() and config.binance_mark_price_parquet().exists()),
    reason="index / mark 未取得",
)
def test_real_index_and_mark_are_distinct_series():
    idx = pl.read_parquet(IDX_PATH)
    mark = pl.read_parquet(config.binance_mark_price_parquet())
    assert set(idx.columns) != set(mark.columns)
    joined = idx.join(mark, on="ts", how="inner")
    differing = joined.filter(pl.col("index_open") != pl.col("mark_open")).height
    assert differing > 0, "index と mark が同一系列になっている"


def test_off_grid_open_time_is_refused():
    """`open_time` が5分グリッドから外れたら**送出して止まる**。

    共有の `_check_close_time("exact")` は `close_time` しか見ないので、
    グリッド検査は IDX 側で明示的に持つ(Phase 7 系列の挙動は変えていない)。
    """
    with pytest.raises(nb.BinanceNormalizationError, match="5分グリッド"):
        nb.normalize_index_price(rows_of([idx_row(T0 + 60_000)]))


def test_grid_check_is_local_to_index_and_does_not_change_phase7():
    """Phase 7 / F1 の normalizer にグリッド検査を足していない(挙動不変)。"""
    off_grid = T0 + 60_000
    mark_row = (
        f"{off_grid},100,102,97,100,0,{off_grid + BAR_MS - 1},0,300,0,0,0"
    )
    # mark は従来どおり close_time だけを見る(グリッドでは落とさない)
    df = nb.normalize_mark_price(rows_of([mark_row]))
    assert df.height == 1


def test_headerless_and_headered_months_both_appear_in_the_real_dump():
    """header の有無は**月から推測できない**(2022 前半で交互に現れる)。"""
    import zipfile

    raw = raw_dir("index_price_5m")
    if not raw.exists():
        pytest.skip("index price 未取得")
    flags = []
    for path in sorted(raw.glob("*.zip")):
        with zipfile.ZipFile(path) as z:
            first = z.read(z.namelist()[0]).decode().splitlines()[0]
        flags.append(first.split(",")[0].strip().lower() == "open_time")
    assert any(flags) and not all(flags), "片方しか無い(前提が変わった)"
    assert flags != sorted(flags), "単調な切り替わりになっている(文書の記述と食い違う)"


def test_empty_dataset_selection_is_refused_not_silently_defaulted():
    """`--datasets` を値なしで渡したときに黙って F1/F2 を作らない。"""
    from mce.phase8_inputs import build

    with pytest.raises(ValueError, match="空である"):
        build("BTCUSDT", datasets=())
    with pytest.raises(ValueError, match="未知の dataset"):
        build("BTCUSDT", datasets=("nope_5m",))


def test_default_selection_is_still_f1_f2():
    """引数なしの既定は**従来どおり F1/F2**(既存 artifact を作り直さない)。"""
    from mce.phase8_inputs import PHASE8_INPUTS, build

    report = build("BTCUSDT")
    assert [d["dataset"] for d in report["datasets"]] == list(PHASE8_INPUTS)
    assert report["inventory"] == "phase8_inputs_v1"


@pytest.mark.parametrize("column,index", [
    ("index_open", 1), ("index_high", 2), ("index_low", 3), ("index_close", 4),
])
def test_each_price_column_reads_its_own_csv_column(column, index):
    """**どの CSV 列から来るか**を列ごとに固定する。

    列名の集合だけを見るテストでは、`index_low` と `index_close` の読み出しを
    入れ替えても検出できない(変異が生き残る)。
    """
    values = ["0", "11.0", "22.0", "33.0", "44.0", "0",
              str(T0 + BAR_MS - 1), "0", "300", "0", "0", "0"]
    values[0] = str(T0)
    df = nb.normalize_index_price([values])
    assert df[column].to_list() == [float(values[index])], f"{column} が列 {index} を読んでいない"


@pytest.mark.parametrize("index,name", [(1, "open"), (2, "high"), (3, "low"), (4, "close")])
def test_non_positive_prices_are_refused(index, name):
    """非正の index 価格を素通ししない(実測 0 件に依存した規則にしない)。"""
    values = [str(T0), "10.0", "12.0", "9.0", "11.0", "0",
              str(T0 + BAR_MS - 1), "0", "300", "0", "0", "0"]
    values[index] = "-5.0"
    with pytest.raises(nb.BinanceNormalizationError, match="正でない"):
        nb.normalize_index_price([values])
    values[index] = "0"
    with pytest.raises(nb.BinanceNormalizationError, match="正でない"):
        nb.normalize_index_price([values])


def test_positive_price_check_is_local_to_index():
    """Phase 7 / F1 の normalizer に正値検査を足していない(挙動不変)。"""
    mark_row = f"{T0},-5.0,-3.0,-9.0,-6.0,0,{T0 + BAR_MS - 1},0,300,0,0,0"
    df = nb.normalize_mark_price(rows_of([mark_row]))
    assert df.height == 1, "mark の既存挙動を変えている"


@pytest.mark.parametrize("index", [5, 7, 9, 10, 11])
def test_every_column_documented_as_always_zero_is_checked(index):
    """文書と docstring が「全行 0」と書く列を**全部**検査する。

    `ignore`(列 11)を検査から漏らすと、宣言より狭い実装になる。
    """
    values = [str(T0), "10.0", "12.0", "9.0", "11.0", "0",
              str(T0 + BAR_MS - 1), "0", "300", "0", "0", "0"]
    values[index] = "1.0"
    with pytest.raises(nb.BinanceNormalizationError, match="0 でない"):
        nb.normalize_index_price([values])


def test_merge_keeps_existing_rows_when_raw_is_corrected(tmp_path: Path):
    """**バー系列の merge は既存行を優先する**(訂正 dump は自動では入らない)。

    これは IDX 固有ではなく、Phase 7 から続く全バー系列の共有挙動である
    (`store.merge_parquet` が `keep="first"`)。挙動を変えると Phase 7 の
    正規化結果が動くので**変えない**。代わりに性質を明示して固定する:

    - `parquet == f(raw)` は保証されない。保証されるのは**追記の冪等性**だけ
    - 訂正 dump を取り込むには **parquet を消してから再正規化する**
    - イベント系列(`funding_rate`)は導出列を持つため全再生成で、ここが違う
    """
    src = tmp_path / "raw"
    out = tmp_path / "index_price_BTCUSDT_5m.parquet"
    write_zip(src / "BTCUSDT-5m-2020-01.zip", [idx_row(T0, close=7000.0)])
    nb.normalize_dataset("index_price_5m", source_dir=src, out_path=out)
    assert pl.read_parquet(out)["index_open"].to_list() == [7000.0]

    write_zip(src / "BTCUSDT-5m-2020-01.zip", [idx_row(T0, close=7100.0)])
    result = nb.normalize_dataset("index_price_5m", source_dir=src, out_path=out)
    assert pl.read_parquet(out)["index_open"].to_list() == [7000.0], (
        "merge の意味論が変わった(既存行優先でなくなった)"
    )
    assert result["rows_added"] == 0

    # **消してから作り直せば訂正が入る**(運用手順として固定する)
    out.unlink()
    nb.normalize_dataset("index_price_5m", source_dir=src, out_path=out)
    assert pl.read_parquet(out)["index_open"].to_list() == [7100.0]


def test_event_series_rebuilds_where_bar_series_merges():
    """イベント系列だけが全再生成である(挙動の違いを明示する)。"""
    from mce.binance_vision import DATASETS

    assert DATASETS["index_price_5m"].series_kind == "bar_5m"
    assert DATASETS["funding_rate"].series_kind == "event"


# --------------------------------------------------------------------------
# artifact と文書が実データと食い違わないこと
# --------------------------------------------------------------------------

INVENTORY = REPO / "data" / "manifests" / "phase8_index_price_v1.json"
PARQUET_MANIFEST = (
    REPO / "data" / "manifests" / "binance_index_price_index_price_BTCUSDT_5m.json"
)
SCHEMA_DOC = REPO / "docs" / "phase8" / "index_price_dump_schema_v1.md"


@pytest.mark.skipif(not INVENTORY.exists(), reason="inventory 未生成")
def test_inventory_records_what_the_task_requires():
    """行数・期間・欠測・digest・checksum 結果が artifact に残っている。"""
    d = json.loads(INVENTORY.read_text(encoding="utf-8"))["datasets"][0]
    assert d["dataset"] == "index_price_5m"
    assert d["market_type"] == "perp_linear"
    assert d["close_time_policy"] == "exact"
    sd = d["source_digest"]
    assert sd["present"] and sd["periods_absent"] == 0
    assert sd["checksum_verified"] == sd["periods_with_file"] == 72
    assert len(sd["digest"]) == 64
    p = d["parquet"]
    for key in ("rows", "expected_rows", "missing_rows", "ts_min", "ts_max", "columns"):
        assert key in p, key
    g = d["gaps"]
    assert g["missing_bars"] == p["missing_rows"]
    # 欠測は**明細で**残す(要約に丸めない)
    assert len(g["gap_runs_detail"]) == g["gap_runs"]
    assert sum(r["missing_bars"] for r in g["gap_runs_detail"]) == g["missing_bars"]
    ra = d["raw_accounting"]
    assert ra["sealed_rows_dropped"] == 0
    assert "index_stale_bars" in ra
    assert d["timestamp_semantics"]["seal_cutoff"] == FINAL_OOS_START.isoformat()


@pytest.mark.skipif(
    not (INVENTORY.exists() and PARQUET_MANIFEST.exists() and IDX_PATH.exists()),
    reason="index price 未取得",
)
def test_artifacts_agree_with_the_real_parquet():
    inv = json.loads(INVENTORY.read_text(encoding="utf-8"))["datasets"][0]
    man = json.loads(PARQUET_MANIFEST.read_text(encoding="utf-8"))
    df = pl.read_parquet(IDX_PATH)
    assert man["rows"] == inv["parquet"]["rows"] == df.height
    assert man["columns"] == df.columns
    assert hashlib.sha256(IDX_PATH.read_bytes()).hexdigest() == man["sha256"]
    assert df.filter(pl.col("index_samples") == 0).height == inv["raw_accounting"]["index_stale_bars"]


@pytest.mark.skipif(not (SCHEMA_DOC.exists() and INVENTORY.exists()), reason="未生成")
def test_schema_doc_numbers_match_the_artifact():
    """文書の主要な数値が artifact と一致する(片方だけ古くならない)。"""
    doc = SCHEMA_DOC.read_text(encoding="utf-8")
    d = json.loads(INVENTORY.read_text(encoding="utf-8"))["datasets"][0]
    assert d["source_digest"]["digest"] in doc
    assert f"{d['parquet']['rows']:,}" in doc, "行数が文書と食い違う"
    assert f"{d['gaps']['missing_bars']:,}" in doc, "欠測が文書と食い違う"
    assert str(d["gaps"]["largest_gap_bars"]) in doc
    assert str(d["raw_accounting"]["index_stale_bars"]) in doc

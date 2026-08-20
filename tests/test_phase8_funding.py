"""Phase 8 の入力データ配管(F4: funding 決済イベント)の適合テスト。

**取引系列を組み立てない。** rho / シグナル / 清算発生 / return / PnL を
一切計算しない。ここで検査するのは取り込みの正しさだけである。

合成 dump は実測した schema をそのまま使う(2020-01 / 2025-12 を probe して
確認した header は `calc_time,funding_interval_hours,last_funding_rate`)。
"""

import hashlib
import json
import zipfile
from datetime import datetime, timedelta, timezone
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
from mce.phase8_prereg import DELTA_PUB_SECONDS

UTC = timezone.utc
HOUR_MS = 60 * 60 * 1000
T0 = 1577836800000  # 2020-01-01T00:00:00Z(実測した最初の決済時刻)
REPO = Path(__file__).resolve().parents[1]

FUNDING_HEADER = "calc_time,funding_interval_hours,last_funding_rate"


def funding_row(ts_ms: int, rate: float = 0.0001, declared_hours: int = 8) -> str:
    return f"{ts_ms},{declared_hours},{rate}"


def write_zip(path: Path, lines: list[str]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as z:
        z.writestr(path.stem + ".csv", "\n".join(lines) + "\n")
    return path


def rows_of(lines: list[str]) -> list[list[str]]:
    return [line.split(",") for line in lines]


# --------------------------------------------------------------------------
# Phase 7 の不変条件 — 追加が既存を動かしていないこと
# --------------------------------------------------------------------------


def test_phase7_dataset_specs_and_digests_are_unchanged():
    """F4 を足しても Phase 7 の3 dataset spec が1文字も変わっていない。"""
    frozen = {
        "klines_5m": (
            "monthly",
            "data/futures/um/monthly/klines/{sym}/5m/{sym}-5m-{period}.zip",
            "perp_linear",
            "exact",
            "bar_5m",
        ),
        "premium_index_5m": (
            "monthly",
            "data/futures/um/monthly/premiumIndexKlines/{sym}/5m/{sym}-5m-{period}.zip",
            "perp_linear",
            "exact",
            "bar_5m",
        ),
        "metrics_5m": (
            "daily",
            "data/futures/um/daily/metrics/{sym}/{sym}-metrics-{period}.zip",
            "perp_linear",
            "exact",
            "bar_5m",
        ),
    }
    for name, (cadence, template, market_type, policy, kind) in frozen.items():
        spec = DATASETS[name]
        assert spec.cadence == cadence, name
        assert spec.path_template == template, name
        assert spec.market_type == market_type, name
        assert spec.close_time_policy == policy, name
        assert spec.series_kind == kind, name


def test_phase7_source_digest_is_untouched_by_the_funding_ledger(tmp_path: Path):
    """funding の ledger を足しても Phase 7 dataset の digest は動かない。"""
    phase7 = tmp_path / "klines.jsonl"
    phase7.write_text(json.dumps({"period": "2020-01", "sha256": "aa", "status": "saved"}) + "\n")
    before = source_digest("klines_5m", ledger=phase7)["digest"]

    funding = tmp_path / "funding.jsonl"
    funding.write_text(json.dumps({"period": "2020-01", "sha256": "ff", "status": "saved"}) + "\n")
    funding_digest = source_digest("funding_rate", ledger=funding)["digest"]

    assert source_digest("klines_5m", ledger=phase7)["digest"] == before
    assert before == hashlib.sha256(b"2020-01 aa").hexdigest()
    assert funding_digest != before, "別 dataset なのに digest が一致している"


def test_phase7_seal_cutoff_default_is_unchanged():
    """per-target 化しても Phase 7 経路の cutoff は `FINAL_OOS_START` のまま。"""
    for dataset in ("klines_5m", "premium_index_5m", "metrics_5m"):
        assert nb.seal_cutoff_for(dataset) == FINAL_OOS_START, dataset
    assert nb.apply_screening_cutoff.__defaults__ == (FINAL_OOS_START,)


# --------------------------------------------------------------------------
# dataset の登録と分離
# --------------------------------------------------------------------------


def test_funding_dataset_is_registered_as_an_event_series():
    spec = DATASETS["funding_rate"]
    assert spec.cadence == "monthly"
    assert spec.market_type == MARKET_TYPE == "perp_linear"
    assert spec.series_kind == "event", "バー系列として登録されている"
    assert spec.close_time_policy == "not_applicable"
    rel = spec.relative_path("BTCUSDT", "2021-06")
    assert rel.startswith(MARKET_PATH + "/")
    assert "fundingRate" in rel
    # **klines と違い interval ディレクトリが無く、ファイル名も別パターン**(実測)
    assert rel.endswith("/fundingRate/BTCUSDT/BTCUSDT-fundingRate-2021-06.zip")
    assert "/5m/" not in rel


def test_funding_has_its_own_raw_dir_and_ledger():
    """dataset ごとに raw も ledger も分かれること(digest が混ざらない)。"""
    others = ("klines_5m", "premium_index_5m", "metrics_5m", "mark_price_5m", "spot_klines_5m")
    dirs = {d: raw_dir(d).as_posix() for d in (*others, "funding_rate")}
    assert len(set(dirs.values())) == len(dirs), dirs
    ledgers = {d: ledger_path(d).as_posix() for d in (*others, "funding_rate")}
    assert len(set(ledgers.values())) == len(ledgers), ledgers
    assert raw_dir("funding_rate").name == "BTCUSDT"
    assert raw_dir("funding_rate").parent.name == "funding_rate"


def test_funding_output_does_not_collide_with_the_phase7_funding_parquet():
    """Phase 7 由来の `funding_rate/binance_BTCUSDT.parquet` を上書きしない。"""
    out = config.binance_funding_rate_parquet()
    assert out.name == "funding_rate_BTCUSDT.parquet"
    assert out.parent.name == "binance"
    assert out != config.funding_parquet()
    assert out.resolve() != (config.NORMALIZED_DIR / "funding_rate" / "binance_BTCUSDT.parquet").resolve()


# --------------------------------------------------------------------------
# 正規化 — 実測 schema
# --------------------------------------------------------------------------


def test_funding_normalizer_reads_the_measured_schema():
    df = nb.normalize_funding_rate(rows_of([funding_row(T0, 0.00012345, 8)]))
    assert df["ts"].to_list() == [datetime(2020, 1, 1, tzinfo=UTC)]
    assert df["funding_rate"].to_list() == [0.00012345]
    assert df["funding_interval_hours_declared"].to_list() == [8]
    assert df["market_type"].to_list() == ["perp_linear"]
    assert df["source"].to_list() == ["binance"]


def test_funding_has_no_fabricated_mark_price_column():
    """dump に markPrice は**無い**(実測)。null 列を作って埋めない。"""
    df = nb.normalize_funding_rate(rows_of([funding_row(T0)]))
    out = nb.add_funding_intervals(df)
    for column in ("mark_price", "mark_close", "mark_open", "mark_high", "mark_low"):
        assert column not in out.columns, column
    assert set(out.columns) == {
        "ts", "funding_rate", "funding_interval_hours", "funding_interval_ms",
        "funding_interval_hours_declared", "symbol", "source", "market_type",
    }


def test_funding_header_row_is_recognised_and_not_parsed_as_data(tmp_path: Path):
    path = write_zip(
        tmp_path / "BTCUSDT-fundingRate-2020-01.zip",
        [FUNDING_HEADER, funding_row(T0), funding_row(T0 + 8 * HOUR_MS)],
    )
    rows = nb.read_zip_rows(path, nb._FUNDING_COLUMNS)
    assert len(rows) == 2, "header 行がデータとして残っている"
    assert rows[0][0] == str(T0)


# --------------------------------------------------------------------------
# 間隔の導出(§4 の要求)
# --------------------------------------------------------------------------


def test_interval_is_derived_from_the_previous_settlement_not_fixed_at_8h():
    """1h / 4h / 8h が混在してもそのまま保持する。**8 に固定しない。**"""
    ts = [T0, T0 + 8 * HOUR_MS, T0 + 12 * HOUR_MS, T0 + 13 * HOUR_MS]
    df = nb.normalize_funding_rate(rows_of([funding_row(t) for t in ts]))
    stats: dict = {}
    out = nb.add_funding_intervals(df, stats)
    assert out["funding_interval_hours"].to_list() == [None, 8.0, 4.0, 1.0]
    assert stats["funding_interval_rows_derived"] == 3
    assert len(set(out["funding_interval_hours"].drop_nulls().to_list())) == 3


def test_first_event_interval_is_null_and_not_back_filled():
    df = nb.normalize_funding_rate(rows_of([funding_row(T0), funding_row(T0 + 8 * HOUR_MS)]))
    stats: dict = {}
    out = nb.add_funding_intervals(df, stats)
    assert out["funding_interval_hours"][0] is None
    assert out["funding_interval_ms"][0] is None
    assert stats["funding_interval_first_event_null"] == 1


def test_interval_does_not_look_ahead():
    """行 t の間隔は `ts[t] - ts[t-1]` だけで決まる(未来行から逆算しない)。

    末尾に決済を1本足しても、既存行の間隔は1つも変わってはならない。
    """
    ts = [T0, T0 + 8 * HOUR_MS, T0 + 16 * HOUR_MS]
    short = nb.add_funding_intervals(nb.normalize_funding_rate(rows_of([funding_row(t) for t in ts])))
    extended = nb.add_funding_intervals(
        nb.normalize_funding_rate(rows_of([funding_row(t) for t in (*ts, T0 + 17 * HOUR_MS)]))
    )
    assert extended["funding_interval_ms"].to_list()[:3] == short["funding_interval_ms"].to_list()


def test_subsecond_jitter_is_not_rounded_away_but_is_tolerated():
    """実測の `calc_time` は正時 + 0〜47ms。**値は丸めず**、宣言値との比較だけ許容する。"""
    ts = [T0, T0 + 8 * HOUR_MS + 4]  # +4ms のジッタ
    stats: dict = {}
    out = nb.add_funding_intervals(
        nb.normalize_funding_rate(rows_of([funding_row(t) for t in ts])), stats
    )
    assert out["funding_interval_ms"].to_list()[1] == 8 * HOUR_MS + 4, "ms を丸めている"
    assert out["funding_interval_hours"].to_list()[1] != 8.0
    assert stats["funding_interval_disagrees_with_declared_rows"] == 0
    assert stats["funding_interval_max_deviation_ms"] == 4


def test_non_positive_and_disagreeing_intervals_are_recorded_as_anomalies():
    """非正・宣言値と食い違う間隔は**数える**。落とさない・丸めない。"""
    ts = [T0, T0 + 8 * HOUR_MS, T0 + 200 * HOUR_MS]  # 3本目は宣言 8h と大きく食い違う
    stats: dict = {}
    out = nb.add_funding_intervals(
        nb.normalize_funding_rate(rows_of([funding_row(t) for t in ts])), stats
    )
    assert out.height == 3, "異常間隔の行が落ちている"
    assert stats["funding_interval_non_positive_rows"] == 0
    assert stats["funding_interval_disagrees_with_declared_rows"] == 1
    # 導出 192h に対し宣言は 8h。ずれは 184h であって 192h ではない。
    assert stats["funding_interval_max_deviation_ms"] == 184 * HOUR_MS


def test_declared_interval_is_kept_separately_from_the_derived_one():
    """dump の宣言値と導出値を**別列**で持つ(取り違えない)。"""
    df = nb.normalize_funding_rate(
        rows_of([funding_row(T0, declared_hours=8), funding_row(T0 + 4 * HOUR_MS, declared_hours=4)])
    )
    out = nb.add_funding_intervals(df)
    assert out["funding_interval_hours_declared"].to_list() == [8, 4]
    assert out["funding_interval_hours"].to_list() == [None, 4.0]


# --------------------------------------------------------------------------
# 決済間隔の導出は**系列全体**に1回だけ適用する
# --------------------------------------------------------------------------


def test_interval_is_derived_across_month_files_not_per_file(tmp_path: Path):
    """月ファイルをまたいでも、各月の先頭行の間隔が null にならない。"""
    src = tmp_path / "raw"
    jan_last = T0 + 16 * HOUR_MS
    write_zip(src / "BTCUSDT-fundingRate-2020-01.zip",
              [FUNDING_HEADER, funding_row(T0), funding_row(T0 + 8 * HOUR_MS), funding_row(jan_last)])
    write_zip(src / "BTCUSDT-fundingRate-2020-02.zip",
              [FUNDING_HEADER, funding_row(jan_last + 8 * HOUR_MS)])
    df, accounting = nb.scan_dataset("funding_rate", source_dir=src)
    assert df.height == 4
    intervals = df.sort("ts")["funding_interval_hours"].to_list()
    assert intervals == [None, 8.0, 8.0, 8.0], "2月の先頭が null になっている"
    assert accounting["funding_interval_first_event_null"] == 1
    assert accounting["series_kind"] == "event"


# --------------------------------------------------------------------------
# 封印 / 重複 / 冪等
# --------------------------------------------------------------------------


def test_seal_cutoff_drops_settlements_at_or_after_final_oos(tmp_path: Path):
    """`ts >= 2026-01-01` の決済は**物理的に落とす**。"""
    src = tmp_path / "raw"
    before = int(datetime(2025, 12, 31, 16, tzinfo=UTC).timestamp() * 1000)
    at_cutoff = int(FINAL_OOS_START.timestamp() * 1000)
    write_zip(src / "BTCUSDT-fundingRate-2025-12.zip",
              [FUNDING_HEADER, funding_row(before), funding_row(at_cutoff),
               funding_row(at_cutoff + 8 * HOUR_MS)])
    df, accounting = nb.scan_dataset("funding_rate", source_dir=src)
    assert accounting["sealed_rows_dropped"] == 2
    assert df.height == 1
    assert df["ts"].max() < FINAL_OOS_START
    assert accounting["cutoff"] == FINAL_OOS_START.isoformat()


def test_per_target_cutoff_is_explicit_and_overridable(tmp_path: Path):
    """cutoff は **target ごとに明示**され、グローバル改変なしで差し替えられる。"""
    assert set(nb.SEAL_CUTOFFS) >= set(nb.NORMALIZERS)
    assert nb.SEAL_CUTOFFS["funding_rate"] == FINAL_OOS_START, "H5 承認前は Phase 7 と同値"
    src = tmp_path / "raw"
    write_zip(src / "BTCUSDT-fundingRate-2020-01.zip",
              [FUNDING_HEADER, funding_row(T0), funding_row(T0 + 8 * HOUR_MS)])
    earlier = datetime(2020, 1, 1, 4, tzinfo=UTC)
    _, accounting = nb.scan_dataset("funding_rate", source_dir=src, cutoff=earlier)
    assert accounting["sealed_rows_dropped"] == 1
    assert nb.seal_cutoff_for("klines_5m") == FINAL_OOS_START, "Phase 7 の既定が動いた"


def test_market_type_is_part_of_the_dedup_key():
    """同一 source/symbol/ts でも market_type が違えば別行(Y23)。"""
    assert nb.KEY_COLS == ["source", "symbol", "market_type", "ts"]
    perp = nb.normalize_funding_rate(rows_of([funding_row(T0)]), market_type="perp_linear")
    spot = nb.normalize_funding_rate(rows_of([funding_row(T0)]), market_type="spot")
    both = pl.concat([perp, spot], how="vertical")
    assert both.unique(subset=nb.KEY_COLS).height == 2, "spot が perp を上書きしている"
    assert both.unique(subset=["source", "symbol", "ts"]).height == 1, "前提が変わった"


def test_exact_duplicates_are_removed_and_counted(tmp_path: Path):
    src = tmp_path / "raw"
    write_zip(src / "BTCUSDT-fundingRate-2020-01.zip",
              [FUNDING_HEADER, funding_row(T0), funding_row(T0), funding_row(T0 + 8 * HOUR_MS)])
    df, accounting = nb.scan_dataset("funding_rate", source_dir=src)
    assert df.height == 2
    assert accounting["duplicate_rows_dropped"] == 1
    assert accounting["conflicting_duplicates"] == 0


def test_missing_months_are_not_interpolated(tmp_path: Path):
    """欠けた月を**埋めない**。間隔がそのぶん長く出るだけである。"""
    src = tmp_path / "raw"
    write_zip(src / "BTCUSDT-fundingRate-2020-01.zip", [FUNDING_HEADER, funding_row(T0)])
    march = int(datetime(2020, 3, 1, tzinfo=UTC).timestamp() * 1000)
    write_zip(src / "BTCUSDT-fundingRate-2020-03.zip", [FUNDING_HEADER, funding_row(march)])
    df, accounting = nb.scan_dataset("funding_rate", source_dir=src)
    assert df.height == 2, "存在しない決済が作られている"
    gap_hours = df.sort("ts")["funding_interval_hours"].to_list()[1]
    assert gap_hours == (march - T0) / HOUR_MS
    assert accounting["funding_interval_disagrees_with_declared_rows"] == 1


def test_normalize_is_idempotent(tmp_path: Path):
    """同じ raw なら同じ parquet(再実行で行数も内容も動かない)。"""
    src = tmp_path / "raw"
    out = tmp_path / "funding_rate_BTCUSDT.parquet"
    write_zip(src / "BTCUSDT-fundingRate-2020-01.zip",
              [FUNDING_HEADER, funding_row(T0), funding_row(T0 + 8 * HOUR_MS)])
    first = nb.normalize_dataset("funding_rate", source_dir=src, out_path=out)
    frame_a = pl.read_parquet(out)
    second = nb.normalize_dataset("funding_rate", source_dir=src, out_path=out)
    frame_b = pl.read_parquet(out)
    assert first["rows"] == second["rows"] == 2
    assert second["rows_added"] == 0
    assert frame_a.equals(frame_b)


def test_rebuild_refreshes_derived_intervals_when_history_is_added(tmp_path: Path):
    """後から過去月を足したとき、**古い null が居座らない**(導出列の再生成)。"""
    src = tmp_path / "raw"
    out = tmp_path / "funding_rate_BTCUSDT.parquet"
    feb = int(datetime(2020, 2, 1, tzinfo=UTC).timestamp() * 1000)
    write_zip(src / "BTCUSDT-fundingRate-2020-02.zip", [FUNDING_HEADER, funding_row(feb)])
    nb.normalize_dataset("funding_rate", source_dir=src, out_path=out)
    assert pl.read_parquet(out)["funding_interval_hours"].to_list() == [None]

    write_zip(src / "BTCUSDT-fundingRate-2020-01.zip",
              [FUNDING_HEADER, funding_row(feb - 8 * HOUR_MS)])
    nb.normalize_dataset("funding_rate", source_dir=src, out_path=out)
    after = pl.read_parquet(out).sort("ts")
    assert after.height == 2
    assert after["funding_interval_hours"].to_list() == [None, 8.0], "古い null が残っている"


# --------------------------------------------------------------------------
# 観測可能性 — as-of join が未来の funding を掴まない
# --------------------------------------------------------------------------


def test_asof_join_never_picks_a_settlement_published_after_the_decision_time():
    """**未来 funding を as-of join が掴まない**(protocol §5.2 / Y5)。

    本コミットは feature 層(`features_carry`)を実装していない。ここで固定するのは
    正規化済み系列が満たすべき性質である: `funding_key = 決済時刻 + DELTA_PUB` を
    キーに backward join すると、決定時刻より後に公開された決済は決して選ばれない。
    tolerance を広げても未来参照は防げないので、**キーのシフト**で表す。
    """
    settlements = [T0, T0 + 8 * HOUR_MS, T0 + 16 * HOUR_MS]
    funding = nb.add_funding_intervals(
        nb.normalize_funding_rate(rows_of([funding_row(t, rate=i / 1e5)
                                           for i, t in enumerate(settlements)]))
    ).with_columns(
        (pl.col("ts") + pl.duration(seconds=DELTA_PUB_SECONDS)).alias("funding_key")
    ).sort("funding_key")

    # 5分バーの開始時刻(決定時刻)。決済直前・直後・ちょうどを含める。
    bar_ts = [
        datetime(2020, 1, 1, tzinfo=UTC) - timedelta(minutes=5),
        datetime(2020, 1, 1, tzinfo=UTC),
        datetime(2020, 1, 1, 7, 55, tzinfo=UTC),
        datetime(2020, 1, 1, 8, 5, tzinfo=UTC),
        datetime(2020, 1, 1, 16, 5, tzinfo=UTC),
    ]
    bars = pl.DataFrame({"ts": bar_ts}).with_columns(
        pl.col("ts").cast(pl.Datetime(time_unit="ms", time_zone="UTC"))
    ).sort("ts")

    joined = bars.join_asof(
        funding.select(["funding_key", "funding_rate"]),
        left_on="ts", right_on="funding_key", strategy="backward",
    )
    for row in joined.iter_rows(named=True):
        if row["funding_rate"] is None:
            continue
        picked = funding.filter(pl.col("funding_rate") == row["funding_rate"])
        assert picked["funding_key"][0] <= row["ts"], (
            f"決定時刻 {row['ts']} より後に公開された決済を掴んでいる"
        )
    # 決済ちょうどのバーは、公開遅延のぶんまだ掴めない(境界を甘くしない)
    at_settlement = joined.filter(pl.col("ts") == datetime(2020, 1, 1, tzinfo=UTC))
    assert at_settlement["funding_rate"][0] is None
    assert DELTA_PUB_SECONDS > 0


# --------------------------------------------------------------------------
# inventory
# --------------------------------------------------------------------------

from mce.phase8_funding import PHASE8_EVENT_INPUTS, interval_report, inventory  # noqa: E402
from mce.phase8_inputs import PHASE8_INPUTS  # noqa: E402


def test_funding_inventory_registry_is_separate_from_f1_f2():
    """F1/F2 の inventory registry を汚さない(向こうはバー系列の会計)。"""
    assert set(PHASE8_EVENT_INPUTS) == {"funding_rate"}
    assert set(PHASE8_INPUTS) == {"mark_price_5m", "spot_klines_5m"}
    assert not set(PHASE8_EVENT_INPUTS) & set(PHASE8_INPUTS)


def test_interval_report_counts_the_distribution_without_assuming_8h():
    df = nb.add_funding_intervals(
        nb.normalize_funding_rate(
            rows_of([funding_row(t) for t in (T0, T0 + 8 * HOUR_MS, T0 + 12 * HOUR_MS)])
        )
    )
    report = interval_report(df)
    assert report["events"] == 3
    assert report["intervals_derived"] == 2
    assert report["first_event_interval_is_null"] is True
    assert report["distribution_hours"] == {"4": 1, "8": 1}
    assert report["min_interval_hours"] == 4.0
    assert report["max_interval_hours"] == 8.0


def test_interval_report_is_empty_for_an_empty_series():
    assert interval_report(pl.DataFrame())["events"] == 0


@pytest.mark.skipif(
    not config.binance_funding_rate_parquet().exists(), reason="funding 未取得"
)
def test_real_funding_series_is_an_event_series_under_the_seal():
    record = inventory("funding_rate")
    assert record["series_kind"] == "event"
    assert record["market_type"] == "perp_linear"
    assert record["timestamp_semantics"]["field"] == "calc_time"
    assert "mark_price" in record["columns_absent_in_source"]
    # 期待間隔を持たない = 存在しない「欠測バー」を数えていない
    assert "expected_rows" not in record["parquet"]
    assert "missing_rows" not in record["parquet"]

    df = pl.read_parquet(config.binance_funding_rate_parquet())
    assert df["ts"].max() < FINAL_OOS_START, "封印域の決済が残っている"
    assert df["market_type"].unique().to_list() == ["perp_linear"]
    assert df["funding_interval_hours"].null_count() == 1, "先頭以外に null がある"
    assert "mark_price" not in df.columns

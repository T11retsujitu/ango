import json

from conftest import make_ohlcv
from mce.manifest import dataset_manifest, write_manifest

BAR_MS = 5 * 60_000


def _write(tmp_path, bars, name="t.parquet"):
    path = tmp_path / name
    make_ohlcv(bars).write_parquet(path)
    return path


def test_manifest_fields_and_missing_rows(tmp_path):
    # 0, 5, 15分 → 期待4本(0,5,10,15)のうち10分が欠損
    path = _write(tmp_path, [(0, 100, 1), (5, 101, 1), (15, 102, 1)])
    m = dataset_manifest(path, interval_ms=BAR_MS)
    assert m["rows"] == 3
    assert m["expected_rows"] == 4
    assert m["missing_rows"] == 1
    assert m["ts_min"] == "1970-01-01T00:00:00+00:00"
    assert m["ts_max"] == "1970-01-01T00:15:00+00:00"
    assert "close" in m["columns"]
    assert len(m["sha256"]) == 64


def test_manifest_is_deterministic(tmp_path):
    path = _write(tmp_path, [(0, 100, 1), (5, 101, 1)])
    assert dataset_manifest(path, BAR_MS) == dataset_manifest(path, BAR_MS)


def test_manifest_detects_content_change(tmp_path):
    p1 = _write(tmp_path, [(0, 100, 1)], "a.parquet")
    p2 = _write(tmp_path, [(0, 999, 1)], "b.parquet")
    assert dataset_manifest(p1, BAR_MS)["sha256"] != dataset_manifest(p2, BAR_MS)["sha256"]


def test_write_manifest_roundtrip(tmp_path):
    path = _write(tmp_path, [(0, 100, 1), (5, 101, 1)])
    out = write_manifest("ohlcv", path, BAR_MS, tmp_path / "manifests")
    loaded = json.loads(out.read_text(encoding="utf-8"))
    assert loaded == dataset_manifest(path, BAR_MS)


def test_no_interval_skips_missing_calc(tmp_path):
    path = _write(tmp_path, [(0, 100, 1), (480, 101, 1)])  # funding のような不定間隔
    m = dataset_manifest(path, interval_ms=None)
    assert "missing_rows" not in m
    assert m["rows"] == 2


# --------------------------------------------------------------------------
# CLI は fail-closed である(既定で commit 済みの指紋を上書きしない)
# --------------------------------------------------------------------------

import json as _json
import pytest

from mce import config, manifest as manifest_mod
from mce.manifest import ManifestSelectionError, main, select_datasets


def _snapshot(directory) -> dict:
    """内容と mtime_ns の両方を記録する(書き換えを見逃さない)。"""
    return {
        p.name: (p.read_text(encoding="utf-8"), p.stat().st_mtime_ns)
        for p in sorted(directory.glob("*.json"))
    }


@pytest.fixture()
def manifests_dir(tmp_path, monkeypatch):
    """`data/manifests/` を触らずに CLI を試すための差し替え先。"""
    out = tmp_path / "manifests"
    out.mkdir()
    for name in ("sentinel_a.json", "sentinel_b.json"):
        (out / name).write_text('{"sentinel": true}\n', encoding="utf-8")
    monkeypatch.setattr(config, "MANIFESTS_DIR", out)
    return out


def test_no_arguments_writes_nothing_and_exits_nonzero(manifests_dir):
    """**引数なしでは1バイトも書かない。**"""
    before = _snapshot(manifests_dir)
    assert main([]) != 0
    assert _snapshot(manifests_dir) == before


def test_no_arguments_does_not_touch_the_real_manifests_directory():
    """本物の data/manifests/ も内容・mtime とも変化しない。"""
    real = config.MANIFESTS_DIR
    before = _snapshot(real)
    assert main([]) != 0
    assert _snapshot(real) == before, "引数なし実行で commit 済み manifest が動いた"


@pytest.mark.skipif(
    not config.binance_index_price_parquet().exists(), reason="index price 未取得"
)
def test_selecting_one_dataset_leaves_the_others_untouched(manifests_dir):
    before = _snapshot(manifests_dir)
    assert main(["--datasets", "binance_index_price"]) == 0
    after = _snapshot(manifests_dir)
    written = set(after) - set(before)
    assert len(written) == 1
    assert next(iter(written)).startswith("binance_index_price_")
    for name, value in before.items():
        assert after[name] == value, f"{name} が書き換わった"


def test_only_all_selects_every_dataset():
    """`--all` のときだけ全系列が対象になる。"""
    every = select_datasets(None, select_all=True)
    assert every == list(manifest_mod._datasets())
    assert len(every) > 1
    assert select_datasets(["ohlcv"]) == ["ohlcv"]


def test_empty_unknown_and_duplicate_selections():
    with pytest.raises(ManifestSelectionError, match="空である"):
        select_datasets([])
    with pytest.raises(ManifestSelectionError, match="未知の dataset"):
        select_datasets(["ohlcv", "nope"])
    with pytest.raises(ManifestSelectionError, match="指定していない"):
        select_datasets(None)
    with pytest.raises(ManifestSelectionError, match="同時に指定できない"):
        select_datasets(["ohlcv"], select_all=True)
    # 重複は順序を保って一意化する(同じ manifest を二度書かない)
    assert select_datasets(["labels", "ohlcv", "labels"]) == ["labels", "ohlcv"]


def test_argparse_rejects_empty_and_unknown_before_writing(manifests_dir):
    before = _snapshot(manifests_dir)
    for argv in (["--datasets"], ["--datasets", "nope"], ["--datasets", "ohlcv", "--all"]):
        with pytest.raises(SystemExit) as exc:
            main(argv)
        assert exc.value.code != 0
    assert _snapshot(manifests_dir) == before


def test_a_failing_selection_leaves_no_partial_writes(manifests_dir, monkeypatch, tmp_path):
    """**失敗時に部分的な書き換えを残さない。**

    存在する dataset と存在しない dataset を同時に指定したとき、存在する側だけを
    書いてから落ちる、という挙動になっていないことを固定する。
    """
    present = tmp_path / "present.parquet"
    import polars as pl

    pl.DataFrame({"ts": [1], "v": [1.0]}).with_columns(
        pl.col("ts").cast(pl.Datetime(time_unit="ms", time_zone="UTC"))
    ).write_parquet(present)
    monkeypatch.setattr(
        manifest_mod, "_datasets",
        lambda: {"present": (present, None), "absent": (tmp_path / "nope.parquet", None)},
    )
    before = _snapshot(manifests_dir)
    assert main(["--datasets", "present", "absent"]) != 0
    assert _snapshot(manifests_dir) == before, "存在する側だけ書いてから落ちている"
    # 単独指定なら書ける(上のは「欠落があるから止めた」であって機能不全ではない)
    assert main(["--datasets", "present"]) == 0
    assert set(_snapshot(manifests_dir)) - set(before) == {"present_present.json"}


def test_all_tolerates_absent_datasets_but_still_writes_the_present_ones(
    manifests_dir, monkeypatch, tmp_path
):
    """`--all` は存在しないものを飛ばす(明示指定とは扱いが違う)。"""
    import polars as pl

    present = tmp_path / "present.parquet"
    pl.DataFrame({"ts": [1], "v": [1.0]}).with_columns(
        pl.col("ts").cast(pl.Datetime(time_unit="ms", time_zone="UTC"))
    ).write_parquet(present)
    monkeypatch.setattr(
        manifest_mod, "_datasets",
        lambda: {"present": (present, None), "absent": (tmp_path / "nope.parquet", None)},
    )
    assert main(["--all"]) == 0
    assert (manifests_dir / "present_present.json").exists()
    assert not (manifests_dir / "absent_nope.json").exists()


def test_all_with_nothing_present_fails_without_writing(manifests_dir, monkeypatch, tmp_path):
    monkeypatch.setattr(
        manifest_mod, "_datasets", lambda: {"absent": (tmp_path / "nope.parquet", None)}
    )
    before = _snapshot(manifests_dir)
    assert main(["--all"]) != 0
    assert _snapshot(manifests_dir) == before


def test_written_manifest_is_still_the_deterministic_content(manifests_dir, monkeypatch, tmp_path):
    """CLI 経由でも中身は `dataset_manifest` と同一(書き方を変えていない)。"""
    import polars as pl

    present = tmp_path / "present.parquet"
    pl.DataFrame({"ts": [1], "v": [1.0]}).with_columns(
        pl.col("ts").cast(pl.Datetime(time_unit="ms", time_zone="UTC"))
    ).write_parquet(present)
    monkeypatch.setattr(manifest_mod, "_datasets", lambda: {"present": (present, None)})
    assert main(["--datasets", "present"]) == 0
    written = _json.loads((manifests_dir / "present_present.json").read_text(encoding="utf-8"))
    assert written == dataset_manifest(present, None)

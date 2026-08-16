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

"""data inventory の単体テスト(データ本体が無い環境でも壊れないことを含む)。"""

import json
from pathlib import Path

from mce import data_inventory as di


def test_manifest_inventory_absent_dir(tmp_path: Path):
    out = di.manifest_inventory(tmp_path / "nope")
    assert out["present"] is False
    assert out["datasets"] == {}


def test_manifest_inventory_reads_fields(tmp_path: Path):
    (tmp_path / "ohlcv_x.json").write_text(
        json.dumps(
            {
                "file": "x.parquet",
                "sha256": "aa",
                "size_bytes": 10,
                "rows": 3,
                "missing_rows": 0,
                "ts_min": "2026-01-01T00:00:00+00:00",
                "ts_max": "2026-01-02T00:00:00+00:00",
                "columns": ["ts"],
            }
        ),
        encoding="utf-8",
    )
    out = di.manifest_inventory(tmp_path)
    assert out["present"] is True
    entry = out["datasets"]["ohlcv_x"]
    assert entry["rows"] == 3
    assert entry["ts_max"] == "2026-01-02T00:00:00+00:00"
    assert "columns" not in entry  # 列名は在庫表には持ち込まない


def test_microstructure_inventory_absent(tmp_path: Path):
    out = di.microstructure_inventory(tmp_path / "missing")
    assert out["present"] is False
    assert out["tables"] == {}


def test_microstructure_inventory_counts_partitions(tmp_path: Path):
    for day in ("2026-08-10", "2026-08-11"):
        part = (
            tmp_path
            / "trades"
            / "schema_version=3"
            / f"arrival_date={day}"
            / "arrival_hour=00"
        )
        part.mkdir(parents=True)
        (part / "part-v3-abc-000000.parquet").write_bytes(b"x" * 5)
    out = di.microstructure_inventory(tmp_path)
    trades = out["tables"]["trades"]
    assert trades["files"] == 2
    assert trades["bytes"] == 10
    assert trades["arrival_days"] == 2
    assert trades["arrival_date_min"] == "2026-08-10"
    assert trades["arrival_date_max"] == "2026-08-11"
    assert out["tables"]["bbo"] == {"present": False}
    assert out["arrival_days_total"] == 2


def test_raw_ws_inventory_flags_partial(tmp_path: Path):
    day = tmp_path / "public" / "2026" / "08" / "10"
    day.mkdir(parents=True)
    (day / "a.jsonl.gz").write_bytes(b"x" * 3)
    (day / "b.jsonl.gz.partial").write_bytes(b"x")
    out = di.raw_ws_inventory(tmp_path)
    public = out["streams"]["public"]
    assert public["files"] == 1  # partial は closed に数えない
    assert public["partial_files"] == 1
    assert public["utc_day_min"] == "2026-08-10"
    assert out["streams"]["business"] == {"present": False}


def test_build_inventory_is_deterministic_and_renderable():
    first = di.build_inventory()
    second = di.build_inventory()
    assert first == second  # 時刻・乱数を含めない
    text = di.render(first)
    assert "# data inventory" in text
    assert "## microstructure normalized" in text

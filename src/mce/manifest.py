"""データ資産の manifest(指紋)生成。

    python -m mce.manifest

data 配下の各 Parquet について sha256・行数・列名・ts 範囲・欠損バー数を
data/manifests/<stem>.json へ記録する(git 管理)。実験 artifact はこの
sha256 を参照して「どのデータで評価したか」を固定する。

manifest はファイル内容から決定的に導出される(タイムスタンプ等の
非決定的フィールドは含めない)。
"""

import hashlib
import json
from pathlib import Path

import polars as pl

from mce import config

# dataset 名 → (パス, 期待バー間隔 ms。None = 不定間隔で欠損数を計算しない)
def _datasets() -> dict[str, tuple[Path, int | None]]:
    return {
        "ohlcv": (config.ohlcv_parquet(), config.BAR_MS),
        "funding_rate": (config.funding_parquet(), None),
        "open_interest": (config.open_interest_parquet(), config.BAR_MS),
        "features": (config.features_parquet(), config.BAR_MS),
        "labels": (config.labels_parquet(), config.BAR_MS),
    }


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def dataset_manifest(path: Path, interval_ms: int | None = None) -> dict:
    """1つの Parquet の manifest dict(決定的)。"""
    df = pl.read_parquet(path)
    ts = df["ts"] if "ts" in df.columns else None
    manifest: dict = {
        "file": path.name,
        "sha256": sha256_file(path),
        "size_bytes": path.stat().st_size,
        "rows": df.height,
        "columns": df.columns,
    }
    if ts is not None and df.height > 0:
        ts_min, ts_max = ts.min(), ts.max()
        manifest["ts_min"] = ts_min.isoformat()
        manifest["ts_max"] = ts_max.isoformat()
        if interval_ms is not None:
            expected = (int(ts_max.timestamp() * 1000) - int(ts_min.timestamp() * 1000)) // interval_ms + 1
            manifest["expected_rows"] = expected
            manifest["missing_rows"] = expected - df.height
    return manifest


def write_manifest(name: str, path: Path, interval_ms: int | None, out_dir: Path) -> Path:
    m = dataset_manifest(path, interval_ms)
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{name}_{path.stem}.json"
    out.write_text(json.dumps(m, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return out


def main() -> None:
    written = 0
    for name, (path, interval_ms) in _datasets().items():
        if not path.exists():
            print(f"{name}: なし ({path})")
            continue
        out = write_manifest(name, path, interval_ms, config.MANIFESTS_DIR)
        print(f"{name}: -> {out}")
        written += 1
    if written == 0:
        raise SystemExit("manifest 対象のデータがありません")


if __name__ == "__main__":
    main()

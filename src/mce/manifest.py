"""データ資産の manifest(指紋)生成。

    python -m mce.manifest --datasets ohlcv features labels
    python -m mce.manifest --all

**対象の明示が必須である(fail-closed)。** 引数なしでは何も書かずに非ゼロ終了する。
manifest は「どのデータで評価したか」を固定する指紋なので、既定で全件を上書きすると
**別環境で実行しただけで commit 済みの指紋が消える**。互換性より研究記録の保全を採る。

**旧い呼び出し方が残っている文書が2つある。**(どちらも**編集してはいけない**)

- `docs/phase1/phase1a_protocol.md`(v1 として**凍結**済みのプロトコル)
- `docs/phase8/phase8_input_plumbing_v1.md`(`carry_freeze_v1_8_5.json` が sha256 で pin)

**凍結記録は「その時そう実行した」という履歴**であり、後から書き換えない。
そちらの手順をなぞる場合は `--all` を足すこと(エラーメッセージにも出る)。

data 配下の各 Parquet について sha256・行数・列名・ts 範囲・欠損バー数を
data/manifests/<stem>.json へ記録する(git 管理)。実験 artifact はこの
sha256 を参照して「どのデータで評価したか」を固定する。

manifest はファイル内容から決定的に導出される(タイムスタンプ等の
非決定的フィールドは含めない)。
"""

import hashlib
import json
import sys
from pathlib import Path
from typing import Sequence

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
        # Phase 7 Tier 0(Binance Vision)。metrics は5分スナップショットなので
        # 期待間隔は同じ 5m だが、欠測の意味は「その時刻のスナップショットが無い」。
        "binance_klines": (config.binance_klines_parquet(), config.BAR_MS),
        "binance_metrics": (config.binance_metrics_parquet(), config.BAR_MS),
        "binance_premium_index": (config.binance_premium_index_parquet(), config.BAR_MS),
        "binance_features": (config.binance_features_parquet(), config.BAR_MS),
        # Phase 8 の入力(F1 / F2)。**mark と spot は別 dataset・別 digest**。
        "binance_mark_price": (config.binance_mark_price_parquet(), config.BAR_MS),
        "binance_spot_klines": (config.binance_spot_klines_parquet(), config.BAR_MS),
        # Phase 8 の入力(F5 = IDX)。5分グリッドなので期待間隔を持つ。
        "binance_index_price": (config.binance_index_price_parquet(), config.BAR_MS),
        # Phase 8 の入力(F4)。**イベント系列なので期待間隔を持たない。**
        # 8h を期待間隔として与えると「欠測」が捏造される(間隔は 1h へ切り替わりうる)。
        "binance_funding_rate": (config.binance_funding_rate_parquet(), None),
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


class ManifestSelectionError(ValueError):
    """対象の指定が不正。**1バイトも書く前に**送出する。"""


def select_datasets(
    names: "Sequence[str] | None", select_all: bool = False
) -> list[str]:
    """書き込む dataset を決める。**fail-closed**(既定は「何も書かない」)。

    - `names` も `select_all` も無い → 送出する(**既定で全件書かない**)
    - 両方 → 送出する(排他)
    - 空の `names` → 送出する(「指定した」のに空、は指定漏れである)
    - 未知の名前 → 送出する
    - 重複 → 順序を保って一意化する(同じ manifest を二度書かない)

    manifest は「どのデータで評価したか」を固定する指紋である。既定で全件を
    上書きすると、**別環境で実行しただけで commit 済みの指紋が消える**。
    """
    known = _datasets()
    if select_all and names is not None:
        raise ManifestSelectionError("--all と --datasets は同時に指定できない")
    if select_all:
        return list(known)
    if names is None:
        raise ManifestSelectionError(
            "対象を指定していない(--datasets NAME ... か --all を明示すること)"
        )
    listed = list(names)
    if not listed:
        raise ManifestSelectionError("--datasets が空である")
    unknown = [n for n in listed if n not in known]
    if unknown:
        raise ManifestSelectionError(
            f"未知の dataset: {unknown}(既知: {sorted(known)})"
        )
    seen: dict[str, None] = {}
    for name in listed:
        seen.setdefault(name, None)
    return list(seen)


def build_manifests(names: "Sequence[str]", require_present: bool) -> list[tuple[str, Path, dict]]:
    """書く前に**全件を組み立てる**。

    `require_present` が真なら、指定された dataset の parquet が無い時点で送出する
    (明示的に頼まれたものが無いなら、他を書き始める前に止まる)。
    偽(`--all`)なら、存在しないものは飛ばす。

    **部分書き換えを残さないため、ここで全部作ってから書く。** 名前の誤り・
    ファイル欠落という現実的な失敗は、1バイトも書かないうちに検出される。
    """
    known = _datasets()
    built: list[tuple[str, Path, dict]] = []
    missing: list[str] = []
    for name in names:
        path, interval_ms = known[name]
        if not path.exists():
            missing.append(f"{name} ({path})")
            continue
        built.append((name, path, dataset_manifest(path, interval_ms)))
    if require_present and missing:
        raise ManifestSelectionError(f"指定された dataset のデータが無い: {missing}")
    return built


def main(argv: "Sequence[str] | None" = None) -> int:
    import argparse

    known = _datasets()
    parser = argparse.ArgumentParser(
        prog="python -m mce.manifest",
        description=(
            "データ資産の manifest 生成。**対象の明示が必須**"
            "(既定で全件を上書きしない)。"
        ),
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--datasets", nargs="+", choices=list(known), metavar="NAME",
        help="この dataset だけ生成する",
    )
    group.add_argument(
        "--all", action="store_true", dest="select_all",
        help="全 dataset を生成する(**明示指定が必要**)",
    )
    args = parser.parse_args(argv)

    try:
        names = select_datasets(args.datasets, args.select_all)
        built = build_manifests(names, require_present=not args.select_all)
    except ManifestSelectionError as exc:
        print(f"error: {exc}\n", file=sys.stderr)
        parser.print_usage(sys.stderr)
        return 2
    if not built:
        print("error: manifest 対象のデータがありません", file=sys.stderr)
        return 2

    # ここまで来たら失敗要因は出尽くしている。**まとめて書く。**
    for name, path, manifest in built:
        out_dir = config.MANIFESTS_DIR
        out_dir.mkdir(parents=True, exist_ok=True)
        out = out_dir / f"{name}_{path.stem}.json"
        out.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        print(f"{name}: -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Phase 8 の入力データ(F1 / F2)の inventory を作る。

**取引系列を組み立てない。** rho / シグナル / 清算発生 / return / PnL を
一切計算しない。ここで出すのは取り込みの会計だけである。

    uv run python -m mce.phase8_inputs --json data/manifests/phase8_inputs_v1.json

1 dataset につき次を記録する:

- **出所**: market path / market_type / cadence / close_time policy
- **dataset 固有の digest**: 公開 zip の SHA-256 から作る環境非依存の指紋
- **時刻規約**: バー開始時刻・グリッド・封印 cutoff
- **重複/欠測の分類**: 完全重複・値の食い違い・所有ファイルによる解決・欠測バー
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

import polars as pl

from mce import config
from mce.backtest.splits import FINAL_OOS_START
from mce.binance_vision import BASE_URL, DATASETS, SOURCE, ledger_path, source_digest
from mce.manifest import dataset_manifest
from mce.normalize_binance import BAR_MS, scan_dataset

UTC = timezone.utc

#: Phase 8 が一級の入力として扱う dataset と、その正規化先(**F1 / F2**)。
#: **この registry は F1/F2 に固定されている**(テストが固定)。後から足した
#: 系列をここへ入れると、既存の `phase8_inputs_v1.json` が別物になってしまう。
PHASE8_INPUTS: dict[str, callable] = {
    "mark_price_5m": config.binance_mark_price_parquet,
    "spot_klines_5m": config.binance_spot_klines_parquet,
}

#: F5(protocol §4.1 の `IDX`)。**別 artifact に出す**ので registry を分ける。
PHASE8_INDEX_INPUTS: dict[str, callable] = {
    "index_price_5m": config.binance_index_price_parquet,
}

#: 5分バー系列の inventory を作れる dataset の全体(参照用)。
_BAR_INPUTS: dict[str, callable] = {**PHASE8_INPUTS, **PHASE8_INDEX_INPUTS}


def gap_report(df: pl.DataFrame) -> dict:
    """5分グリッドに対する**欠測バー**を数える。**埋めない。**"""
    if df.is_empty():
        return {"bars": 0, "expected_bars": 0, "missing_bars": 0, "gap_runs": 0,
                "largest_gap_bars": 0, "largest_gap_start_utc": None}
    ts = df["ts"].sort()
    lo, hi = int(ts.min().timestamp() * 1000), int(ts.max().timestamp() * 1000)
    expected = (hi - lo) // BAR_MS + 1
    values = ts.diff().dt.total_milliseconds().to_list()
    runs = [
        {
            "after_utc": ts[i - 1].isoformat(),
            "resume_utc": ts[i].isoformat(),
            "missing_bars": int(v // BAR_MS - 1),
        }
        for i, v in enumerate(values)
        if v is not None and v > BAR_MS
    ]
    return {
        "bars": df.height,
        "expected_bars": expected,
        "missing_bars": expected - df.height,
        "gap_runs": len(runs),
        "largest_gap_bars": max((r["missing_bars"] for r in runs), default=0),
        # **全件を残す。** 欠測を要約で丸めない。
        "gap_runs_detail": runs,
    }


def inventory(dataset: str, symbol: str = "BTCUSDT") -> dict:
    spec = DATASETS[dataset]
    path = _BAR_INPUTS[dataset](symbol)
    record: dict = {
        "dataset": dataset,
        "symbol": symbol,
        "source": SOURCE,
        "base_url": BASE_URL,
        "market_type": spec.market_type,
        "cadence": spec.cadence,
        "path_template": spec.path_template,
        "close_time_policy": spec.close_time_policy,
        "ledger": ledger_path(dataset, symbol).as_posix(),
        "source_digest": source_digest(dataset, symbol),
        "timestamp_semantics": {
            "field": "open_time",
            "meaning": "bar start; row t covers [ts, ts+5m)",
            "grid_ms": BAR_MS,
            "timezone": "UTC",
            "seal_cutoff": FINAL_OOS_START.isoformat(),
        },
        "normalized_path": path.as_posix(),
        "normalized_present": path.exists(),
    }
    if not path.exists():
        return record
    record["parquet"] = dataset_manifest(path, BAR_MS)
    df = pl.read_parquet(path)
    record["gaps"] = gap_report(df)
    _, accounting = scan_dataset(dataset, symbol)
    for key in (
        "files", "raw_rows", "sealed_rows_dropped", "duplicate_rows_dropped",
        "conflicting_duplicates", "conflicts_resolved_by_owning_file",
        "unresolved_conflicts", "close_time_not_bar_end_rows",
        "close_time_before_open_rows", "mark_stale_bars", "index_stale_bars",
    ):
        if key in accounting:
            record.setdefault("raw_accounting", {})[key] = accounting[key]
    return record


def build(
    symbol: str = "BTCUSDT",
    datasets: "tuple[str, ...] | None" = None,
    name: str = "phase8_inputs_v1",
) -> dict:
    """既定は **F1 / F2 のまま**(既存 artifact を作り直さない)。

    `datasets=()` は「指定なし」ではなく「**空の指定**」として扱い、送出する。
    黙って既定へ落とすと、`--datasets` を値なしで渡した人が F1/F2 の
    artifact を意図せず作ってしまう。
    """
    if datasets is not None and len(tuple(datasets)) == 0:
        raise ValueError("--datasets が空である(既定を使うなら指定そのものを省く)")
    datasets = tuple(datasets) if datasets is not None else tuple(PHASE8_INPUTS)
    unknown = [d for d in datasets if d not in _BAR_INPUTS]
    if unknown:
        raise ValueError(f"未知の dataset: {unknown}")
    return {
        "inventory": name,
        "purpose": "input-data plumbing only; not joined into a trade series; "
                   "no rho, no signals, no liquidation incidence, no returns, no PnL",
        "built_at_utc": datetime.now(UTC).isoformat(),
        "datasets": [inventory(d, symbol) for d in datasets],
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--symbol", default="BTCUSDT")
    ap.add_argument("--json", type=Path, default=None)
    ap.add_argument("--datasets", nargs="*", default=None, choices=list(_BAR_INPUTS),
                    help="既定は F1 / F2(既存 artifact を作り直さないため)")
    ap.add_argument("--name", default="phase8_inputs_v1", help="artifact の名前")
    args = ap.parse_args()
    report = build(args.symbol, args.datasets, args.name)
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                             encoding="utf-8")
    for d in report["datasets"]:
        pq = d.get("parquet", {})
        print(f"{d['dataset']:16s} {d['market_type']:12s} "
              f"rows={pq.get('rows', 0):>7} missing={d.get('gaps', {}).get('missing_bars', 0):>5} "
              f"digest={d['source_digest'].get('digest', '')[:12]} "
              f"policy={d['close_time_policy']}")


if __name__ == "__main__":
    main()

"""Phase 8 の入力データ(F4: funding 決済イベント)の inventory を作る。

**取引系列を組み立てない。** rho / シグナル / 清算発生 / return / PnL を
一切計算しない。ここで出すのは取り込みの会計だけである。

    uv run python -m mce.phase8_funding --json data/manifests/phase8_funding_v1.json

F1 / F2 の inventory(`mce.phase8_inputs`)とは**別の artifact** にする。
向こうは5分バー系列の会計(欠測バー・グリッド)であり、こちらは
**イベント系列**の会計(決済間隔の分布・宣言値との食い違い)だからである。
同じ表に混ぜると「欠測バー」という概念を funding に誤って適用することになる。

記録するもの:

- **出所**: market path / market_type / cadence / path template
- **dataset 固有の digest**: 公開 zip の SHA-256 から作る環境非依存の指紋
- **時刻規約**: `calc_time` = 決済時刻(X4)。**バー開始時刻ではない**
- **封印**: per-target cutoff と実際に落とした行数
- **間隔**: 直前の決済との差から導出した間隔の分布と、dump の宣言値との突き合わせ
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

import polars as pl

from mce import config
from mce.binance_vision import BASE_URL, DATASETS, SOURCE, ledger_path, source_digest
from mce.manifest import dataset_manifest
from mce.normalize_binance import HOUR_MS, scan_dataset, seal_cutoff_for

UTC = timezone.utc

#: Phase 8 が一級の入力として扱うイベント系列と、その正規化先。
PHASE8_EVENT_INPUTS: dict[str, callable] = {
    "funding_rate": config.binance_funding_rate_parquet,
}


def interval_report(df: pl.DataFrame) -> dict:
    """導出した決済間隔の分布。**8h を仮定しない。埋めない。**

    分布は「分に丸めた時間数」で数える(実測の `calc_time` は正時 + 0〜47ms の
    ジッタを持つため、生の ms で数えると全行が別の値になってしまう)。
    丸めるのは**集計の見出しだけ**で、parquet の値は丸めていない。
    """
    if df.is_empty() or "funding_interval_ms" not in df.columns:
        return {"events": 0, "intervals_derived": 0, "distribution_hours": {}}
    observed = df.filter(pl.col("funding_interval_ms").is_not_null())
    buckets: dict[str, int] = {}
    for ms in observed["funding_interval_ms"].to_list():
        hours = round(ms / HOUR_MS, 2)
        key = f"{hours:g}"
        buckets[key] = buckets.get(key, 0) + 1
    declared: dict[str, int] = {}
    for value in df["funding_interval_hours_declared"].to_list():
        declared[str(value)] = declared.get(str(value), 0) + 1
    return {
        "events": df.height,
        "intervals_derived": observed.height,
        "first_event_interval_is_null": bool(df.height - observed.height == 1),
        # **導出**間隔(ts 差)の分布。降順に全件出す(要約で丸めない)。
        "distribution_hours": dict(
            sorted(buckets.items(), key=lambda kv: (-kv[1], kv[0]))
        ),
        # dump が**宣言**している間隔の分布。導出値と別物として並べる。
        "declared_distribution_hours": dict(
            sorted(declared.items(), key=lambda kv: (-kv[1], kv[0]))
        ),
        # **正規化が導出済みの列をそのまま使う。** ここで ms から割り直すと
        # polars の逆数最適化で 8h が 7.999999999999999 になる(normalize_binance
        # の `add_funding_intervals` に同じ注意書きがある)。
        "min_interval_hours": (
            observed["funding_interval_hours"].min() if observed.height else None
        ),
        "max_interval_hours": (
            observed["funding_interval_hours"].max() if observed.height else None
        ),
    }


def inventory(dataset: str, symbol: str = "BTCUSDT") -> dict:
    spec = DATASETS[dataset]
    path = PHASE8_EVENT_INPUTS[dataset](symbol)
    record: dict = {
        "dataset": dataset,
        "symbol": symbol,
        "source": SOURCE,
        "base_url": BASE_URL,
        "market_type": spec.market_type,
        "cadence": spec.cadence,
        "series_kind": spec.series_kind,
        "path_template": spec.path_template,
        "ledger": ledger_path(dataset, symbol).as_posix(),
        "source_digest": source_digest(dataset, symbol),
        "timestamp_semantics": {
            "field": "calc_time",
            "meaning": "funding settlement time (protocol X4: identical to REST fundingTime)",
            "not_a_bar": "no [ts, ts+5m) interval semantics; this is an event series",
            "timezone": "UTC",
            "epoch_unit": "milliseconds",
            "seal_cutoff": seal_cutoff_for(dataset).isoformat(),
        },
        "columns_absent_in_source": {
            # 実測で無かったものを**明示的に記録する**(黙って null 列にしない)
            "mark_price": "not published in the Vision fundingRate dump "
                          "(header is calc_time,funding_interval_hours,last_funding_rate); "
                          "protocol 8.1 MarkPrice(s) must come from another source",
        },
        "normalized_path": path.as_posix(),
        "normalized_present": path.exists(),
    }
    if not path.exists():
        return record
    # **イベント系列なので期待間隔を渡さない**(欠測バーという概念が無い)
    record["parquet"] = dataset_manifest(path, None)
    df = pl.read_parquet(path)
    record["intervals"] = interval_report(df)
    _, accounting = scan_dataset(dataset, symbol)
    for key in (
        "files", "raw_rows", "sealed_rows_dropped", "cutoff", "duplicate_rows_dropped",
        "conflicting_duplicates", "conflicts_resolved_by_owning_file",
        "unresolved_conflicts", "funding_interval_rows_derived",
        "funding_interval_first_event_null", "funding_interval_non_positive_rows",
        "funding_interval_disagrees_with_declared_rows",
        "funding_interval_max_deviation_ms",
    ):
        if key in accounting:
            record.setdefault("raw_accounting", {})[key] = accounting[key]
    return record


def build(symbol: str = "BTCUSDT") -> dict:
    return {
        "inventory": "phase8_funding_v1",
        "purpose": "input-data plumbing only; not joined into a trade series; "
                   "no rho, no signals, no liquidation incidence, no returns, no PnL",
        "built_at_utc": datetime.now(UTC).isoformat(),
        "datasets": [inventory(d, symbol) for d in PHASE8_EVENT_INPUTS],
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--symbol", default="BTCUSDT")
    ap.add_argument("--json", type=Path, default=None)
    args = ap.parse_args()
    report = build(args.symbol)
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                             encoding="utf-8")
    for d in report["datasets"]:
        pq = d.get("parquet", {})
        iv = d.get("intervals", {})
        print(f"{d['dataset']:14s} {d['market_type']:12s} "
              f"events={pq.get('rows', 0):>6} "
              f"digest={d['source_digest'].get('digest', '')[:12]} "
              f"intervals={iv.get('distribution_hours', {})}")


if __name__ == "__main__":
    main()

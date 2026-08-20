"""Phase 8 の入力データ(公式 REST の funding markPrice)の inventory と照合。

**取引系列を組み立てない。** rho / シグナル / 清算発生 / return / PnL を
一切計算しない。ここで出すのは取り込みと照合の会計だけである。

    uv run python -m mce.phase8_funding_rest --json data/manifests/phase8_funding_rest_v1.json

`phase8_funding_v1.json`(Vision 側 = canonical)とは**別の artifact** にする。
既存 artifact も凍結記録も**変更しない**。

照合の向き(§4):

- **canonical は Vision の決済イベント**である。REST は markPrice を供給する
  第2の観測であって、Vision の funding rate を置換するものではない。
- 一致しなかった行は**落とさない**。理由(`match_status`)つきで残す。
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

import polars as pl

from mce import config
from mce.backtest.splits import FINAL_OOS_START
from mce.binance_rest import (
    ALLOWED_PATHS,
    FUNDING_TIME_TOLERANCE_MS,
    MATCH_STATUSES,
    REST_HOST,
    reconcile,
)
from mce.binance_vision import MARKET_TYPE, SOURCE
from mce.manifest import dataset_manifest

UTC = timezone.utc


def mark_price_report(rest: pl.DataFrame) -> dict:
    """markPrice の欠測・非正・parse 不能の会計。**補完しない。**"""
    if rest.is_empty():
        return {"rows": 0}
    counts = (
        rest.group_by("mark_price_status").len().sort("mark_price_status")
    )
    by_status = {row["mark_price_status"]: row["len"] for row in counts.iter_rows(named=True)}
    present = rest.filter(pl.col("mark_price_status") == "present")
    # markPrice が付き始めた境界を**実測して記録する**(推定しない)
    with_mark = rest.filter(pl.col("mark_price").is_not_null())
    without_mark = rest.filter(pl.col("mark_price").is_null())
    return {
        "rows": rest.height,
        "by_status": by_status,
        "rows_with_mark_price": with_mark.height,
        "rows_without_mark_price": without_mark.height,
        "first_ts_with_mark_price": (
            with_mark["rest_funding_time"].min().isoformat() if with_mark.height else None
        ),
        "last_ts_without_mark_price": (
            without_mark["rest_funding_time"].max().isoformat() if without_mark.height else None
        ),
        "min_mark_price": present["mark_price"].min() if present.height else None,
        "max_mark_price": present["mark_price"].max() if present.height else None,
    }


def order_report(rest: pl.DataFrame) -> dict:
    """順序と重複の会計(取得時の停止条件とは独立に、保存物を再検査する)。"""
    if rest.is_empty():
        return {"rows": 0}
    times = [int(t.timestamp() * 1000) for t in rest["rest_funding_time"].to_list()]
    gaps = [b - a for a, b in zip(times, times[1:])]
    return {
        "rows": rest.height,
        "strictly_ascending": all(g > 0 for g in gaps),
        "duplicate_funding_times": len(times) - len(set(times)),
        "min_gap_ms": min(gaps) if gaps else None,
        "max_gap_ms": max(gaps) if gaps else None,
        "rate_types": {
            row["rate_type"]: row["len"]
            for row in rest.group_by("rate_type").len().sort("rate_type").iter_rows(named=True)
        },
    }


def build(symbol: str = "BTCUSDT", write_reconciliation: bool = True) -> dict:
    vision_path = config.binance_funding_rate_parquet(symbol)
    rest_path = config.binance_funding_rate_rest_parquet(symbol)
    recon_path = config.binance_funding_reconciliation_parquet(symbol)

    record: dict = {
        "inventory": "phase8_funding_rest_v1",
        "purpose": "input-data plumbing only; not joined into a trade series; "
                   "no rho, no signals, no liquidation incidence, no returns, no PnL",
        "built_at_utc": datetime.now(UTC).isoformat(),
        "source": SOURCE,
        "market_type": MARKET_TYPE,
        "rest_host": REST_HOST,
        "allowed_paths": sorted(ALLOWED_PATHS),
        "authentication": "none (public endpoint only; no api key, no signature)",
        "canonical_series": {
            "path": vision_path.as_posix(),
            "role": "canonical funding events (Binance Vision monthly/fundingRate)",
            "not_replaced_by_rest": True,
        },
        "rest_series": {
            "path": rest_path.as_posix(),
            "role": "second observation supplying settlement-time markPrice",
            "present": rest_path.exists(),
        },
        "reconciliation": {
            "path": recon_path.as_posix(),
            "tolerance_ms": FUNDING_TIME_TOLERANCE_MS,
            "timestamp_exact_match_assumed": False,
            "statuses": list(MATCH_STATUSES),
        },
        "seal_cutoff": FINAL_OOS_START.isoformat(),
    }
    if not (vision_path.exists() and rest_path.exists()):
        return record

    vision = pl.read_parquet(vision_path)
    rest = pl.read_parquet(rest_path)
    record["rest_series"]["parquet"] = dataset_manifest(rest_path, None)
    record["rest_series"]["mark_price"] = mark_price_report(rest)
    record["rest_series"]["order"] = order_report(rest)

    table, summary = reconcile(vision, rest)
    if write_reconciliation:
        recon_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = recon_path.with_suffix(".parquet.tmp")
        table.write_parquet(tmp)
        tmp.replace(recon_path)
        record["reconciliation"]["parquet"] = dataset_manifest(recon_path, None)
    record["reconciliation"]["summary"] = summary
    record["reconciliation"]["status_counts"] = {
        row["match_status"]: row["len"]
        for row in table.group_by("match_status").len().sort("match_status").iter_rows(named=True)
    }
    record["periods"] = {
        "vision_ts_min": vision["ts"].min().isoformat(),
        "vision_ts_max": vision["ts"].max().isoformat(),
        "rest_ts_min": rest["rest_funding_time"].min().isoformat(),
        "rest_ts_max": rest["rest_funding_time"].max().isoformat(),
    }
    return record


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
    summary = report.get("reconciliation", {}).get("summary")
    if summary:
        print(
            f"vision={summary['vision_events']} rest={summary['rest_events']} "
            f"matched={summary['matched_one_to_one']} "
            f"unmatched_vision={summary['unmatched_vision']} "
            f"unmatched_rest={summary['unmatched_rest']} "
            f"rate_mismatch={summary['rate_mismatch']} "
            f"offsets={summary['offset_distribution_ms']}"
        )
        mark = report["rest_series"]["mark_price"]
        print(f"mark_price: {mark['by_status']} first_with_mark={mark['first_ts_with_mark_price']}")
    else:
        print("REST 系列または Vision 系列が未取得")


if __name__ == "__main__":
    main()

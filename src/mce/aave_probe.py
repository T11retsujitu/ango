"""Aave 履歴レートの**可用性プローブ**(取得可否の確認のみ)。

各世代の代表日と**すべての接合境界の両側**で、履歴 state を実際に引けるかを確かめる。
**戦略 artifact を一切生成しない。** rho も損益も計算しない。

    uv run python -m mce.aave_probe --json experiments/phase8/aave_availability_probe_v1.json
"""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from mce import phase8_prereg as P
from mce.aave_rates import DEFAULT_RPC_ENDPOINTS, GENERATIONS, daily_observation

UTC = timezone.utc

# 代表日 + 接合境界の両側(§27.2 の splice を跨ぐ)
PROBE_DATES: tuple[str, ...] = (
    "2019-12-01",  # V1 稼働前(None を返すべき)
    "2020-01-08",  # V1 genesis
    "2020-06-01",  # V1 期
    "2020-12-02",  # splice 1 の直前(V1)
    "2020-12-03",  # splice 1 ちょうど(V2 へ)
    "2020-12-04",  # splice 1 の直後(V2)
    "2021-06-01",  # V2 期
    "2022-06-01",  # V2 期
    "2023-01-26",  # splice 2 の直前(V2)
    "2023-01-27",  # splice 2 ちょうど(V3 Core へ)
    "2023-01-28",  # splice 2 の直後(V3)
    "2023-06-01",  # V3 期
    "2024-06-01",  # V3 期
    "2025-06-01",  # layer 2 開始日
    "2025-12-31",  # layer 2 終了直前
    "2026-03-30",  # V4 ローンチ日。**V3 のままであるべき**
    "2026-08-01",  # 直近
)


def _window(start: str, days: int) -> tuple[str, ...]:
    d0 = datetime.fromisoformat(start).replace(tzinfo=UTC)
    return tuple((d0 + timedelta(days=i)).date().isoformat() for i in range(days))


# 接合境界の**連続窓**。境界直後の bootstrap 挙動を特徴づけるために使う。
SPLICE_DATES: tuple[str, ...] = _window("2020-11-30", 12) + _window("2023-01-24", 13)


def run(endpoint: str, dates: tuple[str, ...], *, probe_name: str) -> dict:
    cache: dict[int, int] = {}
    rows = []
    for d in dates:
        day = datetime.fromisoformat(d).replace(tzinfo=UTC)
        obs = daily_observation(endpoint, day, cache=cache)
        rows.append(asdict(obs))
    complete = sum(1 for r in rows if r["mean_apr"] is not None)
    # H17: 未初期化 reserve の 0% が平均へ混入した日を明示的に集計する。
    # **値は凍結どおりのまま**で、事実だけを残す。
    anomalies = [
        {"date_utc": r["date_utc"], "generation": r["generation"],
         "uninitialised_reserves": list(r["uninitialised_reserves"]),
         "mean_apr": r["mean_apr"]}
        for r in rows if r["mean_apr"] is not None and r["uninitialised_reserves"]
    ]
    return {
        "probe": probe_name,
        "purpose": "input-data reconstruction availability only; no rho, no signals, no returns",
        "protocol_version": P.PROTOCOL_VERSION,
        "source_fidelity": P.RATE_SOURCE_FIDELITY,
        "frozen_generations": [
            {"name": g.name, "pool": g.pool_address, "rate_word_index": g.rate_word_index,
             "expected_word_count": g.expected_word_count,
             "start": g.start.isoformat(), "end": g.end.isoformat() if g.end else None,
             "tokens": [{"symbol": t.symbol, "address": t.address, "decimals": t.decimals}
                        for t in g.tokens]}
            for g in GENERATIONS
        ],
        "endpoint": endpoint,
        "dates_probed": len(rows),
        "dates_complete": complete,
        "h17_uninitialised_reserve_days": anomalies,
        "observations": rows,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--endpoint", default=DEFAULT_RPC_ENDPOINTS[0])
    ap.add_argument("--json", type=Path, default=None)
    ap.add_argument("--mode", choices=("availability", "splice"), default="availability")
    args = ap.parse_args()
    dates = PROBE_DATES if args.mode == "availability" else SPLICE_DATES
    name = f"aave_{args.mode}_v1"
    report = run(args.endpoint, dates, probe_name=name)
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    for row in report["observations"]:
        mean = row["mean_apr"]
        shown = f"{mean:.6f}" if mean is not None else "None"
        print(f"{row['date_utc']}  {str(row['generation']):13s} "
              f"blk={str(row['block_number']):>9s}  mean_apr={shown:>10s}  {row['note'] or ''}")
    print(f"\n{report['dates_complete']}/{report['dates_probed']} dates complete")
    if report["h17_uninitialised_reserve_days"]:
        print(f"H17: 未初期化 reserve が平均に混入した日 = "
              f"{len(report['h17_uninitialised_reserve_days'])}")


if __name__ == "__main__":
    main()

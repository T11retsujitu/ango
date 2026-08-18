"""Aave 日次レート**入力系列**の再構成(v1.8.4 §27 / §30)。

**入力データのみ。** BTC データへ join しない。rho もシグナルも損益も
Phase 8 の帰結も計算しない。本モジュールは取引 artifact を一切生成しない。

    uv run python -m mce.aave_series --start 2020-01-08 --end 2026-08-18 \
        --out data/phase8/aave_daily_rate_v1.jsonl

生の外部応答は保存しない。各観測は provenance(chain id / block number /
block timestamp / block hash / token address / 生 RAY / 小数 APR / 応答 SHA-256)
だけを残す。reserve list は**内容の SHA-256 と要素数と membership 判定**を残す
(アドレス列そのものはブロックから再取得できるため保存しない)。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import time
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Final

from mce import phase8_prereg as P
from mce.aave_rates import (
    DEFAULT_RPC_ENDPOINTS,
    ETHEREUM_MAINNET_CHAIN_ID,
    DailyObservation,
    RpcError,
    chain_id_of,
    daily_observation,
)

UTC = timezone.utc


#: adapter が「protocol state としての不在」を表すときの文言。
#: これ以外の error は **transport の失敗**であり、観測ではない。
_PROTOCOL_ABSENCE: Final = "空応答("


def classify(row: dict) -> str:
    """1行を `complete` / `missing_by_protocol` / `transport_failure` へ分類する。

    **これは凍結規則の変更ではない。** §30.2 の null は *protocol state* についての
    言明であって、私の取得パイプラインが失敗したことの言明ではない。
    両者を同じ null として系列に載せると、経済的な記録を偽ることになる。
    transport の失敗は**観測として採用せず、再取得する**。
    """
    if row["mean_apr"] is not None:
        return "complete"
    if row["integrity_error"]:
        return "integrity_error"
    if row["generation"] is None:
        return "missing_by_protocol"  # V1 稼働前。取得の問題ではない
    if row["block_number"] is None:
        return "transport_failure"
    lst = row.get("reserve_list")
    if lst is None:
        return "transport_failure"
    if lst["error"] and not lst["error"].startswith(_PROTOCOL_ABSENCE):
        return "transport_failure"
    for comp in row["components"]:
        err = comp["error"]
        if err and not err.startswith(_PROTOCOL_ABSENCE) and "word 数" not in err:
            return "transport_failure"
    return "missing_by_protocol"


def _plain(value):
    """tuple を list へ正規化する。**メモリ上と JSON 上の形を一致させるため**。"""
    if isinstance(value, tuple):
        return [_plain(v) for v in value]
    if isinstance(value, list):
        return [_plain(v) for v in value]
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    return value


def to_record(obs: DailyObservation) -> dict:
    """1日分の観測を系列行へ落とす。**値の加工はしない**(§30.3)。"""
    row = _plain(asdict(obs))
    lst = row.pop("reserve_list")
    row["reserve_list"] = (
        None
        if lst is None
        else {
            "signature": lst["signature"],
            "count": lst["count"],
            "response_sha256": lst["response_sha256"],
            "error": lst["error"],
        }
    )
    return row


def build(
    endpoints: list[str], start: datetime, end: datetime, out: Path, *, attempts: int = 6
) -> dict:
    """日次観測を取得する。**transport の失敗は観測として採用しない。**

    endpoint を巡回して再取得し、それでも通らなければ**系列を書かずに中断**する。
    「取得できなかった日」を「レートが無い日」として記録することを禁じる。
    """
    verified: list[tuple[str, int]] = []
    for ep in endpoints:
        try:
            cid = chain_id_of(ep)
        except RpcError as exc:
            print(f"[skip] {ep}: {exc}")
            continue
        if cid != ETHEREUM_MAINNET_CHAIN_ID:
            print(f"[skip] {ep}: chain id が mainnet ではない: {cid}")
            continue
        verified.append((ep, cid))
    if not verified:
        raise SystemExit("mainnet を返す endpoint が1つも無い")

    caches: dict[str, dict[int, int]] = {ep: {} for ep, _ in verified}
    hints: dict[str, int] = {}
    stats = {"complete": 0, "missing_by_protocol": 0, "integrity_error": 0, "retries": 0}
    out.parent.mkdir(parents=True, exist_ok=True)
    day = start
    with out.open("w", encoding="utf-8") as fh:
        while day <= end:
            row = None
            for attempt in range(attempts):
                ep, cid = verified[attempt % len(verified)]
                obs = daily_observation(
                    ep, day, cache=caches[ep], hint=hints.get(ep), chain_id=cid
                )
                if obs.block_number is not None:
                    hints[ep] = obs.block_number
                candidate = to_record(obs)
                if classify(candidate) != "transport_failure":
                    row = candidate
                    break
                stats["retries"] += 1
                time.sleep(min(8.0, 0.5 * 2**attempt))
            if row is None:
                raise SystemExit(
                    f"{day.date()} を {attempts} 回試しても取得できなかった。"
                    "取得失敗を『レートが無い日』として記録しない(系列を書かずに中断)。"
                )
            stats[classify(row)] = stats.get(classify(row), 0) + 1
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
            day += timedelta(days=1)
    return stats


def merge(parts: list[Path], out: Path) -> list[dict]:
    """分割実行した断片を**日付順**に結合する。重複日と欠番を拒否する。

    `hint` は答えを変えないため(T59)、分割の仕方は系列の内容に影響しない。
    """
    rows: dict[str, dict] = {}
    for part in parts:
        for line in part.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            key = row["date_utc"]
            if key in rows and rows[key] != row:
                raise SystemExit(f"同じ日で内容が食い違う断片がある: {key}")
            rows[key] = row
    ordered = [rows[k] for k in sorted(rows)]
    first = datetime.fromisoformat(ordered[0]["date_utc"]).date()
    last = datetime.fromisoformat(ordered[-1]["date_utc"]).date()
    if (last - first).days + 1 != len(ordered):
        raise SystemExit("日付に欠番がある(暦日が連続していない)")
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as fh:
        for row in ordered:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    return ordered


def manifest(rows: list[dict], series_path: Path) -> dict:
    """版管理へ入れる manifest。**系列の値そのものは入れない**。"""
    complete = [r for r in rows if r["mean_apr"] is not None]
    missing = [r for r in rows if r["mean_apr"] is None and not r["integrity_error"]]
    integrity = [r for r in rows if r["integrity_error"]]
    by_gen: dict[str, dict[str, int]] = {}
    for r in rows:
        g = by_gen.setdefault(str(r["generation"]), {"days": 0, "complete": 0})
        g["days"] += 1
        g["complete"] += 1 if r["mean_apr"] is not None else 0
    aprs = [r["mean_apr"] for r in complete]
    return {
        "series": "aave_daily_rate_v1",
        "purpose": "input-data reconstruction only; not joined to BTC data; "
                   "no rho, no signals, no returns, no PnL, no Phase-8 outcome",
        "protocol_version": P.PROTOCOL_VERSION,
        "source_of_truth": P.RATE_SOURCE_OF_TRUTH,
        "access_route": P.RATE_ACCESS_ROUTE,
        "access_provider_role": P.RATE_ACCESS_PROVIDER_ROLE,
        "chain_id": P.RATE_CHAIN_ID,
        "completeness_rule": P.RATE_COMPLETENESS_RULE,
        "value_treatment": P.RATE_VALUE_TREATMENT,
        "interpolation": P.RATE_INTERPOLATION,
        "source_fidelity": P.RATE_SOURCE_FIDELITY,
        "assets": list(P.RATE_ASSETS),
        "snapshot_hour_utc": P.RATE_SNAPSHOT_HOUR_UTC,
        "path": str(series_path),
        "sha256": hashlib.sha256(series_path.read_bytes()).hexdigest(),
        "bytes": series_path.stat().st_size,
        "first_date_utc": rows[0]["date_utc"],
        "last_date_utc": rows[-1]["date_utc"],
        "days_total": len(rows),
        "days_complete": len(complete),
        "days_missing": len(missing),
        "days_transport_failure": sum(1 for r in rows if classify(r) == "transport_failure"),
        "days_integrity_error": len(integrity),
        "coverage": round(len(complete) / len(rows), 6),
        "by_generation": by_gen,
        "mean_apr_min": min(aprs) if aprs else None,
        "mean_apr_max": max(aprs) if aprs else None,
        "missing_days": [
            {"date_utc": r["date_utc"], "generation": r["generation"],
             "missing_reserves": r["missing_reserves"], "note": r["note"]}
            for r in missing
        ],
        "integrity_error_days": [
            {"date_utc": r["date_utc"], "integrity_error": r["integrity_error"]}
            for r in integrity
        ],
        "note": "生の外部応答は保存していない。各行は provenance("
                "chain id / block number / block timestamp / block hash / token address / "
                "生 RAY / 小数 APR / 応答 SHA-256 / reserve list の SHA-256 と要素数)のみ。",
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--endpoint", action="append", default=None,
                    help="複数指定可。transport 失敗時に巡回する")
    ap.add_argument("--start")
    ap.add_argument("--end")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--merge", nargs="*", type=Path, default=None,
                    help="断片を結合する(取得はしない)")
    ap.add_argument("--manifest", type=Path, default=None)
    args = ap.parse_args()
    if args.merge:
        rows = merge(sorted(args.merge), args.out)
        print(f"merged {len(rows)} rows -> {args.out}")
    else:
        start = datetime.fromisoformat(args.start).replace(tzinfo=UTC)
        end = datetime.fromisoformat(args.end).replace(tzinfo=UTC)
        eps = args.endpoint or list(DEFAULT_RPC_ENDPOINTS)
        stats = build(eps, start, end, args.out)
        digest = hashlib.sha256(args.out.read_bytes()).hexdigest()
        print(f"{args.start}..{args.end}  {stats}  sha256={digest}  "
              f"protocol={P.PROTOCOL_VERSION}")
        rows = [json.loads(x) for x in args.out.read_text(encoding="utf-8").splitlines() if x]
    if args.manifest:
        man = manifest(rows, args.out)
        args.manifest.parent.mkdir(parents=True, exist_ok=True)
        args.manifest.write_text(json.dumps(man, ensure_ascii=False, indent=2) + "\n",
                                 encoding="utf-8")
        print(f"{man['days_complete']}/{man['days_total']} complete "
              f"({man['coverage']:.4f}), missing={man['days_missing']}, "
              f"integrity={man['days_integrity_error']}")


if __name__ == "__main__":
    main()

"""P1 / P2 — mark 価格の**入力可用性プローブ**(公開 REST への読み取りのみ)。

    GET /fapi/v1/markPriceKlines   (public。認証不要)

**入力データの可用性だけを見る。** 清算発生・rho・シグナル・return・PnL を
一切計算しない。**canonical dataset へ REST 値を併合しない。**

対象:

- **P1**: Vision の markPriceKlines が欠落している全区間。
  区間の前後に**重複対照窓**を付けて要求し、Vision と一致するかを確かめる。
- **P2**: `mark_samples == 0` のバー(前値横引き)。同じやり方で確かめる。

**やらないこと(明示)**:

- 補間しない
- index / premium から mark を合成しない
- **前値の横引きを「バー内の不利側 mark 経路」の証拠として扱わない**
  (横引きは「更新が無かった」ことしか意味しない)

分類:

- ``candidate_deterministic_repair``  : 欠落バーを全て復元し、重複窓で完全一致
- ``mark_path_unobservable``          : REST が応答したが復元できない/不一致
- ``probe_blocked_by_egress``         : **REST 自体に到達できない**。
  これは **source についての所見ではない**。自分の経路が塞がれているだけである。
  この場合に ``mark_path_unobservable`` と分類してはならない。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Final

import polars as pl

from mce import config
from mce.normalize_binance import BAR_MS

UTC = timezone.utc

REST_HOST: Final = "https://fapi.binance.com"
REST_PATH: Final = "/fapi/v1/markPriceKlines"
SYMBOL: Final = "BTCUSDT"
INTERVAL: Final = "5m"
REST_MAX_LIMIT: Final = 1500

#: 欠落区間の前後に付ける重複対照窓(本数)。Vision に実在する行と突き合わせる。
OVERLAP_BARS: Final = 12

#: 地域制限の応答に現れる語。**source の所見ではなく経路の所見**として扱う。
_EGRESS_BLOCK_MARKERS: Final = ("restricted location", "Eligibility", "eligibility")

CLASS_REPAIR: Final = "candidate_deterministic_repair"
CLASS_UNOBSERVABLE: Final = "mark_path_unobservable"
CLASS_BLOCKED: Final = "probe_blocked_by_egress"


class RestUnreachable(RuntimeError):
    """REST に到達できない。**欠測の証拠として使わない。**"""


@dataclass
class ProbeResult:
    kind: str  # "gap" / "stale"
    requested_start_ms: int
    requested_end_ms: int
    requested_start_utc: str
    requested_end_utc: str
    target_open_times: list[int]  # 復元したい(または確かめたい)バー
    overlap_open_times: list[int]  # Vision に実在し対照に使うバー
    returned_open_times: list[int] = field(default_factory=list)
    gap_bars_recovered: int = 0
    overlap_rows_compared: int = 0
    overlap_rows_exact: int = 0
    overlap_max_abs_diff: float | None = None
    response_sha256: str | None = None
    http_note: str | None = None
    retrieved_at_utc: str = ""
    classification: str = ""


def _get(url: str, timeout: int = 40) -> tuple[str, str]:
    proc = subprocess.run(
        ["curl", "-s", "--max-time", str(timeout), url], capture_output=True, text=True
    )
    body = proc.stdout
    if not body.strip():
        raise RestUnreachable("空応答")
    return body, hashlib.sha256(body.encode()).hexdigest()


def _classify_body(body: str) -> str | None:
    """本文が**経路の遮断**を示していれば理由を返す。source の所見ではない。"""
    if any(m in body for m in _EGRESS_BLOCK_MARKERS):
        return "geo/eligibility restriction on this egress"
    stripped = body.lstrip()
    if stripped.startswith("<"):
        return "HTML response (redirect or block page), not JSON"
    return None


def fetch_window(start_ms: int, end_ms: int) -> tuple[list[list], str, str | None]:
    """1回の GET で窓を取る。戻り値 `(klines, digest, block_reason)`。"""
    bars = (end_ms - start_ms) // BAR_MS + 1
    if bars > REST_MAX_LIMIT:
        raise ValueError(f"窓が REST の上限を超える: {bars} > {REST_MAX_LIMIT}")
    url = (
        f"{REST_HOST}{REST_PATH}?symbol={SYMBOL}&interval={INTERVAL}"
        f"&startTime={start_ms}&endTime={end_ms}&limit={REST_MAX_LIMIT}"
    )
    body, digest = _get(url)
    reason = _classify_body(body)
    if reason is not None:
        return [], digest, reason
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return [], digest, "non-JSON response"
    if isinstance(payload, dict):
        return [], digest, f"error payload: code={payload.get('code')}"
    return payload, digest, None


def _vision_frame() -> pl.DataFrame:
    return pl.read_parquet(config.binance_mark_price_parquet()).sort("ts")


def _ms(ts) -> int:
    return int(ts.timestamp() * 1000)


def probe_interval(
    kind: str,
    target_open_times: list[int],
    vision: dict[int, tuple[float, float, float, float]],
    overlap_bars: int = OVERLAP_BARS,
) -> ProbeResult:
    """1区間を要求し、復元本数と重複窓の一致を測る。"""
    lo, hi = min(target_open_times), max(target_open_times)
    start = lo - overlap_bars * BAR_MS
    end = hi + overlap_bars * BAR_MS
    overlap = [t for t in range(start, end + BAR_MS, BAR_MS)
               if t in vision and t not in set(target_open_times)]
    result = ProbeResult(
        kind=kind,
        requested_start_ms=start, requested_end_ms=end,
        requested_start_utc=datetime.fromtimestamp(start / 1000, UTC).isoformat(),
        requested_end_utc=datetime.fromtimestamp(end / 1000, UTC).isoformat(),
        target_open_times=list(target_open_times), overlap_open_times=overlap,
        retrieved_at_utc=datetime.now(UTC).isoformat(),
    )
    try:
        klines, digest, reason = fetch_window(start, end)
    except RestUnreachable as exc:
        result.classification = CLASS_BLOCKED
        result.http_note = str(exc)
        return result
    result.response_sha256 = digest
    if reason is not None:
        # **source の所見ではない。** 経路が塞がれているだけである。
        result.classification = CLASS_BLOCKED
        result.http_note = reason
        return result

    returned = {int(k[0]): (float(k[1]), float(k[2]), float(k[3]), float(k[4])) for k in klines}
    result.returned_open_times = sorted(returned)
    targets = set(target_open_times)
    result.gap_bars_recovered = sum(1 for t in targets if t in returned)

    diffs = []
    exact = 0
    for t in overlap:
        if t not in returned:
            continue
        result.overlap_rows_compared += 1
        rest_row, vis_row = returned[t], vision[t]
        if rest_row == vis_row:
            exact += 1
        diffs.append(max(abs(a - b) for a, b in zip(rest_row, vis_row)))
    result.overlap_rows_exact = exact
    result.overlap_max_abs_diff = max(diffs) if diffs else None

    full_recovery = result.gap_bars_recovered == len(targets)
    overlap_ok = (
        result.overlap_rows_compared > 0
        and result.overlap_rows_exact == result.overlap_rows_compared
    )
    result.classification = (
        CLASS_REPAIR if (full_recovery and overlap_ok) else CLASS_UNOBSERVABLE
    )
    return result


def gap_targets(vision: pl.DataFrame) -> list[list[int]]:
    """Vision に無いバーの連続塊(P1)。"""
    ts = [_ms(t) for t in vision["ts"].to_list()]
    runs = []
    for prev, nxt in zip(ts, ts[1:]):
        if nxt - prev > BAR_MS:
            runs.append(list(range(prev + BAR_MS, nxt, BAR_MS)))
    return runs


def stale_targets(vision: pl.DataFrame) -> list[list[int]]:
    """`mark_samples == 0` の連続塊(P2)。"""
    stale = [_ms(t) for t in vision.filter(pl.col("mark_samples") == 0)["ts"].to_list()]
    runs: list[list[int]] = []
    for t in stale:
        if runs and t - runs[-1][-1] == BAR_MS:
            runs[-1].append(t)
        else:
            runs.append([t])
    return runs


def run() -> dict:
    vision = _vision_frame()
    lookup = {
        _ms(r["ts"]): (r["mark_open"], r["mark_high"], r["mark_low"], r["mark_close"])
        for r in vision.iter_rows(named=True)
    }
    results = [probe_interval("gap", t, lookup) for t in gap_targets(vision)]
    results += [probe_interval("stale", t, lookup) for t in stale_targets(vision)]
    counts: dict[str, int] = {}
    for r in results:
        counts[r.classification] = counts.get(r.classification, 0) + 1
    return {
        "probe": "mark_gap_probe_v1",
        "purpose": "input availability only; REST values are NOT merged into the "
                   "canonical dataset; no liquidation incidence, no rho, no signals, "
                   "no returns, no PnL",
        "endpoint": REST_HOST + REST_PATH,
        "symbol": SYMBOL,
        "interval": INTERVAL,
        "overlap_bars": OVERLAP_BARS,
        "merged_into_canonical_dataset": False,
        "refusals": [
            "no interpolation",
            "no synthesis of mark from index/premium",
            "carry-forward is not evidence of the intrabar adverse mark path",
        ],
        "built_at_utc": datetime.now(UTC).isoformat(),
        "classification_counts": counts,
        "intervals": [r.__dict__ for r in results],
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--json", type=Path, default=None)
    args = ap.parse_args()
    report = run()
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                             encoding="utf-8")
    for r in report["intervals"]:
        print(f"{r['kind']:6s} {r['requested_start_utc'][:16]} .. {r['requested_end_utc'][:16]}  "
              f"targets={len(r['target_open_times']):>5} recovered={r['gap_bars_recovered']:>5} "
              f"overlap={r['overlap_rows_exact']}/{r['overlap_rows_compared']:<4} "
              f"{r['classification']}  {r['http_note'] or ''}")
    print("\n" + json.dumps(report["classification_counts"], ensure_ascii=False))


if __name__ == "__main__":
    main()

"""Phase 8 — funding 決済 mark の **proxy 誤差の materiality 分析**。

    uv run python -m mce.funding_mark_materiality \
        --json data/manifests/phase8_funding_mark_materiality_v1.json

**入力 proxy の誤差評価である。strategy PnL ではない。**
建玉も数量も保有期間も想定していない。ここで出す「1 BTC あたりの funding
cashflow 差」は、**同じ1回の決済を exact な REST mark で計算した場合と
proxy(`mark_open`)で計算した場合の差**であって、戦略の損益ではない。

rho / シグナル / strategy return / PnL / Layer 1/2/3 を計算しない。
canonical dataset を変更しない。**v1.8.6 を適用も凍結もしない。**

---

## 誤差の定義(**結果を見る前に固定した**)

    price_error   = mark_open − rest_mark                     [USDT]
    bps_error     = (mark_open / rest_mark − 1) × 10,000      [bps]
    cashflow_diff = (mark_open − rest_mark) × funding_rate    [USDT / BTC / 決済]
    notional_ret  = funding_rate × (mark_open / rest_mark − 1) [無次元]

`cashflow_diff` は §8.1 の `q · MarkPrice(s) · f(s)` を `q = 1 BTC` で評価した
ときの差である(`q` を掛ける前の単位誤差)。`notional_ret` はその誤差を
名目に対する比率へ直したものである。

## 層別も**結果を見る前に固定した**

- **年**: 決済時刻の UTC 年
- **volatility 帯**: そのバー自身の `(mark_high − mark_low) / mark_open` を bps にし、
  `[0,10) / [10,25) / [25,50) / [50,100) / [100,∞)` の5帯に切る。
  **分位点で切らない**(分位で切ると帯の定義が結果に依存する)
- **funding rate の符号**: `positive` / `negative` / `zero`

## 比較は**原文の Decimal** で行う

`mark_open` は raw zip の CSV テキスト、`rest_mark` は公開 REST の文字列、
`funding_rate` は Vision fundingRate dump の CSV テキストから読む。
**正規化 parquet(Float64)を数値の経路に入れない。**
"""

from __future__ import annotations

import argparse
import csv
import json
import zipfile
from datetime import datetime, timezone
from decimal import Decimal, getcontext
from pathlib import Path
from typing import Final, Mapping, Sequence

from mce.backtest.splits import FINAL_OOS_START, phase8_layer
from mce.binance_vision import DEFAULT_SYMBOL, raw_dir
from mce.funding_mark_probe import (
    MARK_GAP_PROBE_PATH,
    PRIMARY_USABLE_STATUSES,
    bar_status,
    fetch_rest_mark_texts,
    floor_5m,
    load_funding_events,
    load_mark_bar_text,
)

UTC = timezone.utc
getcontext().prec = 60

#: volatility 帯の上限(bps)。**事前に固定。分位点で切らない。**
VOLATILITY_BANDS_BPS: Final = (10, 25, 50, 100)
VOLATILITY_BAND_LABELS: Final = (
    "[0,10)bps", "[10,25)bps", "[25,50)bps", "[50,100)bps", "[100,inf)bps",
)
BPS: Final = Decimal(10_000)


def load_funding_rate_text(
    symbol: str = DEFAULT_SYMBOL, source_dir: Path | None = None
) -> dict[int, str]:
    """Vision fundingRate dump から `calc_time -> last_funding_rate の原文`。

    **parquet(Float64)を読まない。** 誤差に掛かる係数なので、原文のまま扱う。
    """
    source_dir = source_dir or raw_dir("funding_rate", symbol)
    cutoff_ms = int(FINAL_OOS_START.timestamp() * 1000)
    out: dict[int, str] = {}
    for path in sorted(source_dir.glob("*.zip")):
        with zipfile.ZipFile(path) as z:
            names = [n for n in z.namelist() if n.lower().endswith(".csv")]
            text = z.read(names[0]).decode()
        for row in csv.reader(text.splitlines()):
            if not row or row[0].strip().lower() == "calc_time":
                continue
            ts_ms = int(row[0])
            if ts_ms >= cutoff_ms:
                continue  # 封印域は読まない
            out[ts_ms] = row[2]
    return out


def volatility_band(bar: Mapping) -> str:
    """バー自身の相対値幅(bps)から帯を決める。**事前に固定した境界**。"""
    if "mark_high_text" not in bar or "mark_low_text" not in bar:
        return "undefined"  # 帯を推測しない
    high = Decimal(bar["mark_high_text"])
    low = Decimal(bar["mark_low_text"])
    open_ = Decimal(bar["mark_open_text"])
    if open_ == 0:
        return "undefined"
    width = (high - low) / open_ * BPS
    for limit, label in zip(VOLATILITY_BANDS_BPS, VOLATILITY_BAND_LABELS):
        if width < limit:
            return label
    return VOLATILITY_BAND_LABELS[-1]


def funding_sign(rate: Decimal) -> str:
    return "positive" if rate > 0 else ("negative" if rate < 0 else "zero")


def summarise(values: Sequence[Decimal]) -> dict:
    """nearest-rank の分位点。**両側の裾を必ず出す。**

    符号付きの量で上側だけを出すと、負の裾が成果物から見えなくなる。
    """
    if not values:
        return {"n": 0, "min": None, "p01": None, "p05": None, "p50": None,
                "p90": None, "p95": None, "p99": None, "max": None,
                "sum": None, "sum_abs": None, "mean": None}
    ordered = sorted(values)
    n = len(ordered)

    def at(q: float) -> str:
        rank = max(1, min(n, int(-(-q * n // 1))))  # ceil(q*n)
        return str(ordered[rank - 1])

    total = sum(values, Decimal(0))
    return {
        "n": n,
        "min": str(ordered[0]),
        "p01": at(0.01), "p05": at(0.05), "p50": at(0.50),
        "p90": at(0.90), "p95": at(0.95), "p99": at(0.99),
        "max": str(ordered[-1]),
        "sum": str(total),
        "sum_abs": str(sum((abs(v) for v in values), Decimal(0))),
        "mean": str(total / n),
    }


def _bucket(rows: Sequence[Mapping], key: str, field: str) -> dict:
    out: dict[str, dict] = {}
    groups: dict[str, list[Decimal]] = {}
    exact: dict[str, int] = {}
    for row in rows:
        g = row[key]
        groups.setdefault(g, []).append(row[field])
        exact[g] = exact.get(g, 0) + (1 if row["exact"] else 0)
    for g, values in sorted(groups.items()):
        out[g] = {
            "events": len(values),
            "exact_matches": exact[g],
            "mismatches": len(values) - exact[g],
            **summarise(values),
        }
    return out


#: 感度分析で並べる候補。**どれも primary にしない。**
#: 「規約を選び直せば exact になったのではないか」への反証のためだけに数える。
CANDIDATE_RULES: Final = (
    ("same_bar_open", 0, "mark_open_text"),        # ← primary
    ("same_bar_close", 0, "mark_close_text"),
    ("same_bar_high", 0, "mark_high_text"),
    ("same_bar_low", 0, "mark_low_text"),
    ("previous_bar_open", -1, "mark_open_text"),
    ("previous_bar_close", -1, "mark_close_text"),
    ("next_bar_open", 1, "mark_open_text"),
)
PRIMARY_RULE_NAME: Final = "same_bar_open"
#: 0.1 刻みに貼り付いた値かどうかの判定(誤差の説明のための層別。判定には使わない)
TICK_QUANTUM: Final = Decimal("0.1")


def candidate_sensitivity(
    events: Sequence[Mapping],
    rest_marks: Mapping[int, str],
    bars: Mapping[int, dict],
) -> dict:
    """**「別の規約なら exact だったのでは」への反証**を数える。

    **この結果で primary を選び直さない。** primary は `same_bar_open` に
    固定されており、ここで一致数が並ぶことは選択の材料ではない
    (§ v1.8.6 草案: OR 規則・最近傍選択はいずれも採用しない)。
    """
    counts = {name: 0 for name, _, _ in CANDIDATE_RULES}
    considered = 0
    primary_hit: set[int] = set()
    prev_close_hit: set[int] = set()
    tick_rows = {"tick_pinned": [0, 0], "full_precision": [0, 0]}  # [exact, total]

    for event in events:
        ft = int(event["funding_time_ms"])
        rest_text = rest_marks.get(ft)
        if rest_text is None:
            continue
        bar_ms = floor_5m(ft)
        if bars.get(bar_ms) is None:
            continue
        considered += 1
        rest = Decimal(rest_text)
        for name, shift, field in CANDIDATE_RULES:
            other = bars.get(bar_ms + shift * 300_000)
            if other is None or field not in other:
                continue
            if Decimal(other[field]) == rest:
                counts[name] += 1
                if name == PRIMARY_RULE_NAME:
                    primary_hit.add(ft)
                elif name == "previous_bar_close":
                    prev_close_hit.add(ft)
        proxy = Decimal(bars[bar_ms]["mark_open_text"])
        bucket = "tick_pinned" if proxy % TICK_QUANTUM == 0 else "full_precision"
        tick_rows[bucket][1] += 1
        tick_rows[bucket][0] += 1 if proxy == rest else 0

    union = len(primary_hit | prev_close_hit)
    return {
        "considered": considered,
        "exact_matches_by_rule": counts,
        "primary_rule": PRIMARY_RULE_NAME,
        "primary_is_best": all(
            counts[PRIMARY_RULE_NAME] >= v for k, v in counts.items()
        ),
        "union_open_or_previous_close": union,
        "union_is_not_the_primary_score": True,
        "explained_by_no_rule": considered - union,
        "by_tick_class": {
            name: {"exact": v[0], "events": v[1]} for name, v in tick_rows.items()
        },
        "note": "counted only to refute 'a different bar rule would have been exact'; "
                "the primary candidate is NOT re-selected from these counts, "
                "and the union is NOT reported as the primary's score",
    }


def analyse(
    events: Sequence[Mapping],
    rest_marks: Mapping[int, str],
    rates: Mapping[int, str],
    bars: Mapping[int, dict],
    probe: Mapping | None = None,
) -> dict:
    """REST mark がある決済について proxy 誤差を Decimal で再計算する。"""
    rows: list[dict] = []
    skipped: list[dict] = []

    for event in events:
        ft = int(event["funding_time_ms"])
        rest_text = rest_marks.get(ft)
        if rest_text is None:
            continue
        bar_ms = floor_5m(ft)
        bar = bars.get(bar_ms)
        status = bar_status(bar_ms, bars, probe)
        when = datetime.fromtimestamp(ft / 1000, UTC)
        if bar is None or status not in PRIMARY_USABLE_STATUSES:
            skipped.append({
                "funding_time_utc": when.isoformat(),
                "funding_time_ms": ft,
                "mark_path_status": status,
                "mark_bar_present": bar is not None,
                "reason": "primary candidate unusable; error is undefined, not zero",
            })
            continue
        rate_text = rates.get(ft)
        if rate_text is None:
            skipped.append({
                "funding_time_utc": when.isoformat(),
                "funding_time_ms": ft,
                "reason": "funding rate text absent in the Vision dump",
            })
            continue

        proxy = Decimal(bar["mark_open_text"])
        rest = Decimal(rest_text)
        rate = Decimal(rate_text)
        price_error = proxy - rest
        relative = (proxy / rest - 1) if rest != 0 else Decimal(0)
        rows.append({
            "funding_time_ms": ft,
            "funding_time_utc": when.isoformat(),
            "mark_bar_open_ms": bar_ms,
            "offset_into_bar_ms": ft - bar_ms,
            "year": str(when.year),
            "layer": phase8_layer(when),
            "volatility_band": volatility_band(bar),
            "funding_sign": funding_sign(rate),
            "mark_path_status": status,
            "mark_samples": bar["mark_samples"],
            "proxy_text": bar["mark_open_text"],
            "rest_text": rest_text,
            "funding_rate_text": rate_text,
            "exact": proxy == rest,
            # --- 事前に固定した4つの誤差量 ---
            "price_error": price_error,
            "abs_price_error": abs(price_error),
            "bps_error": relative * BPS,
            "abs_bps_error": abs(relative * BPS),
            "cashflow_diff_per_btc": price_error * rate,
            "abs_cashflow_diff_per_btc": abs(price_error * rate),
            "notional_return_diff": rate * relative,
            "abs_notional_return_diff": abs(rate * relative),
        })

    exact_count = sum(1 for r in rows if r["exact"])
    worst = sorted(rows, key=lambda r: r["abs_price_error"], reverse=True)
    worst_cashflow = sorted(rows, key=lambda r: r["abs_cashflow_diff_per_btc"], reverse=True)

    def provenance(row: Mapping) -> dict:
        return {
            "funding_time_utc": row["funding_time_utc"],
            "funding_time_ms": row["funding_time_ms"],
            "mark_bar_open_ms": row["mark_bar_open_ms"],
            "offset_into_bar_ms": row["offset_into_bar_ms"],
            "layer": row["layer"],
            "year": row["year"],
            "volatility_band": row["volatility_band"],
            "funding_sign": row["funding_sign"],
            "mark_path_status": row["mark_path_status"],
            "mark_samples": row["mark_samples"],
            "proxy_mark_open_text": row["proxy_text"],
            "rest_mark_price_text": row["rest_text"],
            "funding_rate_text": row["funding_rate_text"],
            "price_error": str(row["price_error"]),
            "bps_error": str(row["bps_error"]),
            "cashflow_diff_per_btc": str(row["cashflow_diff_per_btc"]),
            "notional_return_diff": str(row["notional_return_diff"]),
        }

    metrics = {}
    for field in ("price_error", "abs_price_error", "bps_error", "abs_bps_error",
                  "cashflow_diff_per_btc", "abs_cashflow_diff_per_btc",
                  "notional_return_diff", "abs_notional_return_diff"):
        metrics[field] = summarise([r[field] for r in rows])

    return {
        "compared_events": len(rows),
        "exact_matches": exact_count,
        "mismatches": len(rows) - exact_count,
        "exact_match_rate": f"{exact_count}/{len(rows)}" if rows else "0/0",
        "skipped": skipped,
        "metrics": metrics,
        "cumulative_absolute_error": {
            "price_usdt": metrics["abs_price_error"]["sum"],
            "bps": metrics["abs_bps_error"]["sum"],
            "cashflow_per_btc_usdt": metrics["abs_cashflow_diff_per_btc"]["sum"],
            "notional_return": metrics["abs_notional_return_diff"]["sum"],
            "note": "sum of |error| over the compared settlements; "
                    "NOT a strategy PnL and not a position-weighted quantity",
        },
        "signed_net_error": {
            "price_usdt": metrics["price_error"]["sum"],
            "cashflow_per_btc_usdt": metrics["cashflow_diff_per_btc"]["sum"],
            "notional_return": metrics["notional_return_diff"]["sum"],
            "note": "signed sums are reported so that cancellation is visible; "
                    "cancellation across settlements is NOT a reason to call the proxy exact",
        },
        "by_year": _bucket(rows, "year", "abs_bps_error"),
        "by_year_cashflow": _bucket(rows, "year", "abs_cashflow_diff_per_btc"),
        "by_volatility_band": _bucket(rows, "volatility_band", "abs_bps_error"),
        "by_funding_sign": _bucket(rows, "funding_sign", "abs_bps_error"),
        "by_funding_sign_cashflow": _bucket(rows, "funding_sign", "cashflow_diff_per_btc"),
        "by_layer": _bucket(rows, "layer", "abs_bps_error"),
        "worst_by_absolute_price_error": [provenance(r) for r in worst[:25]],
        "worst_by_absolute_cashflow_diff": [provenance(r) for r in worst_cashflow[:25]],
        "all_mismatch_provenance": [provenance(r) for r in rows if not r["exact"]],
    }


def build(symbol: str = DEFAULT_SYMBOL, transport=None) -> dict:
    if not MARK_GAP_PROBE_PATH.exists():
        raise FileNotFoundError(f"mark gap probe が見つからない: {MARK_GAP_PROBE_PATH}")
    probe = json.loads(MARK_GAP_PROBE_PATH.read_text(encoding="utf-8"))
    events = load_funding_events(symbol)
    bars = load_mark_bar_text(symbol)
    rates = load_funding_rate_text(symbol)
    rest_marks, rest_pages = fetch_rest_mark_texts(symbol, transport)
    result = analyse(events, rest_marks, rates, bars, probe)
    result["candidate_sensitivity"] = candidate_sensitivity(events, rest_marks, bars)
    return {
        "analysis": "phase8_funding_mark_materiality_v1",
        "purpose": "input-proxy error assessment only; NOT a strategy PnL; "
                   "no rho, no signals, no strategy return, no layer execution; "
                   "v1.8.6 is neither applied nor frozen by this analysis",
        "built_at_utc": datetime.now(UTC).isoformat(),
        "symbol": symbol,
        "seal_cutoff": FINAL_OOS_START.isoformat(),
        "definitions": {
            "price_error": "mark_open - rest_mark  [USDT]",
            "bps_error": "(mark_open / rest_mark - 1) * 10000  [bps]",
            "cashflow_diff_per_btc": "(mark_open - rest_mark) * funding_rate  "
                                     "[USDT per BTC per settlement]",
            "notional_return_diff": "funding_rate * (mark_open / rest_mark - 1)  "
                                    "[dimensionless]",
            "volatility_band": "(mark_high - mark_low) / mark_open in bps, "
                               "fixed cut points 10/25/50/100 (not quantiles)",
            "comparison": "Decimal on source text on all three inputs "
                          "(mark_open, rest markPrice, funding rate); no float in the path",
            # **推定方式を明記する。** 書かないと、線形補間を既定にする再現者が
            # 別の数字を出して「一致しない」と読む。
            "percentile_method": "nearest-rank: index = ceil(q * n) - 1 on the "
                                 "ascending order; NOT linear interpolation",
        },
        "refusals": [
            "the proxy is not described as exact",
            "no reclassification of mismatches as within-tolerance matches",
            "no per-event nearest-candidate selection and no OR rule",
            "no interpolation of missing or stale marks",
            "signed cancellation is not treated as evidence of exactness",
            "not a strategy PnL",
        ],
        "sources": {
            "funding_events_canonical": "data/normalized/binance/funding_rate_BTCUSDT.parquet",
            "mark_open_text": raw_dir("mark_price_5m", symbol).as_posix(),
            "funding_rate_text": raw_dir("funding_rate", symbol).as_posix(),
            "rest_mark_price": "public GET /fapi/v1/fundingRate (markPrice text, in memory only)",
            "mark_gap_probe": MARK_GAP_PROBE_PATH.as_posix(),
        },
        "rest_page_provenance": rest_pages,
        **result,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--symbol", default=DEFAULT_SYMBOL)
    ap.add_argument("--json", type=Path, default=None)
    args = ap.parse_args()
    report = build(args.symbol)
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                             encoding="utf-8")
    m = report["metrics"]
    print(f"compared={report['compared_events']} exact={report['exact_match_rate']} "
          f"mismatch={report['mismatches']} skipped={len(report['skipped'])}")
    print(f"  |bps|      p50={m['abs_bps_error']['p50']} p95={m['abs_bps_error']['p95']} "
          f"max={m['abs_bps_error']['max'][:12]}")
    print(f"  |cashflow| p95={m['abs_cashflow_diff_per_btc']['p95'][:12]} "
          f"max={m['abs_cashflow_diff_per_btc']['max'][:12]} "
          f"sum={report['cumulative_absolute_error']['cashflow_per_btc_usdt'][:14]}")


if __name__ == "__main__":
    main()

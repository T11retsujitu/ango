"""Phase 8 — funding 決済時 markPrice を `markPriceKlines` から再構成できるかの**検証**。

    uv run python -m mce.funding_mark_probe \
        --json data/manifests/phase8_funding_mark_reconstruction_probe_v1.json

**検証だけである。** 値を生成しない・補間しない・canonical dataset へ併合しない。
rho / シグナル / 清算発生 / return / PnL を計算しない。Layer 1/2/3 を走らせない。

---

## 候補は**結果を見る前に**固定してある

primary(唯一の昇格候補):

    fundingTime を含む5分バーの **mark_open**
    バーの識別は floor_5m(fundingTime)
    mark 経路の状態が observed / verified_repair のときだけ利用可能
    route_unverified / stale_unverified / source_unobservable は**利用不能**

diagnostic(**primary へ昇格させない**):

    fundingTime 直前の5分バーの mark_close
    同一バーの mark_close

**各イベントごとに最も近い価格を選ぶことはしない。** primary は
`mark_open` に固定されており、候補の中から誤差が小さいものを事後に選ぶ経路が
そもそも存在しない(`DIAGNOSTIC_*` は判定に入らない)。

## Decimal 完全一致は**原文**で判定する

正規化 parquet は `mark_price` を `Float64` で持つ。float を経由した比較は
**Decimal 完全一致の判定に使ってはならない**。実測でも Vision の生 CSV は
`89766.10000000` のような末尾ゼロ付き十進表記で、`repr(float(...))` は
これを再現しない(2025-12 の 8,928 行中 4,897 行)。

したがって本検証は**両側とも原文から Decimal を作る**:

- Vision 側: `data/raw/binance/vision/mark_price_5m/**.zip` の CSV テキスト
- REST 側 : 公開 `GET /fapi/v1/fundingRate` の `markPrice` 文字列

(末尾ゼロの有無は Decimal の**数値比較**では差にならない。桁落ちが問題なので
float を経路から外している。)
"""

from __future__ import annotations

import argparse
import csv
import json
import zipfile
from datetime import datetime, timedelta, timezone
from decimal import Decimal, getcontext
from pathlib import Path
from typing import Final, Iterable, Mapping, Sequence

import polars as pl

from mce import config
from mce.backtest import mark_path
from mce.backtest.splits import FINAL_OOS_START, phase8_layer
from mce.binance_vision import DEFAULT_SYMBOL, raw_dir

UTC = timezone.utc
BAR_MS: Final = 5 * 60 * 1000
getcontext().prec = 50  # bps 換算のための十分な桁

# --- 事前に固定した候補定義(結果を見て変えない)-----------------------------

PRIMARY_CANDIDATE: Final = "mark_open_of_bar_containing_funding_time"
PRIMARY_BAR_RULE: Final = "floor_5m(funding_time)"
PRIMARY_FIELD: Final = "mark_open"
#: primary が利用可能な mark 経路の状態(§32)。それ以外は**利用不能**。
PRIMARY_USABLE_STATUSES: Final = (mark_path.OBSERVED, mark_path.VERIFIED_REPAIR)
#: 診断専用。**primary へ昇格させない。**
DIAGNOSTIC_CANDIDATES: Final = (
    "mark_close_of_previous_bar",
    "mark_close_of_same_bar",
)

VERDICT_DETERMINISTIC: Final = "candidate_deterministic_reconstruction"
VERDICT_PROXY_ONLY: Final = "proxy_only_not_exact_reconstruction"
VERDICT_INCOMPLETE: Final = "incomplete_conformance"
#: 比較対象が **0 件**のとき。事前登録の3判定は「比較できた標本がある」ことを
#: 前提にしており、0 件に当てはめると「全件一致」が空虚に真になってしまう。
#: **「検証できなかった」を「決定的に再構成できた」に化けさせない。**
VERDICT_NOT_VERIFIABLE: Final = "not_verifiable_no_overlap"

#: 検証2 の可用性の区別(**「取れなかった」を「存在しない」に化けさせない**)
AVAILABILITY_KINDS: Final = (
    "exact_conformance_verified",        # overlap 期間で REST と原文一致を確認済み
    "conformance_unverified_bar_present",  # バーはあるが REST に markPrice が無く未確認
    "mark_bar_absent",                   # markPriceKlines に観測が無い
    "mark_path_not_usable",              # バーはあるが状態が primary 利用不能
)


def floor_5m(ts_ms: int) -> int:
    """その時刻を含む5分バーの開始時刻(ms)。

    バー境界ちょうどは**新しいバー**、その 1ms 前は**前のバー**である。
    """
    return (ts_ms // BAR_MS) * BAR_MS


# --------------------------------------------------------------------------
# 原文の読み出し(**float を経路に入れない**)
# --------------------------------------------------------------------------


def load_mark_bar_text(
    symbol: str = DEFAULT_SYMBOL, source_dir: Path | None = None
) -> dict[int, dict]:
    """raw zip から `open_time -> {mark_open/mark_close の原文, mark_samples}`。

    **正規化 parquet を読まない。** parquet は Float64 なので、そこから作った
    Decimal は原文の十進値と一致する保証がない。
    """
    source_dir = source_dir or raw_dir("mark_price_5m", symbol)
    out: dict[int, dict] = {}
    for path in sorted(source_dir.glob("*.zip")):
        with zipfile.ZipFile(path) as z:
            names = [n for n in z.namelist() if n.lower().endswith(".csv")]
            text = z.read(names[0]).decode()
        for row in csv.reader(text.splitlines()):
            if not row or row[0].strip().lower() == "open_time":
                continue
            open_ms = int(row[0])
            if open_ms >= int(FINAL_OOS_START.timestamp() * 1000):
                continue  # 封印域は読まない
            out[open_ms] = {
                "mark_open_text": row[1],
                # high / low は **primary 候補ではない**。誤差の層別(バー自身の
                # 値幅)にだけ使う。判定経路には入らない。
                "mark_high_text": row[2],
                "mark_low_text": row[3],
                "mark_close_text": row[4],
                "mark_samples": int(row[8]),
            }
    return out


def bar_status(
    bar_ms: int, bars: Mapping[int, dict], probe: Mapping | None = None
) -> str:
    """凍結済み `mark_path.canonical_timeline` の規則でそのバーの状態を決める。

    **状態の定義をここで作り直さない**(§32 の凍結実装をそのまま使う)。
    """
    ts = datetime.fromtimestamp(bar_ms / 1000, UTC)
    row = bars.get(bar_ms)
    rows = [{"ts": ts, "mark_high": None, "mark_close": None,
             "mark_samples": row["mark_samples"]}] if row else []
    timeline = mark_path.canonical_timeline(rows, start=ts, end=ts, probe=probe)
    return timeline[0].mark_path_status


# --------------------------------------------------------------------------
# 検証1 — REST markPrice がある決済との Decimal 完全一致
# --------------------------------------------------------------------------


def _percentiles(values: Sequence[Decimal]) -> dict:
    """nearest-rank 方式。**空なら null**(0 で埋めない)。

    **`min` / `p01` / `p05` も必ず出す。** 上側だけを出すと、符号付きの量で
    負の裾が成果物から見えなくなり、分布が片側であるかのように読める。
    """
    if not values:
        return {"min": None, "p01": None, "p05": None, "p50": None,
                "p95": None, "p99": None, "max": None, "n": 0}
    ordered = sorted(values)
    n = len(ordered)

    def at(q: float) -> str:
        rank = max(1, min(n, int(-(-q * n // 1))))  # ceil(q*n)
        return str(ordered[rank - 1])

    return {"min": str(ordered[0]), "p01": at(0.01), "p05": at(0.05),
            "p50": at(0.50), "p95": at(0.95), "p99": at(0.99),
            "max": str(ordered[-1]), "n": n}


def verify_exact_conformance(
    funding_events: Sequence[Mapping],
    rest_marks: Mapping[int, str],
    bars: Mapping[int, dict],
    probe: Mapping | None = None,
) -> dict:
    """REST markPrice がある決済について primary candidate と**原文で**比較する。

    `funding_events`: `{"funding_time_ms": int}` の列(Vision canonical)。
    `rest_marks`: `funding_time_ms -> markPrice の原文`(空文字は含めない)。
    """
    considered: list[dict] = []
    unmappable: list[dict] = []
    ambiguous: list[dict] = []
    mismatches: list[dict] = []
    status_counts: dict[str, int] = {}
    samples_counts: dict[str, int] = {}
    layer_counts: dict[str, int] = {}
    abs_diffs: list[Decimal] = []
    abs_bps: list[Decimal] = []
    signed_bps: list[Decimal] = []
    bar_claims: dict[int, list[int]] = {}

    # **全決済**でバーの占有を数える。REST markPrice の有無で絞ると、
    # 「片方だけ REST がある2決済が同じバーに寄った」衝突を見逃す。
    for event in funding_events:
        ft = int(event["funding_time_ms"])
        bar_claims.setdefault(floor_5m(ft), []).append(ft)

    for event in funding_events:
        ft = int(event["funding_time_ms"])
        rest_text = rest_marks.get(ft)
        if rest_text is None:
            continue
        bar_ms = floor_5m(ft)
        status = bar_status(bar_ms, bars, probe)
        status_counts[status] = status_counts.get(status, 0) + 1
        layer = phase8_layer(datetime.fromtimestamp(ft / 1000, UTC))
        layer_counts[layer] = layer_counts.get(layer, 0) + 1
        record = {
            "funding_time_utc": datetime.fromtimestamp(ft / 1000, UTC).isoformat(),
            "funding_time_ms": ft,
            "mark_bar_open_ms": bar_ms,
            "mark_bar_open_utc": datetime.fromtimestamp(bar_ms / 1000, UTC).isoformat(),
            "offset_into_bar_ms": ft - bar_ms,
            "mark_path_status": status,
            "layer": layer,
        }
        # **多対一を拒否する**(同じバーに複数の決済が寄ったら曖昧)
        if len(bar_claims.get(bar_ms, [])) > 1:
            ambiguous.append(record | {"reason": "multiple_funding_events_share_one_mark_bar",
                                       "sharing_funding_times_ms": bar_claims[bar_ms]})
            continue
        row = bars.get(bar_ms)
        if row is None:
            unmappable.append(record | {"reason": "mark_bar_absent"})
            continue
        if status not in PRIMARY_USABLE_STATUSES:
            unmappable.append(record | {"reason": f"mark_path_not_usable:{status}"})
            continue

        candidate_text = row["mark_open_text"]
        candidate = Decimal(candidate_text)
        rest = Decimal(rest_text)
        samples = row["mark_samples"]
        samples_counts[str(samples)] = samples_counts.get(str(samples), 0) + 1
        exact = candidate == rest
        diff = candidate - rest
        abs_diff = abs(diff)
        abs_diffs.append(abs_diff)
        if rest != 0:
            bps = diff / rest * Decimal(10_000)
            signed_bps.append(bps)
            abs_bps.append(abs(bps))
        considered.append(record | {"exact": exact})
        if not exact:
            mismatches.append(record | {
                "candidate_mark_open_text": candidate_text,
                "rest_mark_price_text": rest_text,
                "absolute_difference": str(abs_diff),
                "signed_bps": str(diff / rest * Decimal(10_000)) if rest != 0 else None,
                "mark_samples": samples,
            })

    exact_count = sum(1 for c in considered if c["exact"])
    if not considered and not unmappable and not ambiguous:
        # 比較できた標本が1件も無い。**空虚に「全件一致」としない。**
        verdict = VERDICT_NOT_VERIFIABLE
    elif unmappable or ambiguous:
        verdict = VERDICT_INCOMPLETE
    elif mismatches:
        verdict = VERDICT_PROXY_ONLY
    else:
        verdict = VERDICT_DETERMINISTIC

    return {
        "candidate": {
            "primary": PRIMARY_CANDIDATE,
            "bar_rule": PRIMARY_BAR_RULE,
            "field": PRIMARY_FIELD,
            "usable_statuses": list(PRIMARY_USABLE_STATUSES),
            "diagnostic_only": list(DIAGNOSTIC_CANDIDATES),
            "diagnostic_promoted_to_primary": False,
            "nearest_candidate_selection": False,
            "comparison": "Decimal on source text on both sides (no float in the path)",
        },
        "events_with_rest_mark_price": len(considered) + len(unmappable) + len(ambiguous),
        "one_to_one_mark_bar_matches": len(considered),
        "exact_decimal_matches": exact_count,
        "exact_decimal_match_rate": (
            f"{exact_count}/{len(considered)}" if considered else "0/0"
        ),
        "mismatches": len(mismatches),
        "unmappable": len(unmappable),
        "ambiguous": len(ambiguous),
        "absolute_price_difference": _percentiles(abs_diffs),
        "absolute_bps_difference": _percentiles(abs_bps),
        "signed_bps_difference": _percentiles(signed_bps),
        "timestamp_correspondence": {
            "rule": PRIMARY_BAR_RULE,
            "offset_into_bar_ms_distribution": _offset_distribution(considered),
        },
        "mark_samples_distribution": dict(
            sorted(samples_counts.items(), key=lambda kv: -kv[1])
        ),
        "mark_path_status_counts": status_counts,
        "layer_counts": layer_counts,
        "mismatch_detail": mismatches,          # **全件。要約で丸めない**
        "unmappable_detail": unmappable,
        "ambiguous_detail": ambiguous,
        "verdict": verdict,
    }


def diagnostic_agreement(
    funding_events: Sequence[Mapping],
    rest_marks: Mapping[int, str],
    bars: Mapping[int, dict],
) -> dict:
    """診断候補の一致数を**参考として**数える。

    **この結果は判定に一切入らない。** 昇格させないことを機械的に保証するため、
    戻り値は `verify_exact_conformance` の入力にも verdict にも使われない。
    「primary が駄目なら一致率の高い方へ乗り換える」ことをしないための記録である。
    """
    counts = {name: 0 for name in DIAGNOSTIC_CANDIDATES}
    considered = 0
    for event in funding_events:
        ft = int(event["funding_time_ms"])
        rest_text = rest_marks.get(ft)
        if rest_text is None:
            continue
        bar_ms = floor_5m(ft)
        same = bars.get(bar_ms)
        previous = bars.get(bar_ms - BAR_MS)
        if same is None:
            continue
        considered += 1
        rest = Decimal(rest_text)
        if previous is not None and Decimal(previous["mark_close_text"]) == rest:
            counts["mark_close_of_previous_bar"] += 1
        if Decimal(same["mark_close_text"]) == rest:
            counts["mark_close_of_same_bar"] += 1
    return {
        "considered": considered,
        "exact_matches": counts,
        "promoted_to_primary": False,
        "note": "diagnostic only; does not enter the verdict and must not be "
                "used to switch the primary candidate after seeing the result",
    }


def _offset_distribution(records: Iterable[Mapping]) -> dict:
    out: dict[str, int] = {}
    for r in records:
        key = str(r["offset_into_bar_ms"])
        out[key] = out.get(key, 0) + 1
    return dict(sorted(out.items(), key=lambda kv: int(kv[0])))


# --------------------------------------------------------------------------
# 検証2 — 全決済に対する primary candidate の入力可用性
# --------------------------------------------------------------------------


def availability_census(
    funding_events: Sequence[Mapping],
    rest_marks: Mapping[int, str],
    bars: Mapping[int, dict],
    probe: Mapping | None = None,
) -> dict:
    """**値を作らない。** primary candidate の入力可用性だけを数える。"""
    status_counts: dict[str, int] = {}
    kind_counts: dict[str, int] = {}
    by_layer: dict[str, dict[str, int]] = {}
    by_year: dict[str, dict[str, int]] = {}
    overlap_detail: list[dict] = []

    for event in funding_events:
        ft = int(event["funding_time_ms"])
        when = datetime.fromtimestamp(ft / 1000, UTC)
        bar_ms = floor_5m(ft)
        status = bar_status(bar_ms, bars, probe)
        row = bars.get(bar_ms)
        has_rest = ft in rest_marks

        if row is None:
            kind = "mark_bar_absent"
        elif status not in PRIMARY_USABLE_STATUSES:
            kind = "mark_path_not_usable"
        elif has_rest:
            kind = "exact_conformance_verified"
        else:
            kind = "conformance_unverified_bar_present"

        layer = phase8_layer(when)
        year = str(when.year)
        status_counts[status] = status_counts.get(status, 0) + 1
        kind_counts[kind] = kind_counts.get(kind, 0) + 1
        by_layer.setdefault(layer, {})
        by_layer[layer][kind] = by_layer[layer].get(kind, 0) + 1
        by_year.setdefault(year, {})
        by_year[year][kind] = by_year[year].get(kind, 0) + 1

        if status not in PRIMARY_USABLE_STATUSES:
            overlap_detail.append({
                "funding_time_utc": when.isoformat(),
                "funding_time_ms": ft,
                "mark_bar_open_ms": bar_ms,
                "mark_bar_open_utc": datetime.fromtimestamp(bar_ms / 1000, UTC).isoformat(),
                "mark_path_status": status,
                "mark_bar_present": row is not None,
                "mark_samples": row["mark_samples"] if row else None,
                "rest_mark_price_present": has_rest,
                "layer": layer,
                "region": "P1_missing_vision_bars" if row is None else "P2_stale_bars",
            })

    return {
        "funding_events": len(funding_events),
        "events_with_rest_mark_price": sum(1 for e in funding_events
                                           if int(e["funding_time_ms"]) in rest_marks),
        "events_without_rest_mark_price": sum(1 for e in funding_events
                                              if int(e["funding_time_ms"]) not in rest_marks),
        "mark_path_status_counts": status_counts,
        "availability_kinds": kind_counts,
        "availability_kind_meaning": {
            "exact_conformance_verified":
                "mark bar usable AND REST published a markPrice, so the reconstruction "
                "was checked against primary data for this settlement",
            "conformance_unverified_bar_present":
                "mark bar usable but REST publishes no markPrice for this settlement, "
                "so exact conformance is UNVERIFIED here (not refuted)",
            "mark_bar_absent":
                "markPriceKlines has no observation for the bar (P1 region)",
            "mark_path_not_usable":
                "bar exists but its mark path status is not observed/verified_repair "
                "(P2 stale, or route_unverified)",
        },
        "by_layer": by_layer,
        "by_year": dict(sorted(by_year.items())),
        "p1_p2_overlap_detail": overlap_detail,   # **全件**
        "values_generated": False,
        "values_stored": False,
        "interpolation": "none",
    }


# --------------------------------------------------------------------------
# artifact
# --------------------------------------------------------------------------


def load_funding_events(symbol: str = DEFAULT_SYMBOL) -> list[dict]:
    """Vision canonical の決済イベント(**読むだけ。変更しない**)。

    **封印を明示的に適用する。** canonical parquet は現状 2025-12 で終わっているが、
    「たまたま範囲外の行が無い」ことに依存すると、系列が延びた日に封印域が
    静かに集計へ混ざる。
    """
    cutoff_ms = int(FINAL_OOS_START.timestamp() * 1000)
    df = pl.read_parquet(config.binance_funding_rate_parquet(symbol))
    return [
        {"funding_time_ms": int(ts.timestamp() * 1000)}
        for ts in df.sort("ts")["ts"].to_list()
        if int(ts.timestamp() * 1000) < cutoff_ms
    ]


def fetch_rest_mark_texts(
    symbol: str = DEFAULT_SYMBOL, transport=None
) -> tuple[dict[int, str], list[dict]]:
    """公開 REST から `fundingTime -> markPrice の原文` と**ページ出所**。

    保存済み parquet は `Float64` なので原文が残っていない。Decimal 完全一致を
    判定するには**文字列のまま**持つ必要がある(モジュール docstring 参照)。

    出所(応答の SHA-256・要求範囲・取得時刻)を**捨てずに返す**。捨てると、
    成果物に載る一致件数を後から誰も再検証できなくなる。
    """
    from mce.binance_rest import fetch_funding_rates, month_range_ms

    start_ms, end_ms = month_range_ms("2020-01", "2025-12")
    rows, pages = fetch_funding_rates(symbol, start_ms=start_ms, end_ms=end_ms,
                                      transport=transport)
    out: dict[int, str] = {}
    for row in rows:
        text = str(row.get("markPrice", "")).strip()
        if text:
            out[int(row["fundingTime"])] = text
    provenance = [
        {
            "page_index": p.page_index,
            "requested_start_ms": p.requested_start_ms,
            "requested_end_ms": p.requested_end_ms,
            "limit": p.limit,
            "http_status": p.http_status,
            "response_sha256": p.response_sha256,
            "response_bytes": p.response_bytes,
            "retrieved_at_utc": p.retrieved_at_utc,
            "row_count": p.row_count,
            "first_funding_time": p.first_funding_time,
            "last_funding_time": p.last_funding_time,
        }
        for p in pages
    ]
    return out, provenance


#: repo 直下からの相対で解決する(cwd 依存にすると、別ディレクトリから走らせた
#: ときに probe が無言で `None` に縮退し、欠測バーの状態が一律 `route_unverified`
#: に落ちる。**「読めなかった」を状態の判定に混ぜない**)。
_REPO_ROOT: Final = Path(__file__).resolve().parents[2]
MARK_GAP_PROBE_PATH: Final = _REPO_ROOT / "experiments" / "phase8" / "mark_gap_probe_v1.json"


def build(symbol: str = DEFAULT_SYMBOL, transport=None) -> dict:
    probe_path = MARK_GAP_PROBE_PATH
    if not probe_path.exists():
        raise FileNotFoundError(
            f"mark gap probe が見つからない: {probe_path}. "
            "欠測バーの状態を無言で route_unverified へ落とさないため、明示的に停止する。"
        )
    probe = json.loads(probe_path.read_text(encoding="utf-8"))
    events = load_funding_events(symbol)
    bars = load_mark_bar_text(symbol)
    rest_marks, rest_pages = fetch_rest_mark_texts(symbol, transport)

    verification_1 = verify_exact_conformance(events, rest_marks, bars, probe)
    verification_2 = availability_census(events, rest_marks, bars, probe)
    return {
        "probe": "phase8_funding_mark_reconstruction_probe_v1",
        "purpose": "input availability verification only; no values generated, "
                   "no merge into any canonical dataset, no interpolation; "
                   "no rho, no signals, no returns, no PnL, no layer execution",
        "built_at_utc": datetime.now(UTC).isoformat(),
        "symbol": symbol,
        "seal_cutoff": FINAL_OOS_START.isoformat(),
        "sources": {
            "funding_events_canonical": config.binance_funding_rate_parquet(symbol).as_posix(),
            "mark_bars_source_text": raw_dir("mark_price_5m", symbol).as_posix(),
            "rest_mark_price": "public GET /fapi/v1/fundingRate (markPrice text, in memory only)",
            "mark_gap_probe": probe_path.as_posix(),
        },
        # **REST 応答の出所を捨てない。** これが無いと、下の一致件数を
        # 後から誰も再検証できない(何を読んだのかが残らない)。
        "rest_page_provenance": rest_pages,
        "refusals": [
            "no interpolation of missing markPrice",
            "no merge into the canonical dataset",
            "no per-event nearest-candidate selection",
            "no float in the exact-match decision path",
            "diagnostic candidates are never promoted to primary",
        ],
        "verification_1_exact_conformance": verification_1,
        "verification_2_availability": verification_2,
        # **判定に入らない。** 記録として残すだけである(昇格させない)。
        "diagnostic_candidates": diagnostic_agreement(events, rest_marks, bars),
        "verdict": verification_1["verdict"],
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
    v1 = report["verification_1_exact_conformance"]
    v2 = report["verification_2_availability"]
    print(f"verdict: {report['verdict']}")
    print(f"  v1: considered={v1['events_with_rest_mark_price']} "
          f"one_to_one={v1['one_to_one_mark_bar_matches']} "
          f"exact={v1['exact_decimal_match_rate']} "
          f"mismatch={v1['mismatches']} unmappable={v1['unmappable']} "
          f"ambiguous={v1['ambiguous']}")
    print(f"  v2: {v2['availability_kinds']}")


if __name__ == "__main__":
    main()

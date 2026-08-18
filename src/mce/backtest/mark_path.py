"""v1.8.5 §32 / §33 — mark 経路の観測可能性と gate の順序。

**取引系列を作らない。** rho もシグナルも return も PnL も計算しない。
ここにあるのは「その5分バーの mark 経路をどれだけ信用できるか」の表現と、
その状態を経済指標より**前**に評価させるための gate だけである。

要点:

- **欠測の Vision mark バーを inner join で消してはならない。**
  canonical な5分タイムラインを保持し、mark データと**品質状態**を付ける。
- 状態は `phase8_prereg.MARK_PATH_STATUSES` に凍結してある。
- 建玉中に `observed` / `verified_repair` 以外の状態のバーがあれば、
  経済指標より前に layer を中断し `liquidation_state_unknown` とする。
- **その経路を `liquidation_count == 0` と数えてはならない。**

gate の順序(§33):

    mark 経路の観測可能性 → 清算検出 → 清算件数 → H14a の手数料 gate → 経済指標
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Final, Iterable, Mapping, Sequence

from mce import phase8_prereg as P

UTC = timezone.utc
BAR = timedelta(minutes=5)

OBSERVED: Final = "observed"
VERIFIED_REPAIR: Final = "verified_repair"
ROUTE_UNVERIFIED: Final = "route_unverified"
STALE_UNVERIFIED: Final = "stale_unverified"
SOURCE_UNOBSERVABLE: Final = "source_unobservable"

#: プローブの分類 → mark 経路の状態。**遮断は source の欠測に化けない。**
PROBE_CLASS_TO_STATUS: Final[Mapping[str, str]] = {
    "candidate_deterministic_repair": VERIFIED_REPAIR,
    "mark_path_unobservable": SOURCE_UNOBSERVABLE,
    "probe_blocked_by_egress": ROUTE_UNVERIFIED,
}

__all__ = [
    "OBSERVED", "VERIFIED_REPAIR", "ROUTE_UNVERIFIED", "STALE_UNVERIFIED",
    "SOURCE_UNOBSERVABLE", "PROBE_CLASS_TO_STATUS", "MarkBar",
    "canonical_timeline", "is_acceptable", "evaluate_gates", "layer_disposition",
]


@dataclass(frozen=True)
class MarkBar:
    """canonical タイムライン上の1バー。**欠測でも行として残る。**"""

    ts: datetime
    mark_high: float | None
    mark_close: float | None
    mark_samples: int | None
    mark_path_status: str

    @property
    def acceptable(self) -> bool:
        return is_acceptable(self.mark_path_status)


def is_acceptable(status: str) -> bool:
    """建玉中に許容できる状態か(§32)。"""
    if status not in P.MARK_PATH_STATUSES:
        raise ValueError(f"凍結されていない mark 経路の状態: {status!r}")
    return status in P.MARK_PATH_ACCEPTABLE


def _probe_status_by_ts(probe: Mapping | None) -> dict[datetime, str]:
    """プローブ成果物から「欠測バー → 状態」を作る。

    プローブが無ければ空。**その場合、欠測は `route_unverified` になる**
    (未判定であって、source の欠測ではない)。
    """
    if not probe:
        return {}
    out: dict[datetime, str] = {}
    for interval in probe.get("intervals", []):
        status = PROBE_CLASS_TO_STATUS.get(interval.get("classification", ""))
        if status is None:
            continue
        for open_ms in interval.get("target_open_times", []):
            out[datetime.fromtimestamp(open_ms / 1000, UTC)] = status
    return out


def canonical_timeline(
    rows: Iterable[Mapping],
    *,
    start: datetime | None = None,
    end: datetime | None = None,
    probe: Mapping | None = None,
) -> list[MarkBar]:
    """5分グリッドを**端から端まで**作り、mark と品質状態を付ける。

    `rows` は `ts` / `mark_high` / `mark_close` / `mark_samples` を持つ写像の列
    (Vision 由来)。**inner join ではない**: `rows` に無いグリッド点も
    行として残り、状態が付く。

    状態の決め方:

    - `rows` にあり `mark_samples > 0`          → ``observed``
    - `rows` にあり `mark_samples == 0`         → ``stale_unverified``
    - `rows` に無く、プローブが復元を検証済み    → ``verified_repair``
    - `rows` に無く、プローブが復元不能と判定    → ``source_unobservable``
    - `rows` に無く、プローブ未実行/経路遮断    → ``route_unverified``
    """
    by_ts = {r["ts"]: r for r in rows}
    if not by_ts and (start is None or end is None):
        return []
    lo = start if start is not None else min(by_ts)
    hi = end if end is not None else max(by_ts)
    probe_status = _probe_status_by_ts(probe)

    out: list[MarkBar] = []
    ts = lo
    while ts <= hi:
        row = by_ts.get(ts)
        if row is None:
            status = probe_status.get(ts, ROUTE_UNVERIFIED)
            out.append(MarkBar(ts, None, None, None, status))
        else:
            samples = row.get("mark_samples")
            status = STALE_UNVERIFIED if samples == 0 else OBSERVED
            out.append(
                MarkBar(ts, row.get("mark_high"), row.get("mark_close"), samples, status)
            )
        ts += BAR
    return out


def evaluate_gates(
    *,
    mark_path_ok: bool,
    liquidation_count: int,
    clearance_fee_resolved: bool,
) -> str | None:
    """§33 の gate を**この順序でのみ**評価する。

    戻り値は disposition。`None` なら経済指標へ進んでよい。

    順序:

    1. **mark 経路の観測可能性** — 満たされなければ即 `liquidation_state_unknown`。
       **ここで `liquidation_count` を見てはならない。** 観測できない経路を
       「清算 0 件」と数えることになるからである。
    2. 清算検出 → 3. 清算件数
    4. **H14a の手数料 gate** — 清算が 1 件以上あり手数料が未解決なら
       `liquidation_model_blocked`。**ゼロ手数料での代替はしない。**
    5. 経済指標
    """
    if not mark_path_ok:
        return P.LIQUIDATION_STATE_UNKNOWN_DISPOSITION
    if liquidation_count == 0:
        return None  # H14a は拘束しない
    if not clearance_fee_resolved:
        return P.LIQUIDATION_MODEL_BLOCKED_DISPOSITION
    return None


def layer_disposition(
    bars_while_open: Sequence[MarkBar],
    *,
    liquidation_count: int,
    clearance_fee_resolved: bool,
) -> str | None:
    """建玉中のバー列から layer の disposition を決める。

    **1本でも許容外の状態があれば** `liquidation_state_unknown`。
    trade を落とさず、清算が無かったとも仮定しない。
    """
    mark_path_ok = all(b.acceptable for b in bars_while_open)
    return evaluate_gates(
        mark_path_ok=mark_path_ok,
        liquidation_count=liquidation_count,
        clearance_fee_resolved=clearance_fee_resolved,
    )

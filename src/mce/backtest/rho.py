"""Phase 8.1 — ρ(A2 の futures-spot deviation)と Arm R のシグナル層。

凍結プロトコル v1.8.2 §4.2 / §6.3 に適合させた**純粋な数学層**である。

    rho = KAPPA * (1 - exp(-(log(perp) - log(spot)))) - (r - r_prime)
    rho_u(C) = KAPPA * log(1 + C)          A2 Table 3 caption
    rho_l(C) = KAPPA * log(1 - C)
    Arm R entry : rho > rho_u(C)
    Arm R exit  : rho <= 0                 （境界ではなく 0 へ戻ったとき)

**設計上の制約(凍結プロトコルと決定ログに由来)**:

- `r` は**明示的な入力**である。本モジュールは金利ソースを知らない。
  H15(Aave 市場の同定)が未解決でも、この層は検証できる。
- コスト `C` も**明示的な入力**である。`COST_SCENARIOS["base_taker"]` を
  暗黙に使わない。**H13 未解決の placeholder を実体化しない。**
- `r` が欠測・陳腐化しているときは **シグナルを出さない**。
  補完値を作らない。point-in-time データ契約が禁じる区間をまたいで前方補完しない。

**本モジュールは実験を実行しない。** バーとレートを受け取り、判定を返すだけである。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from typing import Mapping, Sequence

from mce import phase8_prereg as P

__all__ = [
    "RateObservation",
    "RhoInputs",
    "Signal",
    "SignalPoint",
    "rho",
    "rho_exact",
    "arb_bound_upper",
    "arb_bound_lower",
    "point_in_time_rate",
    "aave_basket_mean",
    "aave_market_for",
    "arm_r_signal",
    "generate_arm_r_signals",
    "require_resolved_cost",
]


class Signal(str, Enum):
    """Arm R の判定。`NO_RATE` は「シグナルなし」であって「建てない」ではない。"""

    ENTER = "enter"
    EXIT = "exit"
    HOLD = "hold"
    NO_RATE = "no_rate"


# ---------------------------------------------------------------------------
# 純粋な数学(H15 と独立に検証できる)
# ---------------------------------------------------------------------------


def rho_exact(perp: float, spot: float, r: float, r_prime: float = P.R_PRIME) -> float:
    """A2 eq.(8) の厳密形。

        rho = KAPPA * (1 - exp(-(log(perp) - log(spot)))) - (r - r_prime)

    `r` は**年率の小数**(0.05 = 5%)。A2 Figure 8 の目盛り 0.00–0.40 は
    "percent" と書かれているが値は小数である(§26.1)。
    """
    if perp <= 0 or spot <= 0:
        raise ValueError("perp / spot は正でなければならない")
    log_gap = math.log(perp) - math.log(spot)
    return P.KAPPA * (1.0 - math.exp(-log_gap)) - (r - r_prime)


def rho(perp: float, spot: float, r: float, r_prime: float = P.R_PRIME) -> float:
    """`rho_exact` の別名。凍結プロトコルの表記に合わせる。"""
    return rho_exact(perp, spot, r, r_prime)


def arb_bound_upper(round_trip_cost: float) -> float:
    """rho_u = KAPPA * log(1 + C)。`C` は往復コストの**小数**。"""
    _check_cost(round_trip_cost)
    return P.arb_bound_upper(round_trip_cost)


def arb_bound_lower(round_trip_cost: float) -> float:
    """rho_l = KAPPA * log(1 - C)。"""
    _check_cost(round_trip_cost)
    return P.arb_bound_lower(round_trip_cost)


def _check_cost(c: float) -> None:
    if not 0.0 <= c < 1.0:
        raise ValueError("往復コスト C は 0 <= C < 1 の小数でなければならない")


# ---------------------------------------------------------------------------
# H13 ガード(placeholder を実体化させない)
# ---------------------------------------------------------------------------


def require_resolved_cost(scenario: str) -> tuple[float, float]:
    """凍結コスト階層から (spot bps, perp bps) を取り出す。

    **H13 が未解決の階層は使わせない。** `base_taker` の perp 側は FAQ 由来の
    placeholder であり、実測値ではない(§22.1)。
    """
    if scenario not in P.COST_SCENARIOS:
        raise KeyError(f"未知のコスト階層: {scenario!r}")
    spot_bps, perp_bps = P.COST_SCENARIOS[scenario]
    if perp_bps == P.PERP_TAKER_BPS and P.COMMISSION_RATE_STATUS != "resolved":
        raise ValueError(
            f"H13 未解決: コスト階層 {scenario!r} の perp taker は placeholder であり "
            "実測値ではない(§22.1)。C を明示的に渡すか、H13 を解決すること。"
        )
    return spot_bps, perp_bps


# ---------------------------------------------------------------------------
# point-in-time な r(欠測は補完しない)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RateObservation:
    """金利観測1件。

    `observed_ts` は**その値が利用可能になった時刻**であり、
    観測が指す暦日ではない。決定時刻より後の観測は使えない。
    """

    observed_ts: datetime
    rate: float


def point_in_time_rate(
    observations: Sequence[RateObservation],
    decision_ts: datetime,
    max_stale_seconds: int = P.RATE_MAX_STALE_SECONDS,
) -> float | None:
    """決定時刻で利用可能な最新の `r`。無ければ **None**(補完しない)。

    - `observed_ts <= decision_ts` のものだけを候補にする(未来参照の禁止)。
    - 直近の観測が `max_stale_seconds` より古ければ **None**。
      **前方補完しない**(point-in-time データ契約が禁じる区間をまたがない)。

    **既定は `RATE_MAX_STALE_SECONDS`(24h)である**(H16 / §28)。
    v1.8.2 までは funding 用の 9h を誤って使っていた。A2 の Aave 金利は**日次**なので、
    9h では同じ暦日の午前中に陳腐化してしまう。

    `observed_ts` は **その日 00:00 UTC のスナップショット時刻**であり、
    基礎となる reserve 更新イベントの時刻ではない(§27.4)。経過時間はスナップショット
    時刻から測る。
    """
    best: RateObservation | None = None
    for obs in observations:
        if obs.observed_ts > decision_ts:
            continue
        if best is None or obs.observed_ts > best.observed_ts:
            best = obs
    if best is None:
        return None
    if decision_ts - best.observed_ts > timedelta(seconds=max_stale_seconds):
        return None
    return best.rate


# ---------------------------------------------------------------------------
# Arm R のシグナル
# ---------------------------------------------------------------------------


def aave_basket_mean(rates: "Mapping[str, float | None]") -> float | None:
    """USDT / USDC / DAI の等加重平均(§27.3)。

    **3成分すべてを要求する。** どれか1つでも欠けたら **None** を返す。
    2成分で平均を取って「黙って basket 構成を変える」ことをしない。

    凍結された構成以外のキーが来たら例外にする(構成の差し替えを検出するため)。
    """
    required = set(P.RATE_ASSETS)
    got = set(rates)
    if got != required:
        raise ValueError(
            f"basket 構成が凍結値と違う: {sorted(got)} != {sorted(required)}(§27.3)"
        )
    values = [rates[a] for a in P.RATE_ASSETS]
    if any(v is None for v in values):
        return None  # 補完しない
    if not P.RATE_BASKET_REQUIRE_ALL:  # pragma: no cover - 凍結値は True
        raise AssertionError("RATE_BASKET_REQUIRE_ALL が False に変えられている")
    return sum(float(v) for v in values) / len(values)  # type: ignore[arg-type]


def aave_market_for(ts: datetime) -> str | None:
    """その時刻に適用する Aave 版(§27.2)。V4 へは移行しない。"""
    for name, start, end in P.RATE_MARKET_SPLICES:
        if ts >= start and (end is None or ts < end):
            return name
    return None  # V1 稼働開始前


@dataclass(frozen=True)
class RhoInputs:
    """1バー分の ρ 入力。`r` は None を取りうる(欠測・陳腐化)。"""

    ts: datetime
    perp: float
    spot: float
    r: float | None


@dataclass(frozen=True)
class SignalPoint:
    ts: datetime
    signal: Signal
    rho: float | None
    rho_upper: float
    in_position: bool


def arm_r_signal(
    rho_value: float | None, round_trip_cost: float, in_position: bool
) -> Signal:
    """Arm R の判定(§6.3)。

    entry : `rho > rho_u(C)`
    exit  : `rho <= 0`      … 境界ではなく **0** へ戻ったとき

    `rho_value is None`(= `r` が無い)なら **`NO_RATE`**。
    **建玉の意思決定をしない。** 補完値を作って判定しない。
    """
    if rho_value is None:
        return Signal.NO_RATE
    if in_position:
        return Signal.EXIT if rho_value <= 0.0 else Signal.HOLD
    return Signal.ENTER if rho_value > arb_bound_upper(round_trip_cost) else Signal.HOLD


def generate_arm_r_signals(
    inputs: Sequence[RhoInputs], round_trip_cost: float
) -> list[SignalPoint]:
    """バー列から Arm R のシグナル列を作る。

    `round_trip_cost` は**明示的な引数**である。凍結階層を暗黙に読まない
    (H13 未解決の placeholder を実体化しないため)。

    `NO_RATE` のバーでは **建玉状態を変更しない**(保有中なら保有のまま)。
    """
    _check_cost(round_trip_cost)
    upper = arb_bound_upper(round_trip_cost)
    out: list[SignalPoint] = []
    in_position = False
    for row in inputs:
        value = (
            None if row.r is None else rho_exact(row.perp, row.spot, row.r)
        )
        sig = arm_r_signal(value, round_trip_cost, in_position)
        if sig is Signal.ENTER:
            in_position = True
        elif sig is Signal.EXIT:
            in_position = False
        out.append(
            SignalPoint(
                ts=row.ts, signal=sig, rho=value, rho_upper=upper, in_position=in_position
            )
        )
    return out

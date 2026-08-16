"""5分OHLC pathを使うfirst-touch評価の純粋ロジック。

シグナル生成や候補選択は含めない。entry以降の連続したbarsとtimeout openを
受け取り、事前に固定したbarrierのどちらへ先に触れたかを悲観的に評価する。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal, Sequence


AmbiguousPolicy = Literal["stop", "take_profit", "exclude"]


@dataclass(frozen=True)
class BarrierSpec:
    barrier_id: str
    stop_bps: float
    take_profit_bps: float
    horizon_minutes: int

    @property
    def horizon_bars(self) -> int:
        if self.horizon_minutes <= 0 or self.horizon_minutes % 5:
            raise ValueError("horizon_minutes must be a positive multiple of 5")
        return self.horizon_minutes // 5


@dataclass(frozen=True)
class OhlcBar:
    ts: datetime
    open: float
    high: float
    low: float
    close: float


@dataclass(frozen=True)
class FirstTouchResult:
    status: Literal["take_profit", "stop", "timeout", "ambiguous_excluded"]
    exit_ts: datetime
    exit_price: float | None
    gross_return: float | None
    holding_minutes: int
    ambiguous: bool
    fixed_horizon_favorable_bps: float
    fixed_horizon_adverse_bps: float

    def net_return(self, round_trip_cost_bps: float) -> float | None:
        if self.gross_return is None:
            return None
        return self.gross_return - round_trip_cost_bps / 10_000


def validate_path(entry_ts: datetime, bars: Sequence[OhlcBar], timeout_ts: datetime) -> None:
    """entryからtimeout直前まで5分足が完全に連続していることを確認する。"""
    expected_count = int((timeout_ts - entry_ts).total_seconds() // 300)
    if timeout_ts <= entry_ts or expected_count * 300 != (timeout_ts - entry_ts).total_seconds():
        raise ValueError("timeout must be a positive 5-minute offset from entry")
    if len(bars) != expected_count:
        raise ValueError(f"path requires {expected_count} bars, got {len(bars)}")
    for i, bar in enumerate(bars):
        expected = entry_ts + timedelta(minutes=5 * i)
        if bar.ts != expected:
            raise ValueError(f"non-contiguous path at index {i}: expected {expected}, got {bar.ts}")
        if not (bar.low <= min(bar.open, bar.close) <= max(bar.open, bar.close) <= bar.high):
            raise ValueError(f"invalid OHLC at {bar.ts}")


def simulate_short(
    *,
    entry_ts: datetime,
    entry_price: float,
    bars: Sequence[OhlcBar],
    timeout_ts: datetime,
    timeout_open: float,
    spec: BarrierSpec,
    ambiguous_policy: AmbiguousPolicy = "stop",
) -> FirstTouchResult:
    """shortのfirst touchを評価する。

    barsは [entry_ts, timeout_ts) を完全に覆う。各足ではopen gapを先に評価し、
    intrabarでTP/SLが同時に見える場合はambiguous_policyに従う。
    """
    if entry_price <= 0 or timeout_open <= 0:
        raise ValueError("prices must be positive")
    if ambiguous_policy not in ("stop", "take_profit", "exclude"):
        raise ValueError(f"unknown ambiguous policy: {ambiguous_policy}")
    if timeout_ts != entry_ts + timedelta(minutes=spec.horizon_minutes):
        raise ValueError("timeout does not match barrier horizon")
    validate_path(entry_ts, bars, timeout_ts)

    stop_price = entry_price * (1 + spec.stop_bps / 10_000)
    take_price = entry_price * (1 - spec.take_profit_bps / 10_000)
    fixed_low = min(bar.low for bar in bars)
    fixed_high = max(bar.high for bar in bars)
    favorable = (entry_price - fixed_low) / entry_price * 10_000
    adverse = (fixed_high - entry_price) / entry_price * 10_000

    for i, bar in enumerate(bars):
        holding_minutes = i * 5
        # A stop gap receives the worse open. A favorable gap receives no price
        # improvement because a resting TP fill cannot be inferred from OHLC.
        if bar.open >= stop_price:
            return _result("stop", bar.ts, bar.open, entry_price, holding_minutes, False, favorable, adverse)
        if bar.open <= take_price:
            return _result(
                "take_profit", bar.ts, take_price, entry_price, holding_minutes, False, favorable, adverse
            )

        hit_stop = bar.high >= stop_price
        hit_take = bar.low <= take_price
        if hit_stop and hit_take:
            if ambiguous_policy == "exclude":
                return FirstTouchResult(
                    status="ambiguous_excluded",
                    exit_ts=bar.ts,
                    exit_price=None,
                    gross_return=None,
                    holding_minutes=holding_minutes,
                    ambiguous=True,
                    fixed_horizon_favorable_bps=favorable,
                    fixed_horizon_adverse_bps=adverse,
                )
            if ambiguous_policy == "take_profit":
                return _result(
                    "take_profit", bar.ts, take_price, entry_price, holding_minutes, True, favorable, adverse
                )
            return _result("stop", bar.ts, stop_price, entry_price, holding_minutes, True, favorable, adverse)
        if hit_stop:
            return _result("stop", bar.ts, stop_price, entry_price, holding_minutes, False, favorable, adverse)
        if hit_take:
            return _result(
                "take_profit", bar.ts, take_price, entry_price, holding_minutes, False, favorable, adverse
            )

    return _result(
        "timeout",
        timeout_ts,
        timeout_open,
        entry_price,
        spec.horizon_minutes,
        False,
        favorable,
        adverse,
    )


def _result(
    status: Literal["take_profit", "stop", "timeout"],
    exit_ts: datetime,
    exit_price: float,
    entry_price: float,
    holding_minutes: int,
    ambiguous: bool,
    favorable: float,
    adverse: float,
) -> FirstTouchResult:
    return FirstTouchResult(
        status=status,
        exit_ts=exit_ts,
        exit_price=exit_price,
        gross_return=(entry_price - exit_price) / entry_price,
        holding_minutes=holding_minutes,
        ambiguous=ambiguous,
        fixed_horizon_favorable_bps=favorable,
        fixed_horizon_adverse_bps=adverse,
    )

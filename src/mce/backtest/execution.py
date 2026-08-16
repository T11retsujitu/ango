"""決定的な next-bar 執行エンジン。

約定規則(docs/data_contract.md §2):

    signal   = close[t] 後に確定(target position ∈ {-1, 0, +1})
    fill     = 次に存在するバーの open

- 同一バーの close で執行することはできない(構造的に不可能)。
- fill 予定バーが欠損している場合、次に存在するバーの open で執行するが、
  signal 時刻から fill までの遅延が cancel_after_ms を超えるならキャンセルする
  (キャンセル数は結果に記録)。
- max_holding_bars を設定すると、保有がその本数に達した close で強制 exit signal
  を出す(fill は通常どおり next open)。
- position は各バー区間 [open_i, open_{i+1}) で一定。バー i の区間リターンは
  open_i → open_{i+1}(欠損ギャップはそのまま1区間として跨ぐ)。
"""

from dataclasses import dataclass, field

import polars as pl


@dataclass(frozen=True)
class ExecutionConfig:
    bar_ms: int = 5 * 60_000
    cancel_after_ms: int = 30 * 60_000  # signal→fill 遅延がこれを超える注文はキャンセル
    max_holding_bars: int | None = None  # 保有バー数の上限(None = 無制限)


@dataclass
class ExecutionResult:
    positions: pl.DataFrame  # ts, position(バー区間 [open_i, open_{i+1}) の保有)
    fills: pl.DataFrame  # signal_ts, fill_ts, fill_price, from_pos, to_pos
    trades: pl.DataFrame  # entry/exit の round trip(未決済は closed=False)
    cancelled_count: int = 0
    stats: dict = field(default_factory=dict)


_FILL_SCHEMA = {
    "signal_ts": pl.Datetime("ms", "UTC"),
    "fill_ts": pl.Datetime("ms", "UTC"),
    "fill_price": pl.Float64,
    "from_pos": pl.Int8,
    "to_pos": pl.Int8,
}

_TRADE_SCHEMA = {
    "side": pl.Int8,
    "entry_signal_ts": pl.Datetime("ms", "UTC"),
    "entry_ts": pl.Datetime("ms", "UTC"),
    "entry_price": pl.Float64,
    "exit_ts": pl.Datetime("ms", "UTC"),
    "exit_price": pl.Float64,
    "bars_held": pl.Int64,
    "gross_return": pl.Float64,  # side * (exit/entry - 1)。コスト控除前
    "closed": pl.Boolean,
}


def execute(bars: pl.DataFrame, target: pl.Series, cfg: ExecutionConfig | None = None) -> ExecutionResult:
    """bars(ts 昇順・重複なし・open 列必須)と target(バーごとの希望 position、
    close[t] 時点で確定)から、決定的に fills / trades / position 系列を作る。"""
    cfg = cfg or ExecutionConfig()
    if bars.height != len(target):
        raise ValueError(f"bars({bars.height}) と target({len(target)}) の長さが一致しない")
    if not bars["ts"].is_sorted():
        raise ValueError("bars は ts 昇順であること")

    ts_ms = bars["ts"].dt.epoch("ms").to_list()
    opens = bars["open"].to_list()
    tgt = target.cast(pl.Int8).to_list()
    n = bars.height

    positions = [0] * n
    fills: list[dict] = []
    trades: list[dict] = []
    cancelled = 0

    current = 0
    pending: tuple[int, int] | None = None  # (desired_pos, signal_ts_ms)
    entry: dict | None = None  # 進行中 trade

    for i in range(n):
        # --- バー i の open: 直前 close で出た signal をここで執行する ---
        if pending is not None:
            desired, signal_ms = pending
            pending = None
            delay = ts_ms[i] - signal_ms  # 欠損が無ければ 0
            if delay > cfg.cancel_after_ms:
                cancelled += 1
            elif desired != current:
                fills.append(
                    {
                        "signal_ts": signal_ms,
                        "fill_ts": ts_ms[i],
                        "fill_price": opens[i],
                        "from_pos": current,
                        "to_pos": desired,
                    }
                )
                if current != 0 and entry is not None:
                    trades.append(_close_trade(entry, ts_ms[i], opens[i], i))
                    entry = None
                if desired != 0:
                    entry = {
                        "side": desired,
                        "entry_signal_ts": signal_ms,
                        "entry_ts": ts_ms[i],
                        "entry_price": opens[i],
                        "entry_idx": i,
                    }
                current = desired

        positions[i] = current

        # --- バー i の close: 次の position を決める ---
        if tgt[i] not in (-1, 0, 1):
            raise ValueError(f"target は -1/0/1 のみ(bar {i}: {tgt[i]})")
        desired = tgt[i]
        if (
            cfg.max_holding_bars is not None
            and current != 0
            and entry is not None
            and i - entry["entry_idx"] + 1 >= cfg.max_holding_bars
        ):
            desired = 0  # forced exit(強制 exit signal)
        if desired != current:
            pending = (desired, ts_ms[i] + cfg.bar_ms)
        # 最終バーの signal は執行先が無いので自然消滅する(キャンセル数には含めない)

    if entry is not None:  # 未決済 trade
        trades.append(
            {
                "side": entry["side"],
                "entry_signal_ts": entry["entry_signal_ts"],
                "entry_ts": entry["entry_ts"],
                "entry_price": entry["entry_price"],
                "exit_ts": None,
                "exit_price": None,
                "bars_held": n - entry["entry_idx"],
                "gross_return": None,
                "closed": False,
            }
        )

    pos_df = pl.DataFrame({"ts": bars["ts"], "position": pl.Series(positions, dtype=pl.Int8)})
    fills_df = _to_df(fills, _FILL_SCHEMA)
    trades_df = _to_df(trades, _TRADE_SCHEMA)
    return ExecutionResult(
        positions=pos_df,
        fills=fills_df,
        trades=trades_df,
        cancelled_count=cancelled,
        stats={"bars": n, "fills": fills_df.height, "trades_closed": int(trades_df["closed"].sum())},
    )


def _close_trade(entry: dict, exit_ms: int, exit_price: float, exit_idx: int) -> dict:
    return {
        "side": entry["side"],
        "entry_signal_ts": entry["entry_signal_ts"],
        "entry_ts": entry["entry_ts"],
        "entry_price": entry["entry_price"],
        "exit_ts": exit_ms,
        "exit_price": exit_price,
        "bars_held": exit_idx - entry["entry_idx"],
        "gross_return": entry["side"] * (exit_price / entry["entry_price"] - 1),
        "closed": True,
    }


def _to_df(rows: list[dict], schema: dict) -> pl.DataFrame:
    df = pl.DataFrame(rows, schema=schema, orient="row") if rows else pl.DataFrame(schema=schema)
    return df


def bar_returns(bars: pl.DataFrame, positions: pl.DataFrame) -> pl.DataFrame:
    """バー区間ごとの gross return と turnover。

    - gross_return[i] = position[i] * (open[i+1] / open[i] - 1)(最終バーは 0)
    - turnover[i] = |position[i] - position[i-1]|(バー i の open での約定量)
    """
    df = bars.select("ts", "open").join(positions, on="ts", how="left").sort("ts")
    df = df.with_columns(
        (pl.col("position").cast(pl.Float64) * (pl.col("open").shift(-1) / pl.col("open") - 1))
        .fill_null(0.0)
        .alias("gross_return"),
        (pl.col("position") - pl.col("position").shift(1).fill_null(0))
        .abs()
        .cast(pl.Float64)
        .alias("turnover"),
    )
    return df.select("ts", "position", "gross_return", "turnover")

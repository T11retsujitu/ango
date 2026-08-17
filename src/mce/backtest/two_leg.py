"""Phase 8.1 — two-leg (spot + USD-M perpetual) carry の執行器。

凍結プロトコル v1.8 `docs/phase8/carry_replication_protocol_v1.md` の
§5.1 / §6 / §7.1 / §8.1 / §9 / §11 に**適合させるための実装**である。
仕様は凍結済みであり、本モジュールは仕様を変更しない。

設計上の要点(既存 `engine.py` を壊さないため別モジュールにした):

- **spot ウォレットと futures ウォレットを分離して持つ**(A3 レビュー S5)。
  単一の equity スカラーに畳むと M7(ヘッジは PnL 中立だが証拠金中立ではない)が
  表現できなくなる。
- **leverage を時系列として持つ**(A3 レビュー S6)。
- **funding cashflow を独立の系列として持つ**(A3 レビュー S7)。
- **2脚を別々にモデル化する。** 合成の1約定に畳まない。

A3(`0xBoringWozniak/optimal-basis-trade-control`)からは
`docs/phase8/a3_source_review_v1.md` が **adopted** と列挙した意味論だけを採る。
**diagnostic threshold と calibration 定数は一切採らない**(同 N1–N8)。

**本モジュールは実験を実行しない。** 与えられたバーと funding イベントに対する
決定的な会計器であり、シグナル生成もデータ取得も行わない。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Literal, Sequence

from mce import phase8_prereg as P
from mce.backtest.costs import TwoLegCostConfig
from mce.backtest.execution import ExecutionConfig

PostLiquidationRule = Literal["unwind", "rehedge"]

__all__ = [
    "Bar",
    "FundingEvent",
    "TwoLegConfig",
    "SpotWallet",
    "FuturesWallet",
    "BarState",
    "TradeResult",
    "UNFROZEN_PARAMETERS",
    "size_position",
    "round_to_lot",
    "simulate_trade",
]

# ---------------------------------------------------------------------------
# 凍結プロトコルに値が無いパラメータ(実装が露出させた仕様の穴)
#
# これらは **既定値を持たない**。呼び出し側が明示的に渡さなければならない。
# 黙って既定値を埋めると「凍結済みの仕様に従った」と誤認されるため。
# 詳細は docs/phase8/two_leg_conformance_notes_v1.md。
# ---------------------------------------------------------------------------
UNFROZEN_PARAMETERS: tuple[str, ...] = (
    "reserve_usdt",  # §11.4 は予備資金を要求するが §11.1 は C を使い切る
    "funding_counts_toward_margin",  # §9 M7 が「明示的に凍結する」と述べたが未凍結
    "post_liquidation",  # §11.3 が「どちらかを事前に選ぶ」と述べたが未凍結
)


@dataclass(frozen=True)
class Bar:
    """5分バー1本。spot と perp を**別々の価格として**保持する。

    `mark_high` は markPriceKlines 由来。清算判定はこれで行い、
    `perp_close`(last price)では行わない(§11.3 / 監査 Y38)。
    いずれかが None のバーは「片脚が無い」扱いになる(§9 M6a / M6b)。
    """

    ts: datetime
    spot_open: float | None
    spot_close: float | None
    perp_open: float | None
    perp_close: float | None
    mark_high: float | None = None
    mark_close: float | None = None

    @property
    def both_legs_present(self) -> bool:
        return None not in (self.spot_open, self.perp_open)


@dataclass(frozen=True)
class FundingEvent:
    """funding 決済1件。

    `settlement_ts` は Binance Vision の `calc_time` == 公式 REST の `fundingTime`
    であり、**決済時刻**である(X4 で照合済み)。`mark_price` は同じ行の
    `markPrice`(A3 レビュー S1 / S2)。`interval_hours` は**行ごとに読む**
    (X5: cap/floor 到達で恒久的に 1h へ切り替わるため 8 をハードコードしない)。
    """

    settlement_ts: datetime
    funding_rate: float
    mark_price: float
    interval_hours: float


@dataclass(frozen=True)
class TwoLegConfig:
    """執行器の設定。凍結値は `phase8_prereg` から既定を取る。

    `reserve_usdt` / `funding_counts_toward_margin` / `post_liquidation` は
    **凍結プロトコルに値が無い**ため既定を置かない(`UNFROZEN_PARAMETERS`)。
    """

    cost: TwoLegCostConfig
    reserve_usdt: float
    funding_counts_toward_margin: bool
    post_liquidation: PostLiquidationRule
    capital_base_usdt: float = P.CAPITAL_BASE_USDT
    leverage: float = P.LEVERAGE
    lot_step: float = P.PERP_LOT_STEP
    min_notional_usdt: float = P.PERP_MIN_NOTIONAL_USDT
    maint_margin_rate: float = P.MAINT_MARGIN_RATE_TIER1
    topup_trigger: float = P.MARGIN_TOPUP_TRIGGER
    topup_target: float = P.MARGIN_TOPUP_TARGET
    liquidation_slippage_bps: float = 0.0
    execution: ExecutionConfig = field(default_factory=ExecutionConfig)

    def __post_init__(self) -> None:
        if self.reserve_usdt < 0:
            raise ValueError("reserve_usdt は非負でなければならない")
        if self.reserve_usdt >= self.capital_base_usdt:
            raise ValueError("reserve_usdt が資本基準 C 以上では建玉できない")
        if not 0 < self.leverage:
            raise ValueError("leverage は正でなければならない")
        if not self.topup_trigger < self.topup_target:
            raise ValueError("topup_trigger < topup_target でなければならない")
        if self.maint_margin_rate >= self.topup_trigger:
            raise ValueError(
                "維持証拠金率 >= 追証トリガでは、追証が発火する前に清算される"
            )

    @property
    def position_capital_usdt(self) -> float:
        """建玉に充てる資本。予備資金を差し引いた残り。

        `reserve_usdt == 0` のとき §11.1 の `q_i = C·L/((L+1)·S_in,i)` に一致する。
        """
        return self.capital_base_usdt - self.reserve_usdt

    @property
    def deployed_capital_usdt(self) -> float:
        """§11.1: 全 trade・全 layer で同一。予備資金を含む(§11.4)。"""
        return self.capital_base_usdt


@dataclass
class SpotWallet:
    """現物ウォレット。**futures の証拠金にはならない**(§9 M7)。"""

    btc: float = 0.0
    cash_usdt: float = 0.0

    def value_usdt(self, spot_price: float) -> float:
        return self.cash_usdt + self.btc * spot_price


@dataclass
class FuturesWallet:
    """USD-M ウォレット。証拠金と perp 建玉を持つ。

    `position_btc` は**ショートを負**で表す。
    """

    margin_usdt: float = 0.0
    position_btc: float = 0.0
    entry_price: float = 0.0

    def unrealized_pnl(self, mark_price: float) -> float:
        return self.position_btc * (mark_price - self.entry_price)

    def notional(self, mark_price: float) -> float:
        return abs(self.position_btc) * mark_price

    def equity(self, mark_price: float) -> float:
        return self.margin_usdt + self.unrealized_pnl(mark_price)

    def margin_ratio(self, mark_price: float) -> float:
        notional = self.notional(mark_price)
        if notional <= 0:
            return float("inf")
        return self.equity(mark_price) / notional


@dataclass
class BarState:
    """1バーの状態記録。artifact へそのまま出す想定。"""

    ts: datetime
    spot_btc: float
    perp_btc: float
    mark_price: float
    futures_margin_usdt: float
    reserve_usdt: float
    leverage_t: float
    margin_ratio: float
    funding_cashflow: float
    cum_funding_cashflow: float
    tracking_error: float
    topped_up_usdt: float
    liquidated: bool


@dataclass
class TradeResult:
    """1 trade の結果。**恒等式の検算に必要な項をすべて保持する**。"""

    opened: bool
    reject_reason: str | None = None
    entry_fill_ts: datetime | None = None
    exit_fill_ts: datetime | None = None
    q_btc: float = 0.0
    spot_in: float = 0.0
    perp_in: float = 0.0
    spot_out: float = 0.0
    perp_out: float = 0.0
    funding_total: float = 0.0
    funding_events_applied: int = 0
    cost_spot_in: float = 0.0
    cost_perp_in: float = 0.0
    cost_spot_out: float = 0.0
    cost_perp_out: float = 0.0
    liquidation_loss: float = 0.0
    liquidated: bool = False
    topup_count: int = 0
    topup_total_usdt: float = 0.0
    lot_residual_btc: float = 0.0
    bar_states: list[BarState] = field(default_factory=list)

    @property
    def cost_total(self) -> float:
        return (
            self.cost_spot_in + self.cost_perp_in + self.cost_spot_out + self.cost_perp_out
        )

    @property
    def basis_in(self) -> float:
        """D_in = P_in − S_in(§6.2)。"""
        return self.perp_in - self.spot_in

    @property
    def basis_out(self) -> float:
        return self.perp_out - self.spot_out

    @property
    def pnl_gross(self) -> float:
        """§6.2 の恒等式そのもの: q(D_in − D_out) + Funding。"""
        return self.q_btc * (self.basis_in - self.basis_out) + self.funding_total

    @property
    def pnl_gross_by_legs(self) -> float:
        """脚ごとに積み上げた同じ量。T3 はこの2つの一致を検査する。"""
        spot_leg = self.q_btc * (self.spot_out - self.spot_in)
        perp_leg = -self.q_btc * (self.perp_out - self.perp_in)
        return spot_leg + perp_leg + self.funding_total

    @property
    def pnl_net(self) -> float:
        return self.pnl_gross - self.cost_total - self.liquidation_loss

    def net_return_on_capital(self, deployed_capital: float) -> float:
        """§17.1 の r_i。分母は全 trade 共通の拘束資本。"""
        return self.pnl_net / deployed_capital


# ---------------------------------------------------------------------------
# サイジング(§11.1 / §9 M1)
# ---------------------------------------------------------------------------


def round_to_lot(qty: float, lot_step: float) -> float:
    """lot step へ**切り下げ**る。切り上げると MIN_NOTIONAL を跨げてしまう。

    両脚を**共通の step**へ丸める(§9 M1)。perp の step が粗いのでそちらに合わせる。
    """
    if lot_step <= 0:
        raise ValueError("lot_step は正でなければならない")
    steps = int(qty / lot_step)  # 切り下げ(qty >= 0 を前提)
    return steps * lot_step


def size_position(spot_in: float, cfg: TwoLegConfig) -> tuple[float, float]:
    """§11.1 のサイジング。戻り値は (丸め後 q, 丸め残差)。

        q_raw = position_capital · L / ((L + 1) · S_in)

    `reserve_usdt == 0` なら §11.1 の式そのものである。
    """
    if spot_in <= 0:
        raise ValueError("spot_in は正でなければならない")
    q_raw = cfg.position_capital_usdt * cfg.leverage / ((cfg.leverage + 1.0) * spot_in)
    q = round_to_lot(q_raw, cfg.lot_step)
    return q, q_raw - q


# ---------------------------------------------------------------------------
# funding(§8.1)
# ---------------------------------------------------------------------------


def _funding_in_window(
    events: Sequence[FundingEvent],
    entry_fill_ts: datetime,
    exit_fill_ts: datetime,
) -> list[FundingEvent]:
    """§8.1 の帰属規則: ``entry_fill_time < s <= exit_fill_time``。

    決済 `s` は **`s` で終わる区間**の保有を精算するので、
    建てた瞬間の決済は受け取らず、決済した瞬間に解消した分は受け取る(監査 Y6)。
    按分はしない。
    """
    return [e for e in events if entry_fill_ts < e.settlement_ts <= exit_fill_ts]


def _funding_cashflow(event: FundingEvent, q_btc: float) -> float:
    """short perp の受払。``+ q · markPrice(s) · f(s)``。

    `f > 0` で short の受取。**同じ行の `markPrice` を使う**(A3 レビュー S2)。
    `interval_hours` は帰属判定には使わないが、行ごとに保持して artifact に出す
    (X5: 8 をハードコードしないことの担保)。
    """
    return q_btc * event.mark_price * event.funding_rate


# ---------------------------------------------------------------------------
# 約定(§5.1 / §9 M6a / M6b)
# ---------------------------------------------------------------------------


def _next_fill_bar(bars: Sequence[Bar], signal_ts: datetime, cfg: TwoLegConfig) -> Bar | None:
    """signal の**次に存在し、かつ両脚が揃う**バーを返す。

    - §5.1: fill は `open[t+1]`。signal バー自身では約定しない。
    - §9 M6b: 片脚が無いバーは飛ばして roll-forward する。
    - 打ち切りは既存 `ExecutionConfig.cancel_after_ms` を再利用する(監査 Y28)。
    """
    limit = signal_ts + timedelta(milliseconds=cfg.execution.cancel_after_ms)
    for bar in bars:
        if bar.ts <= signal_ts:
            continue
        if bar.ts > limit:
            return None
        if bar.both_legs_present:
            return bar
    return None


def _liquidation_price(entry_price: float, margin_usdt: float, q_btc: float, mmr: float) -> float:
    """short perp が清算される mark 価格。

    equity(m) = margin + q(entry − m) が維持証拠金 mmr·q·m を割る点:
        margin + q·entry − q·m = mmr·q·m
        m = (margin + q·entry) / (q(1 + mmr))
    """
    if q_btc <= 0:
        return float("inf")
    return (margin_usdt + q_btc * entry_price) / (q_btc * (1.0 + mmr))


def simulate_trade(
    bars: Sequence[Bar],
    funding: Sequence[FundingEvent],
    entry_signal_ts: datetime,
    exit_signal_ts: datetime,
    cfg: TwoLegConfig,
) -> TradeResult:
    """1 trade を決定的に会計する。

    **シグナルは与えられる。** 本関数は閾値も ρ も評価しない(それは runner の仕事)。
    ここで実装するのは §5.1 / §6 / §7.1 / §8.1 / §9 / §11 の会計だけである。

    バー内の処理順序(**保守側に固定**):
        1. 清算判定(mark の不利側 intrabar 極値)
        2. 追証(予備資金から)
        3. funding の受払
    funding を先に入れると、その分だけ清算が起きにくくなる。short の funding は
    平均的に受取なので、**清算判定を先に置くのが保守側**である。
    """
    result = TradeResult(opened=False)

    entry_bar = _next_fill_bar(bars, entry_signal_ts, cfg)
    if entry_bar is None:
        # §9 M6a: entry 時に両脚が揃わなければ**建てない**(現在情報のみ)
        result.reject_reason = "no_entry_fill_bar_with_both_legs"
        return result

    spot_in = float(entry_bar.spot_open)  # type: ignore[arg-type]
    perp_in = float(entry_bar.perp_open)  # type: ignore[arg-type]

    q, residual = size_position(spot_in, cfg)
    if q <= 0:
        result.reject_reason = "quantity_rounds_to_zero"
        return result
    if q * perp_in < cfg.min_notional_usdt:
        # §11.1: 丸め後の名目が MIN_NOTIONAL を割る trade は棄却する
        result.reject_reason = "below_min_notional"
        result.q_btc = q
        result.lot_residual_btc = residual
        return result

    result.opened = True
    result.entry_fill_ts = entry_bar.ts
    result.q_btc = q
    result.spot_in = spot_in
    result.perp_in = perp_in
    result.lot_residual_btc = residual

    # --- 開始時のウォレット(**分離して持つ**。A3 レビュー S5) -----------------
    spot_w = SpotWallet(btc=q, cash_usdt=0.0)
    initial_margin = q * perp_in / cfg.leverage
    fut_w = FuturesWallet(margin_usdt=initial_margin, position_btc=-q, entry_price=perp_in)
    reserve = cfg.reserve_usdt
    funding_outside_margin = 0.0

    result.cost_spot_in = q * spot_in * cfg.cost.spot_fraction()
    result.cost_perp_in = q * perp_in * cfg.cost.perp_fraction()

    exit_bar = _next_fill_bar(bars, exit_signal_ts, cfg)
    # M6b: exit 側で両脚が揃わなくても **entry を遡って無効化しない**。
    # roll-forward で見つからなければ、最後に両脚が揃ったバーで決済する。
    if exit_bar is None:
        candidates = [b for b in bars if b.both_legs_present and b.ts > entry_bar.ts]
        if not candidates:
            result.reject_reason = "no_exit_fill_bar_with_both_legs"
            exit_bar = entry_bar
        else:
            exit_bar = candidates[-1]

    cum_funding = 0.0
    applied: set[datetime] = set()

    for bar in bars:
        if bar.ts < entry_bar.ts or bar.ts > exit_bar.ts:
            continue
        mark = bar.mark_close if bar.mark_close is not None else bar.perp_close
        mark_adverse = bar.mark_high if bar.mark_high is not None else mark
        if mark is None or mark_adverse is None:
            continue

        topped = 0.0
        liquidated_here = False

        if not result.liquidated:
            # 1. 清算判定。**mark の不利側 intrabar 極値**で行う(§11.3 / Y38)
            ratio_adverse = fut_w.margin_ratio(float(mark_adverse))

            # 2. 追証(§11.4)。予備資金から topup_target まで戻す
            if ratio_adverse < cfg.topup_trigger and reserve > 0:
                notional = fut_w.notional(float(mark_adverse))
                needed = cfg.topup_target * notional - fut_w.equity(float(mark_adverse))
                topped = min(max(needed, 0.0), reserve)
                fut_w.margin_usdt += topped
                reserve -= topped
                result.topup_count += 1 if topped > 0 else 0
                result.topup_total_usdt += topped
                ratio_adverse = fut_w.margin_ratio(float(mark_adverse))

            # 予備資金が尽きて維持証拠金を割れば清算を受け入れる(§11.4)
            if ratio_adverse <= cfg.maint_margin_rate:
                liquidated_here = True

        if liquidated_here:
            trigger = _liquidation_price(
                fut_w.entry_price, fut_w.margin_usdt, q, cfg.maint_margin_rate
            )
            # 強制決済はトリガー価格 + スリッページより良い価格では約定しない(§11.3)
            fill = trigger * (1.0 + cfg.liquidation_slippage_bps * 1e-4)
            result.liquidated = True
            result.liquidation_loss = max(
                0.0, -(fut_w.margin_usdt + fut_w.unrealized_pnl(fill))
            )
            fut_w.margin_usdt = max(0.0, fut_w.margin_usdt + fut_w.unrealized_pnl(fill))
            fut_w.position_btc = 0.0
            if cfg.post_liquidation == "rehedge":
                raise NotImplementedError(
                    "post_liquidation='rehedge' の再建玉規則は凍結プロトコルに無い"
                    "(§11.3 は『どちらかを事前に選ぶ』と述べるが値が凍結されていない)。"
                    "UNFROZEN_PARAMETERS を参照。"
                )

        # 3. funding(§8.1)。entry < s <= bar.ts のうち未適用のもの
        for ev in _funding_in_window(funding, entry_bar.ts, bar.ts):
            if ev.settlement_ts in applied or result.liquidated:
                continue
            applied.add(ev.settlement_ts)
            cash = _funding_cashflow(ev, q)
            cum_funding += cash
            result.funding_events_applied += 1
            if cfg.funding_counts_toward_margin:
                fut_w.margin_usdt += cash
            else:
                funding_outside_margin += cash

        spot_px = bar.spot_close if bar.spot_close is not None else spot_in
        perp_val = abs(fut_w.position_btc) * float(mark)
        spot_val = spot_w.btc * float(spot_px)
        tracking = abs(spot_val - perp_val) / spot_val if spot_val > 0 else 0.0
        balance = fut_w.margin_usdt
        result.bar_states.append(
            BarState(
                ts=bar.ts,
                spot_btc=spot_w.btc,
                perp_btc=fut_w.position_btc,
                mark_price=float(mark),
                futures_margin_usdt=fut_w.margin_usdt,
                reserve_usdt=reserve,
                leverage_t=(perp_val / balance) if balance > 0 else float("inf"),
                margin_ratio=fut_w.margin_ratio(float(mark)),
                funding_cashflow=cum_funding
                - (result.bar_states[-1].cum_funding_cashflow if result.bar_states else 0.0),
                cum_funding_cashflow=cum_funding,
                tracking_error=tracking,
                topped_up_usdt=topped,
                liquidated=result.liquidated,
            )
        )

    result.exit_fill_ts = exit_bar.ts
    result.spot_out = float(exit_bar.spot_open)  # type: ignore[arg-type]
    result.perp_out = float(exit_bar.perp_open)  # type: ignore[arg-type]
    result.funding_total = cum_funding
    result.cost_spot_out = q * result.spot_out * cfg.cost.spot_fraction()
    result.cost_perp_out = q * result.perp_out * cfg.cost.perp_fraction()
    return result

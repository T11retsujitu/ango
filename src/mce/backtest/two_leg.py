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
# 凍結プロトコル v1.8.1 に値が**まだ**無いパラメータ(H14)
#
# v1.8 では 3件(reserve / funding-margin / post-liquidation)が未凍結だったが、
# **v1.8.1 §24.1–24.3 ですべて凍結された**。残るのは H14(清算コスト)だけである。
#
# v1.8.5 §31 で H14 は H14a / H14b へ分割され、**H14b は解決した**:
# 執行価格は観測された約定価格の不利側極値を破産価格で頭打ちにして決まる。
# **固定の `liquidation_slippage_bps` は廃止した**(市場状態依存であり、
# 取引所の固定パラメータではないため)。
#
# 残るのは H14a(清算 clearance fee 率)だけである。既定値を持たない。
# ---------------------------------------------------------------------------
UNFROZEN_PARAMETERS: tuple[str, ...] = (
    "liquidation_clearance_fee_rate",  # §34 H14a: 権威ある率が未取得
)


@dataclass(frozen=True)
class Bar:
    """5分バー1本。spot と perp を**別々の価格として**保持する。

    `mark_high` は markPriceKlines 由来。清算判定はこれで行い、
    `perp_close`(last price)では行わない(§11.3 / 監査 Y38)。
    いずれかが None のバーは「片脚が無い」扱いになる(§9 M6a / M6b)。

    v1.8.5 §31:

    - **`mark_high` は清算判定にのみ使う**(trigger-only)
    - **`perp_high` は執行価格の代理にのみ使う**(execution-proxy-only)

    **この2つを入れ替えてはならない。** 入れ替えると、板に無い価格で約定した
    ことにするか、清算されない局面で清算したことにするかのどちらかになる。

    `mark_path_status` は §32 の品質状態。**合成バーは既定で "observed"**
    (合成した時点で mark 経路は完全に指定されているため)。経験データの
    loader は canonical タイムラインから**必ず明示的に**設定する。
    """

    ts: datetime
    spot_open: float | None
    spot_close: float | None
    perp_open: float | None
    perp_close: float | None
    perp_high: float | None = None  # 約定価格の高値。**執行代理専用**
    mark_high: float | None = None  # mark の高値。**清算判定専用**
    mark_close: float | None = None
    mark_path_status: str = "observed"  # §32

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
    """執行器の設定。凍結値は `phase8_prereg` から既定を取る(v1.8.1)。

    `liquidation_clearance_fee_rate` だけは **既定を持たない**
    (H14a 未解決。`UNFROZEN_PARAMETERS`)。
    """

    cost: TwoLegCostConfig
    liquidation_clearance_fee_rate: float | None
    capital_base_usdt: float = P.CAPITAL_BASE_USDT
    reserve_usdt: float = P.MARGIN_RESERVE_USDT
    funding_counts_toward_margin: bool = P.FUNDING_COUNTS_TOWARD_MARGIN
    post_liquidation: PostLiquidationRule = P.POST_LIQUIDATION_RULE
    leverage: float = P.LEVERAGE
    lot_step: float = P.PERP_LOT_STEP
    min_notional_usdt: float = P.PERP_MIN_NOTIONAL_USDT
    maint_margin_rate: float = P.MAINT_MARGIN_RATE_TIER1
    topup_trigger: float = P.MARGIN_TOPUP_TRIGGER
    topup_target: float = P.MARGIN_TOPUP_TARGET
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
        if self.post_liquidation != "unwind":
            raise ValueError(
                "v1.8.1 §24.3 は POST_LIQUIDATION_RULE = 'unwind' を凍結している。"
                "再ヘッジは実装しない。"
            )

    @property
    def liquidation_cost_is_resolved(self) -> bool:
        """H14a が解決済みか。**滑りはもう設定項目ではない**(§31)。"""
        return self.liquidation_clearance_fee_rate is not None

    def require_liquidation_cost(self) -> float:
        """H14a の clearance fee 率を取り出す。未解決なら明示的に落とす。

        **ゼロでの代替は行わない**(§34)。
        """
        if not self.liquidation_cost_is_resolved:
            raise ValueError(
                "H14a 未解決: 清算 clearance fee 率が凍結されていない(§34)。"
                "ゼロで代替してはならない。"
            )
        return float(self.liquidation_clearance_fee_rate)  # type: ignore[arg-type]

    @property
    def position_capital_usdt(self) -> float:
        """建玉に充てる資本 `C − R`(§24.1)。primary では 8000。"""
        return self.capital_base_usdt - self.reserve_usdt

    @property
    def deployed_capital_usdt(self) -> float:
        """§11.1 / §24.1: 予備資金を含む。全 trade・全 layer で同一。"""
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
    """1 trade の結果。**恒等式の検算に必要な項をすべて保持する**。

    v1.8.1 §24.5: 清算経路では
      - perp 脚は **清算約定**で終了する(予定 exit 価格を使わない)
      - 通常の `cost_perp_out` は**計上しない**
      - 価格損失は `q(P_in − P_liq)` に**一度だけ**現れる
      - `liquidation_fee_usdt` は **clearance fee のみ**を表す(PnL を二重計上しない)
    """

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
    # --- v1.8.1 G5: 清算経路の明示フィールド ---
    liquidated: bool = False
    liquidation_ts: datetime | None = None
    liquidation_fill: float | None = None
    liquidation_fee_usdt: float = 0.0
    spot_unwind_ts: datetime | None = None
    spot_unwind_fill: float | None = None
    naked_spot_bars: int = 0
    topup_count: int = 0
    topup_total_usdt: float = 0.0
    lot_residual_btc: float = 0.0
    # --- v1.8.5 §31 / §32 ---
    fill_rule_binding: str | None = None  # "floor" / "observed" / "cap"
    disposition: str | None = None  # "liquidation_state_unknown" など
    unobservable_mark_bars: int = 0
    bar_states: list[BarState] = field(default_factory=list)

    @property
    def cost_total(self) -> float:
        """4約定の手数料。**清算時は cost_perp_out を含まない**(§24.5 b)。"""
        return (
            self.cost_spot_in + self.cost_perp_in + self.cost_spot_out + self.cost_perp_out
        )

    @property
    def basis_in(self) -> float:
        """D_in = P_in − S_in(§6.2)。"""
        return self.perp_in - self.spot_in

    @property
    def basis_out(self) -> float:
        """D_out = P_exit_actual − S_exit_actual。

        清算経路では `perp_out` が清算約定、`spot_out` が巻き戻し約定になる
        (脚が別時刻でも恒等式は保たれる。§24.5)。
        """
        return self.perp_out - self.spot_out

    @property
    def pnl_gross(self) -> float:
        """§6.2 の恒等式そのもの: q(D_in − D_out) + Funding。"""
        return self.q_btc * (self.basis_in - self.basis_out) + self.funding_total

    @property
    def pnl_gross_by_legs(self) -> float:
        """脚ごとに積み上げた同じ量。T3 / T35 はこの2つの一致を検査する。"""
        spot_leg = self.q_btc * (self.spot_out - self.spot_in)
        perp_leg = -self.q_btc * (self.perp_out - self.perp_in)
        return spot_leg + perp_leg + self.funding_total

    @property
    def pnl_net(self) -> float:
        """手数料と清算手数料を引く。**価格損失は pnl_gross に一度だけ入っている**。"""
        return self.pnl_gross - self.cost_total - self.liquidation_fee_usdt

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

        q_raw = (C − R) · L / ((L + 1) · S_in)          … v1.8.1 §24.1

    丸め前は `q_raw · S_in · (1 + 1/L) == POSITION_CAPITAL_USDT` が**全ての L で厳密**。
    基準は `C` ではなく `C − R` である(T16 の修正。§24.1)。
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


def _first_executable_spot_open(
    bars: Sequence[Bar], after_ts: datetime
) -> tuple[datetime, float] | None:
    """清算バーより**後**で最初に因果的に執行可能な spot open(§24.3)。

    §9 M6b の roll-forward 意味論をそのまま使う(両脚が揃うことは要求しない —
    perp 脚は既に消えているので spot open があれば足りる)。
    """
    for bar in bars:
        if bar.ts > after_ts and bar.spot_open is not None:
            return bar.ts, float(bar.spot_open)
    return None


def simulate_trade(
    bars: Sequence[Bar],
    funding: Sequence[FundingEvent],
    entry_signal_ts: datetime,
    exit_signal_ts: datetime,
    cfg: TwoLegConfig,
) -> TradeResult:
    """1 trade を決定的に会計する。

    **シグナルは与えられる。** 本関数は閾値も ρ も評価しない(runner の責務)。

    バー内の処理順序(**v1.8.1 §24.4 で凍結**):
        1. 適格な funding を先物ウォレットへ適用する
        2. 不利側 mark 経路で証拠金を評価する
        3. TOPUP_TRIGGER(維持証拠金より上)を先に処理する
        4. 追証の後、なお維持証拠金以下なら清算する

    v1.8 の実装と note は「清算 → 追証 → funding」と書いていたが誤りであり、
    §24.4 が上記の順序を正とした。
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

    # --- ウォレットは**分離**して持つ(A3 レビュー S5) ------------------------
    spot_w = SpotWallet(btc=q, cash_usdt=0.0)
    fut_w = FuturesWallet(
        margin_usdt=q * perp_in / cfg.leverage, position_btc=-q, entry_price=perp_in
    )
    reserve = cfg.reserve_usdt

    result.cost_spot_in = q * spot_in * cfg.cost.spot_fraction()
    result.cost_perp_in = q * perp_in * cfg.cost.perp_fraction()

    scheduled_exit = _next_fill_bar(bars, exit_signal_ts, cfg)
    if scheduled_exit is None:
        candidates = [b for b in bars if b.both_legs_present and b.ts > entry_bar.ts]
        if not candidates:
            result.reject_reason = "no_exit_fill_bar_with_both_legs"
            scheduled_exit = entry_bar
        else:
            scheduled_exit = candidates[-1]

    cum_funding = 0.0
    applied: set[datetime] = set()

    for bar in bars:
        if bar.ts < entry_bar.ts or bar.ts > scheduled_exit.ts:
            continue
        if result.liquidated:
            break
        # --- §32/§33 gate 1: mark 経路の観測可能性を**最初に**見る -----------
        # 観測できない経路を「清算が起きなかった」と数えてはならない。
        if bar.mark_path_status not in P.MARK_PATH_ACCEPTABLE:
            result.disposition = P.LIQUIDATION_STATE_UNKNOWN_DISPOSITION
            result.unobservable_mark_bars += 1
            break

        mark = bar.mark_close if bar.mark_close is not None else bar.perp_close
        mark_adverse = bar.mark_high if bar.mark_high is not None else mark
        if mark is None or mark_adverse is None:
            continue

        bar_funding = 0.0
        topped = 0.0

        # --- 1. funding を先に適用する(§24.4)-------------------------------
        for ev in _funding_in_window(funding, entry_bar.ts, bar.ts):
            if ev.settlement_ts in applied:
                continue
            applied.add(ev.settlement_ts)
            cash = _funding_cashflow(ev, q)
            cum_funding += cash
            bar_funding += cash
            result.funding_events_applied += 1
            if cfg.funding_counts_toward_margin:
                # 正負いずれも先物ウォレットを動かす(§24.2)
                fut_w.margin_usdt += cash

        # --- 2/3. 不利側 mark で証拠金を評価し、追証を先に処理する ------------
        ratio = fut_w.margin_ratio(float(mark_adverse))
        if ratio < cfg.topup_trigger and reserve > 0:
            notional = fut_w.notional(float(mark_adverse))
            needed = cfg.topup_target * notional - fut_w.equity(float(mark_adverse))
            topped = min(max(needed, 0.0), reserve)
            if topped > 0:
                fut_w.margin_usdt += topped
                reserve -= topped
                result.topup_count += 1
                result.topup_total_usdt += topped
                ratio = fut_w.margin_ratio(float(mark_adverse))

        # --- 4. 追証の後、なお維持証拠金以下なら清算する ----------------------
        if ratio <= cfg.maint_margin_rate:
            fee_rate = cfg.require_liquidation_cost()
            trigger = _liquidation_price(
                fut_w.entry_price, fut_w.margin_usdt, q, cfg.maint_margin_rate
            )
            bankruptcy = trigger * (1.0 + cfg.maint_margin_rate)
            # **執行は約定価格の不利側極値**(§31)。mark ではない。
            if bar.perp_high is None:
                result.disposition = P.LIQUIDATION_STATE_UNKNOWN_DISPOSITION
                result.unobservable_mark_bars += 1
                break
            candidate = max(trigger, float(bar.perp_high))
            fill = min(candidate, bankruptcy)
            result.fill_rule_binding = (
                "cap" if candidate > bankruptcy
                else "floor" if candidate <= trigger
                else "observed"
            )
            result.liquidated = True
            result.liquidation_ts = bar.ts
            result.liquidation_fill = fill
            # clearance fee のみ。価格損失は q(P_in − P_liq) に既に入っている(§24.5 c)
            result.liquidation_fee_usdt = fee_rate * q * fill
            result.perp_out = fill  # 予定 exit 価格は使わない(§24.5 a)
            fut_w.margin_usdt = max(0.0, fut_w.margin_usdt + fut_w.unrealized_pnl(fill))
            fut_w.position_btc = 0.0

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
                leverage_t=(perp_val / balance) if balance > 0 else 0.0,
                margin_ratio=fut_w.margin_ratio(float(mark)),
                funding_cashflow=bar_funding,
                cum_funding_cashflow=cum_funding,
                tracking_error=tracking,
                topped_up_usdt=topped,
                liquidated=result.liquidated,
            )
        )

    result.funding_total = cum_funding

    if result.liquidated:
        # --- G3/§24.3: 清算バーの後、最初に因果的に執行可能な spot open で巻き戻す
        unwind = _first_executable_spot_open(bars, result.liquidation_ts)  # type: ignore[arg-type]
        if unwind is None:
            result.reject_reason = "no_spot_unwind_bar_after_liquidation"
            result.spot_unwind_ts = result.liquidation_ts
            result.spot_out = float(
                next(b.spot_close for b in bars if b.ts == result.liquidation_ts)  # type: ignore[arg-type]
            )
        else:
            result.spot_unwind_ts, result.spot_out = unwind
        result.spot_unwind_fill = result.spot_out
        result.exit_fill_ts = result.spot_unwind_ts
        # naked spot(perp 消失〜spot 解消)の本数を記録する(§24.3)
        result.naked_spot_bars = sum(
            1
            for b in bars
            if result.liquidation_ts < b.ts < result.spot_unwind_ts  # type: ignore[operator]
        )
        # spot 側の手数料は通常どおり。**perp 側の cost_perp_out は計上しない**(§24.5 b)
        result.cost_spot_out = q * result.spot_out * cfg.cost.spot_fraction()
        result.cost_perp_out = 0.0
        return result

    result.exit_fill_ts = scheduled_exit.ts
    result.spot_out = float(scheduled_exit.spot_open)  # type: ignore[arg-type]
    result.perp_out = float(scheduled_exit.perp_open)  # type: ignore[arg-type]
    result.cost_spot_out = q * result.spot_out * cfg.cost.spot_fraction()
    result.cost_perp_out = q * result.perp_out * cfg.cost.perp_fraction()
    return result

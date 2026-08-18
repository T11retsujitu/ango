"""transaction cost model(bps 建て・成分分離)。

- 成分: exchange fee / spread proxy / slippage proxy(いずれも片道 bps)。
  funding は Phase 0 では PnL 外(Q4 の決定。config に枠のみ、既定 0)。
  market impact はモデル化せず、シナリオ(stress)の感応度で扱う。
- 適用: net_return[i] = gross_return[i] − per_side_rate × turnover[i]
  (turnover は |Δposition|。片道1単位の約定 = 片道コスト1回)
- break-even cost: 総 gross PnL を総 turnover で割った「PnL がゼロになる
  片道コスト」(bps)。重要 metric として artifact に記録する。

シナリオの既定値は OKX BTC-USDT-SWAP の運用基準(docs/findings の恒久ルール5:
taker 5bps/片道)に合わせた config であり、真値ではない。変更時は ADR に記録する。
"""

from dataclasses import dataclass

import polars as pl


@dataclass(frozen=True)
class CostConfig:
    name: str
    fee_bps: float = 0.0  # 片道
    spread_bps: float = 0.0  # 片道(半スプレッド相当)
    slippage_bps: float = 0.0  # 片道
    funding_bps_per_bar: float = 0.0  # Phase 0 では 0 固定(枠のみ)

    @property
    def per_side_bps(self) -> float:
        return self.fee_bps + self.spread_bps + self.slippage_bps

    @property
    def per_side_rate(self) -> float:
        return self.per_side_bps * 1e-4

    @property
    def roundtrip_bps(self) -> float:
        return 2 * self.per_side_bps


SCENARIOS: dict[str, CostConfig] = {
    "zero": CostConfig("zero"),
    "maker_low": CostConfig("maker_low", fee_bps=1.0),  # maker 往復2bps(findings 準拠)
    "base_taker": CostConfig("base_taker", fee_bps=5.0),  # taker 5bps/片道(恒久ルール5)
    "stress": CostConfig("stress", fee_bps=5.0, spread_bps=2.0, slippage_bps=3.0),  # 10bps/片道
}


def apply_costs(bar_df: pl.DataFrame, cfg: CostConfig) -> pl.DataFrame:
    """bar_returns の出力(gross_return, turnover)に net_return / cost 列を足す。"""
    if cfg.funding_bps_per_bar:
        raise NotImplementedError("funding は Phase 0 では PnL 外(感応度分析で扱う)")
    return bar_df.with_columns(
        (pl.col("turnover") * cfg.per_side_rate).alias("cost"),
    ).with_columns((pl.col("gross_return") - pl.col("cost")).alias("net_return"))


def break_even_cost_bps(bar_df: pl.DataFrame) -> float | None:
    """総 net PnL がゼロになる片道コスト(bps)。turnover ゼロなら None。"""
    total_turnover = bar_df["turnover"].sum()
    if total_turnover == 0:
        return None
    return float(bar_df["gross_return"].sum() / total_turnover * 1e4)


# ---------------------------------------------------------------------------
# Phase 8.1 two-leg carry(凍結プロトコル §7.1)
#
# ADR 補記: 本モジュール冒頭の「funding は PnL 外(Q4 の決定)」は、
# 単一銘柄の `CostConfig` パイプラインについての決定である。**Phase 8.1 の
# two-leg carry PnL については、凍結プロトコル §8 が funding を PnL の中心に
# 置くため、その範囲でのみ Q4 の ADR を supersede する**(監査 Y32)。
# `CostConfig` / `apply_costs` の挙動は一切変更していない。
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TwoLegCostConfig:
    """spot 脚と perp 脚に**別々**の片道コストを持つ設定(§7.1)。

    単一の `CostConfig` に畳まないのは、Binance の spot taker(0.100%)と
    USD-M taker が**異なる**ためである(§7.2 / 監査 Y36)。
    """

    name: str
    spot_bps: float  # 片道
    perp_bps: float  # 片道
    transfer_bps: float = 0.0  # Binance 内 spot<->futures 振替。仮定を可視化する(Y42)

    @property
    def round_trip_bps(self) -> float:
        """4約定(entry 2 + exit 2)の合計 bps。"""
        return 2.0 * (self.spot_bps + self.perp_bps)

    @property
    def round_trip_fraction(self) -> float:
        """A2 の no-arbitrage 帯に渡す往復コスト C(小数)。"""
        return self.round_trip_bps * 1e-4

    def spot_fraction(self) -> float:
        return self.spot_bps * 1e-4

    def perp_fraction(self) -> float:
        return self.perp_bps * 1e-4

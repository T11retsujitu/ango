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

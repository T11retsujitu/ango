"""Binance Vision dump → 共通スキーマ Parquet(Phase 7 Tier 0)。

    python -m mce.normalize_binance

`data/raw/binance/vision/<dataset>/<symbol>/*.zip` を読み、
`data/normalized/binance/` へ冪等マージする。補間はしない。

時刻規約(docs/data_contract.md §2 を Binance へ適用):

- klines / premiumIndexKlines の `open_time` は**バー開始時刻**。行 t は
  `[ts, ts+5m)` を表す(OKX candle と同じ規約)。`close_time` は `ts+5m-1ms`。
- metrics の `create_time` は**5分ごとのスナップショット時刻**(UTC)。
  値は「その時刻の状態」であり区間集約ではない(`sum_taker_long_short_vol_ratio`
  だけは直前区間の集約とみなすのが自然だが、いずれも `ts` 時点で確定しているため
  観測可能性の判定は同じ)。
- fundingRate の `calc_time` は**決済時刻**(protocol X4)。**バー開始時刻ではない。**
  `funding_rate` は5分グリッド上の系列ではなく**イベント系列**である。

**封印の継承**: 正規化時点で `ts >= FINAL_OOS_START (2026-01-01)` の行を落とす。
別 venue のデータでも Final OOS と同じ暦期間を screening で見ないため
(docs/phase7/information_space_expansion_v1.md §6.3)。落とした行数は記録する。

単位(そのまま保持し、換算しない):

- klines `volume` は BTC 建て、`volume_quote` / `taker_buy_quote` は USDT 建て
- metrics `open_interest` は BTC 建て、`open_interest_value` は USDT 建て
- **premiumIndexKlines の volume / trades / taker 系は常に 0** であり、
  約定フローではない(`premium_samples` として件数のみ保持する)
"""

import argparse
import csv
import zipfile
from datetime import datetime
from pathlib import Path

import polars as pl

from mce import config, store
from mce.backtest.splits import FINAL_OOS_START
from mce.binance_vision import DATASETS, DEFAULT_SYMBOL, MARKET_TYPE, SOURCE, raw_dir

_TS = pl.Datetime(time_unit="ms", time_zone="UTC")
BAR_MS = 5 * 60 * 1000
HOUR_MS = 60 * 60 * 1000

#: 重複排除キー(docs/data_contract.md §6)。
#: **`market_type` を含む**(Y23)。spot と perp はどちらも `source="binance"` /
#: `symbol="BTCUSDT"` なので、`market_type` が無いと spot が perp を上書きしうる。
#: 契約と実装は同一コミットで一致させる。
KEY_COLS = ["source", "symbol", "market_type", "ts"]

# kline CSV の列順(header 有無どちらの版もこの順序)
_KLINE_COLUMNS = 12
_METRIC_COLUMNS = 8
#: fundingRate CSV の列数。実測 header: calc_time,funding_interval_hours,last_funding_rate
_FUNDING_COLUMNS = 3


class BinanceNormalizationError(ValueError):
    pass


def read_zip_rows(path: Path, expected_columns: int) -> list[list[str]]:
    """zip 内の単一 CSV を行のリストへ。古い dump は header 行が無い。"""
    with zipfile.ZipFile(path) as z:
        names = [n for n in z.namelist() if n.lower().endswith(".csv")]
        if len(names) != 1:
            raise BinanceNormalizationError(f"{path}: CSV が1つではない {names}")
        text = z.read(names[0]).decode()
    # 実 dump には引用符つきの空セル("")があるので split(",") ではなく CSV parser を使う
    rows = [row for row in csv.reader(text.splitlines()) if row]
    if rows and _is_header(rows[0]):
        rows = rows[1:]
    for row in rows:
        if len(row) != expected_columns:
            raise BinanceNormalizationError(
                f"{path}: 列数 {len(row)} != {expected_columns}: {row[:3]}"
            )
    return rows


# header 行の先頭セル(metrics の1列目はデータ側も非数値なので、名前で判定する)
# fundingRate dump は header 付き("calc_time,...")で公開されている(実測)。
_HEADER_FIRST_CELLS = {"open_time", "create_time", "calc_time"}


def _is_header(row: list[str]) -> bool:
    return row[0].strip().lower() in _HEADER_FIRST_CELLS


def _epoch_ms(value: str) -> int:
    """ms / µs のどちらで書かれていても ms へ揃える(2025年以降の µs 版対策)。"""
    raw = int(value)
    if raw > 10**14:  # µs(~1.7e15)は ms(~1.7e12)より3桁大きい
        if raw % 1000 not in (0, 999):
            raise BinanceNormalizationError(f"µs timestamp を ms へ落とせない: {raw}")
        return raw // 1000
    return raw


def _provenance(
    df: pl.DataFrame, symbol: str, market_type: str = MARKET_TYPE
) -> pl.DataFrame:
    """出所を行に焼き込む。**market_type は dataset ごとに違う**(spot / perp_linear)。"""
    return df.with_columns(
        pl.lit(symbol).alias("symbol"),
        pl.lit(SOURCE).alias("source"),
        pl.lit(market_type).alias("market_type"),
    ).select(["ts", *[c for c in df.columns if c != "ts"], "symbol", "source", "market_type"])


def _check_close_time(
    open_ms: list[int], close_ms: list[int], policy: str, stats: dict | None
) -> None:
    """`close_time` を dump の意味論に従って検査し、逸脱を**分類して数える**。

    - `"exact"`: `close_time == open_time + 5m - 1ms` を要求する
      (Phase 7 の perp / premium と F1 の mark。**挙動を変えない**)
    - `"last_trade_time"`: `close_time` は**そのバーの最終約定時刻**であって
      バー終端ではない。この場合は `open_time` が5分グリッド上にあることを
      不変条件とし、`close_time` の逸脱は**落とさずに分類して記録する**。
      空バー(出来高0)では最終約定が `open_time` より前になることがある。
    """
    if policy == "exact":
        for o, c in zip(open_ms, close_ms):
            if c != o + BAR_MS - 1:
                raise BinanceNormalizationError(
                    f"close_time が open_time+5m-1ms でない: {o} {c}"
                )
        return
    if policy != "last_trade_time":
        raise BinanceNormalizationError(f"未知の close_time policy: {policy!r}")
    not_bar_end = before_open = 0
    for o, c in zip(open_ms, close_ms):
        if o % BAR_MS != 0:
            raise BinanceNormalizationError(f"open_time が5分グリッド上にない: {o}")
        if c != o + BAR_MS - 1:
            not_bar_end += 1
            if c < o:
                before_open += 1
    if stats is not None:
        stats["close_time_not_bar_end_rows"] = (
            stats.get("close_time_not_bar_end_rows", 0) + not_bar_end
        )
        stats["close_time_before_open_rows"] = (
            stats.get("close_time_before_open_rows", 0) + before_open
        )


def normalize_klines(
    rows: list[list[str]],
    symbol: str = DEFAULT_SYMBOL,
    market_type: str = MARKET_TYPE,
    *,
    close_time_policy: str = "exact",
    stats: dict | None = None,
) -> pl.DataFrame:
    """kline 行 → 共通形式(taker buy 数量と約定件数を保持する)。

    perp(`klines_5m`)と spot(`spot_klines_5m`)で**同じ列構成**を使い、
    区別は `market_type` 列と出力ファイルで行う。
    `close_time` の意味論は dump ごとに違うので `close_time_policy` で切り替える。
    """
    if not rows:
        return pl.DataFrame()
    open_ms = [_epoch_ms(r[0]) for r in rows]
    close_ms = [_epoch_ms(r[6]) for r in rows]
    _check_close_time(open_ms, close_ms, close_time_policy, stats)
    df = pl.DataFrame(
        {
            "ts": open_ms,
            "open": [float(r[1]) for r in rows],
            "high": [float(r[2]) for r in rows],
            "low": [float(r[3]) for r in rows],
            "close": [float(r[4]) for r in rows],
            "volume": [float(r[5]) for r in rows],
            "volume_quote": [float(r[7]) for r in rows],
            "trades": [int(r[8]) for r in rows],
            "taker_buy_volume": [float(r[9]) for r in rows],
            "taker_buy_quote": [float(r[10]) for r in rows],
        }
    ).with_columns(pl.col("ts").cast(_TS))
    return _provenance(df, symbol, market_type)


def normalize_mark_price(
    rows: list[list[str]],
    symbol: str = DEFAULT_SYMBOL,
    market_type: str = MARKET_TYPE,
    *,
    close_time_policy: str = "exact",
    stats: dict | None = None,
) -> pl.DataFrame:
    """markPriceKlines 行 → **mark 価格**の OHLC(F1)。

    **これは約定価格ではない。** 清算トリガーの判定にだけ使う値である。
    `volume` / `quote_volume` / `taker_*` は常に 0(mark は板の約定ではない)なので
    捨てる。`count` は5分間の mark サンプル数(300 = 毎秒1本)であり、
    `mark_samples` として保持する。

    列名を `mark_open/high/low/close` にするのは、**約定価格の
    `open/high/low/close` と取り違えられないようにするため**である。
    """
    if not rows:
        return pl.DataFrame()
    open_ms = [_epoch_ms(r[0]) for r in rows]
    close_ms = [_epoch_ms(r[6]) for r in rows]
    _check_close_time(open_ms, close_ms, close_time_policy, stats)
    for r in rows:
        for idx in (5, 7, 9, 10):  # volume / quote_volume / taker_buy_* は 0 のはず
            if float(r[idx]) != 0.0:
                raise BinanceNormalizationError(
                    f"markPriceKlines の列 {idx} が 0 でない: {r[idx]!r}(約定量ではないはず)"
                )
    # `count` は5分間の mark サンプル数(通常 300 = 毎秒1本)。**0 は停止したバー**
    # であり、mark が更新されないまま前値が横引きされている。値は捏造ではないので
    # 落とさないが、**清算トリガーの入力としては品質が違う**ので数えて記録する。
    if stats is not None:
        stale = sum(1 for r in rows if int(r[8]) == 0)
        stats["mark_stale_bars"] = stats.get("mark_stale_bars", 0) + stale
    df = pl.DataFrame(
        {
            "ts": open_ms,
            "mark_open": [float(r[1]) for r in rows],
            "mark_high": [float(r[2]) for r in rows],
            "mark_low": [float(r[3]) for r in rows],
            "mark_close": [float(r[4]) for r in rows],
            "mark_samples": [int(r[8]) for r in rows],
        }
    ).with_columns(pl.col("ts").cast(_TS))
    return _provenance(df, symbol, market_type)


def normalize_index_price(
    rows: list[list[str]],
    symbol: str = DEFAULT_SYMBOL,
    market_type: str = MARKET_TYPE,
    *,
    close_time_policy: str = "exact",
    stats: dict | None = None,
) -> pl.DataFrame:
    """indexPriceKlines 行 → **index 価格**の OHLC(F5 = protocol §4.1 の `IDX`)。

    **mark 価格でも約定価格でもない。** index は複数の現物取引所から合成される
    参照価格であり、`mark_price_5m`(清算トリガー)とも `klines_5m`(perp の約定)
    とも別の系列である。列名を `index_open/high/low/close` にするのは、
    **3者を取り違えられないようにするため**である。

    実測(実装前に 2020-01 / 2025-12 を probe し、その後 72 か月全件で確認した):

    - 12 列の kline 形式。**header の有無はファイルごとに違う**(あり 45 / なし 27)。
      2022-01〜2022-06 は `なし → あり → なし → あり → なし → あり` と交互に現れるので、
      **月から header の有無を推測してはならない**。`read_zip_rows` が1ファイルずつ
      先頭セルで判定するので、この非単調性の影響は受けない
    - `close_time == open_time + 5m − 1ms` が全行で成立(`close_time_policy="exact"`)
    - `volume` / `quote_volume` / `taker_buy_*` / `ignore` は**全行 0**
      (index は板の約定ではない)。0 でなければ**送出して止まる**
    - `count` は5分間の index サンプル数(通常 300 = 毎秒1本)。
      `index_samples` として保持する
    """
    if not rows:
        return pl.DataFrame()
    open_ms = [_epoch_ms(r[0]) for r in rows]
    close_ms = [_epoch_ms(r[6]) for r in rows]
    _check_close_time(open_ms, close_ms, close_time_policy, stats)
    # **グリッドを明示的に検査する。** `_check_close_time` の `"exact"` 経路は
    # `close_time` しか見ないので、`open_time` が5分グリッドから外れても素通りする
    # (グリッド検査は `"last_trade_time"` 経路にしか無い)。実測では全行がグリッド上
    # だったが、**「実測でそうだった」を不変条件の代わりにしない**。
    # ここは IDX 固有の検査であり、Phase 7 系列の挙動は1文字も変えていない。
    for o in open_ms:
        if o % BAR_MS != 0:
            raise BinanceNormalizationError(f"open_time が5分グリッド上にない: {o}")
    # `ignore`(列 11)も含めて検査する。docstring と文書が「全行 0」と書いている
    # 列は、**書いたとおりに全部検査する**(宣言より狭い実装にしない)。
    for r in rows:
        for idx in (5, 7, 9, 10, 11):  # volume / quote_volume / taker_buy_* / ignore
            if float(r[idx]) != 0.0:
                raise BinanceNormalizationError(
                    f"indexPriceKlines の列 {idx} が 0 でない: {r[idx]!r}(約定量ではないはず)"
                )
    # **価格の正値性を検査する。** 実測 628,115 行では非正値 0 件だったが、
    # 同じ関数が stale bar と grid について「実測で 0 件だったことに依存した規則に
    # しない」と書いている以上、価格にだけ例外を作らない。非正の index 価格は
    # 下流で log を取れば汚染源になる(J7 の log(0) と同型の失敗)。
    for r in rows:
        for idx in (1, 2, 3, 4):  # open / high / low / close
            if float(r[idx]) <= 0.0:
                raise BinanceNormalizationError(
                    f"indexPriceKlines の価格が正でない: 列 {idx} = {r[idx]!r}"
                )
    # `count == 0` は index が更新されないまま前値が横引きされたバー。
    # 実測の2か月では 0 件だったが、**0 件に依存した規則にしない**。
    # 値は捏造ではないので落とさず、品質が違うので数えて記録する。
    if stats is not None:
        stale = sum(1 for r in rows if int(r[8]) == 0)
        stats["index_stale_bars"] = stats.get("index_stale_bars", 0) + stale
    df = pl.DataFrame(
        {
            "ts": open_ms,
            "index_open": [float(r[1]) for r in rows],
            "index_high": [float(r[2]) for r in rows],
            "index_low": [float(r[3]) for r in rows],
            "index_close": [float(r[4]) for r in rows],
            "index_samples": [int(r[8]) for r in rows],
        }
    ).with_columns(pl.col("ts").cast(_TS))
    return _provenance(df, symbol, market_type)


def normalize_premium_index(
    rows: list[list[str]],
    symbol: str = DEFAULT_SYMBOL,
    market_type: str = MARKET_TYPE,
    *,
    close_time_policy: str = "exact",
    stats: dict | None = None,
) -> pl.DataFrame:
    """premiumIndexKlines 行 → premium の OHLC。volume 系は常に0なので捨てる。"""
    if not rows:
        return pl.DataFrame()
    df = pl.DataFrame(
        {
            "ts": [_epoch_ms(r[0]) for r in rows],
            "premium_open": [float(r[1]) for r in rows],
            "premium_high": [float(r[2]) for r in rows],
            "premium_low": [float(r[3]) for r in rows],
            "premium_close": [float(r[4]) for r in rows],
            "premium_samples": [int(r[8]) for r in rows],
        }
    ).with_columns(pl.col("ts").cast(_TS))
    return _provenance(df, symbol, market_type)


def normalize_funding_rate(
    rows: list[list[str]],
    symbol: str = DEFAULT_SYMBOL,
    market_type: str = MARKET_TYPE,
    *,
    close_time_policy: str = "not_applicable",
    stats: dict | None = None,
) -> pl.DataFrame:
    """fundingRate 行 → funding **決済イベント**(F4)。

    実測した dump の schema(2020-01 / 2025-12 の2か月を probe して確認した。
    推測していない):

        calc_time,funding_interval_hours,last_funding_rate

    - **`calc_time` は決済時刻**(epoch ms)。protocol X4 のとおり公式 REST の
      `fundingTime` と同じ量である。**バー開始時刻ではない**ので、
      `[ts, ts+5m)` の区間解釈をしてはならない。
    - `funding_interval_hours` は **dump 自身が宣言する**間隔。実測では 8 だが
      **8 をハードコードしない**(cap/floor 到達時に恒久的に1時間へ切り替わる
      規則がある。X5)。宣言値は `funding_interval_hours_declared` として保持し、
      **実際に使う間隔は正規化の後段で直前の決済との ts 差から導出する**
      (`add_funding_intervals`。未来行から逆算しない)。
    - `last_funding_rate` は**その決済で確定したレート**。
    - **markPrice 列は存在しない**(実測。header は上の3列のみ)。したがって
      §8.1 の `MarkPrice(s)` はこの dump からは供給できない。
      **null 列を作って埋めることはしない**(存在しないものを列にしない)。
    """
    if not rows:
        return pl.DataFrame()
    df = pl.DataFrame(
        {
            "ts": [_epoch_ms(r[0]) for r in rows],
            "funding_rate": [float(r[2]) for r in rows],
            # dump の宣言値。導出値と取り違えないよう列名を分ける。
            "funding_interval_hours_declared": [int(r[1]) for r in rows],
        }
    ).with_columns(pl.col("ts").cast(_TS))
    return _provenance(df, symbol, market_type)


#: 決済時刻のサブ秒ジッタの許容(**間隔の切り替わりを隠さない幅**)。
#: 実測(全 6,576 決済)では `calc_time` は必ず正時 + 0〜47ms に載っている。1分あれば
#: ジッタは吸収でき、1h/4h/8h の切り替え(時間オーダ)は必ず検出される。
FUNDING_INTERVAL_JITTER_TOLERANCE_MS = 60_000


def add_funding_intervals(df: pl.DataFrame, stats: dict | None = None) -> pl.DataFrame:
    """`funding_interval_hours` を **直前の決済との ts 差**から導出する。

    - **未来行から逆算しない。** 行 t の間隔は `ts[t] - ts[t-1]` だけで決まる。
    - **最初のイベントは null**(直前の決済が観測範囲に無いので不明。捏造しない)。
    - **8時間に固定しない。** 1h / 4h / 8h いずれが現れてもそのまま保持する。
    - **非正な間隔と、dump の宣言値と食い違う間隔は品質異常として数える。**
      数えるだけで落とさない・丸めない(値は実データである)。

    系列全体(全 zip を結合し重複排除して ts 昇順にしたもの)に対して1回だけ
    適用する。月ファイル単位で適用すると各月の先頭が null になってしまう。
    """
    if df.is_empty():
        return df
    df = df.sort("ts").with_columns(
        pl.col("ts").diff().dt.total_milliseconds().alias("funding_interval_ms")
    )
    # **`/ HOUR_MS` と書いてはいけない。** polars は定数除算を逆数の乗算へ最適化するため、
    # ちょうど 8h(28,800,000ms)が 7.999999999999999 になる。整数部と剰余に分けると
    # Python の除算と 1bit まで一致し、8h / 4h / 1h がそのままの値で出る
    # (丸めているのではない。丸めなくても厳密になる書き方を選んでいる)。
    df = df.with_columns(
        (
            (pl.col("funding_interval_ms") // HOUR_MS).cast(pl.Float64)
            + (pl.col("funding_interval_ms") % HOUR_MS).cast(pl.Float64) / HOUR_MS
        ).alias("funding_interval_hours")
    )
    if stats is not None:
        observed = df.filter(pl.col("funding_interval_ms").is_not_null())
        deviation = (
            pl.col("funding_interval_ms")
            - pl.col("funding_interval_hours_declared") * HOUR_MS
        ).abs()
        stats["funding_interval_rows_derived"] = observed.height
        stats["funding_interval_first_event_null"] = int(df.height - observed.height)
        stats["funding_interval_non_positive_rows"] = int(
            observed.filter(pl.col("funding_interval_ms") <= 0).height
        )
        stats["funding_interval_disagrees_with_declared_rows"] = int(
            observed.filter(deviation > FUNDING_INTERVAL_JITTER_TOLERANCE_MS).height
        )
        # 許容幅で「隠れた」ずれの最大値も必ず出す(閾値で丸めたことを見えなくしない)
        worst = observed.select(deviation.max()).item() if observed.height else None
        stats["funding_interval_max_deviation_ms"] = int(worst or 0)
    ordered = [
        "ts",
        "funding_rate",
        "funding_interval_hours",
        "funding_interval_ms",
        "funding_interval_hours_declared",
        "symbol",
        "source",
        "market_type",
    ]
    return df.select([c for c in ordered if c in df.columns])


def _optional_float(cell: str) -> float | None:
    """空セルは null。実 dump の ratio 列には空欄が存在する(値の捏造をしない)。"""
    text = cell.strip()
    return float(text) if text else None


def normalize_metrics(
    rows: list[list[str]],
    symbol: str = DEFAULT_SYMBOL,
    market_type: str = MARKET_TYPE,
    *,
    close_time_policy: str = "exact",
    stats: dict | None = None,
) -> pl.DataFrame:
    """metrics 行 → derivatives state。`create_time` は UTC の naive 文字列。

    OI 2列は必須(空なら例外)。long/short 系 ratio は空欄がありうるので null にする。
    """
    if not rows:
        return pl.DataFrame()
    for r in rows:
        if r[1] != symbol:
            raise BinanceNormalizationError(f"symbol 不一致: {r[1]!r} != {symbol!r}")
    df = pl.DataFrame(
        {
            "ts": [r[0] for r in rows],
            "open_interest": [float(r[2]) for r in rows],
            "open_interest_value": [float(r[3]) for r in rows],
            "top_trader_account_ls_ratio": [_optional_float(r[4]) for r in rows],
            "top_trader_position_ls_ratio": [_optional_float(r[5]) for r in rows],
            "global_account_ls_ratio": [_optional_float(r[6]) for r in rows],
            "taker_ls_vol_ratio": [_optional_float(r[7]) for r in rows],
        },
        schema_overrides={
            "top_trader_account_ls_ratio": pl.Float64,
            "top_trader_position_ls_ratio": pl.Float64,
            "global_account_ls_ratio": pl.Float64,
            "taker_ls_vol_ratio": pl.Float64,
        },
    ).with_columns(
        pl.col("ts")
        .str.to_datetime("%Y-%m-%d %H:%M:%S", time_unit="ms")
        .dt.replace_time_zone("UTC")
    )
    return _provenance(df, symbol, market_type)


NORMALIZERS = {
    "klines_5m": (normalize_klines, _KLINE_COLUMNS, config.binance_klines_parquet),
    "premium_index_5m": (normalize_premium_index, _KLINE_COLUMNS, config.binance_premium_index_parquet),
    "metrics_5m": (normalize_metrics, _METRIC_COLUMNS, config.binance_metrics_parquet),
    # --- Phase 8 の入力(F1 / F2)---
    "mark_price_5m": (normalize_mark_price, _KLINE_COLUMNS, config.binance_mark_price_parquet),
    "spot_klines_5m": (normalize_klines, _KLINE_COLUMNS, config.binance_spot_klines_parquet),
    # --- Phase 8 の入力(F5 = protocol §4.1 の IDX)---
    "index_price_5m": (normalize_index_price, _KLINE_COLUMNS, config.binance_index_price_parquet),
    # --- Phase 8 の入力(F4)。**イベント系列**であってバーではない ---
    "funding_rate": (normalize_funding_rate, _FUNDING_COLUMNS, config.binance_funding_rate_parquet),
}


#: **target ごとの封印 cutoff**(Y13)。グローバルな可変値にしない。
#:
#: Phase 7 の3系列の既定は `FINAL_OOS_START` のままで**1文字も変えない**
#: (変えると Phase 7 の再現性が壊れる)。**H5 が承認されるまで Phase 8 の
#: 系列も同じ値を使う**ので、現時点で全 dataset の値は一致している。
#: 「一致していること」と「同じ1個のグローバルを共有していること」は違う。
#: 将来 layer 3 を有効化するときは、**この表の Phase 8 側だけ**を動かす。
SEAL_CUTOFFS: dict[str, datetime] = {dataset: FINAL_OOS_START for dataset in NORMALIZERS}


def seal_cutoff_for(dataset: str) -> datetime:
    """dataset の封印 cutoff。未登録の dataset は既定(Phase 7 と同じ)。"""
    return SEAL_CUTOFFS.get(dataset, FINAL_OOS_START)


def apply_screening_cutoff(df: pl.DataFrame, cutoff: datetime = FINAL_OOS_START) -> tuple[pl.DataFrame, int]:
    """封印期間(既定 2026-01-01 以降)の行を落とす。戻り値は (残り, 落とした行数)。"""
    if df.is_empty():
        return df, 0
    kept = df.filter(pl.col("ts") < cutoff)
    return kept, df.height - kept.height


def scan_dataset(
    dataset: str,
    symbol: str = DEFAULT_SYMBOL,
    source_dir: Path | None = None,
    cutoff: datetime | None = None,
) -> tuple[pl.DataFrame, dict]:
    """raw zip 群を読み、結合フレームと raw 段階の会計を返す(書き込みはしない)。

    会計は品質レポートの入力でもある(重複・封印落ちは normalized からは見えない)。
    """
    normalizer, columns, _ = NORMALIZERS[dataset]
    spec = DATASETS[dataset]
    market_type = spec.market_type
    cutoff = seal_cutoff_for(dataset) if cutoff is None else cutoff
    time_stats: dict[str, int] = {}
    fmt = "%Y-%m" if DATASETS[dataset].cadence == "monthly" else "%Y-%m-%d"
    source_dir = source_dir or raw_dir(dataset, symbol)
    files = sorted(source_dir.glob("*.zip"))
    frames: list[pl.DataFrame] = []
    raw_rows = 0
    sealed_dropped = 0
    for path in files:
        rows = read_zip_rows(path, columns)
        raw_rows += len(rows)
        df = normalizer(
            rows, symbol, market_type,
            close_time_policy=spec.close_time_policy, stats=time_stats,
        )
        df, dropped = apply_screening_cutoff(df, cutoff)
        sealed_dropped += dropped
        if not df.is_empty():
            # dump が「どの期間のファイルか」を持ち回る(境界行の所有者判定に使う)
            frames.append(df.with_columns(pl.lit(period_of(path)).alias("_period")))
    combined = pl.concat(frames, how="vertical") if frames else pl.DataFrame()
    accounting = {
        "dataset": dataset,
        "symbol": symbol,
        "market_type": market_type,
        "close_time_policy": spec.close_time_policy,
        "series_kind": spec.series_kind,
        "files": len(files),
        "raw_rows": raw_rows,
        "sealed_rows_dropped": sealed_dropped,
        "cutoff": cutoff.isoformat(),
        **time_stats,
    }
    if combined.is_empty():
        return combined, accounting

    # 完全重複(同一 ts・同一値)は raw に実在する(2020年の metrics は1日576行)。
    # 同一 ts で値が食い違う重複も少数だが実在する(日付境界 00:00 が前日ファイルにも
    # 入っている)。**その ts の日付を持つファイルを所有者として採用する**という
    # 決定的な規則で解決し、件数と解決可否を必ず報告する。
    unique_full_rows = combined.unique().height
    unique_keys = combined.unique(subset=KEY_COLS).height
    conflicts = unique_full_rows - unique_keys
    combined = combined.with_columns(
        (pl.col("ts").dt.strftime(fmt) == pl.col("_period")).alias("_owns")
    )
    conflicting_ts = (
        combined.unique()
        .group_by(KEY_COLS)
        .agg(pl.len().alias("rows"), pl.col("_owns").sum().alias("owning_rows"))
        .filter(pl.col("rows") > 1)
    )
    resolved = combined.sort(["ts", "_owns"], descending=[False, True]).unique(
        subset=KEY_COLS, keep="first", maintain_order=True
    )
    accounting |= {
        "duplicate_rows_dropped": combined.height - unique_keys,
        "conflicting_duplicates": conflicts,
        "conflicts_resolved_by_owning_file": int(
            conflicting_ts.filter(pl.col("owning_rows") == 1).height
        ),
        "unresolved_conflicts": int(conflicting_ts.filter(pl.col("owning_rows") != 1).height),
    }
    resolved = resolved.drop("_period", "_owns")
    if spec.series_kind == "event":
        # **結合・重複排除の後に1回だけ**導出する。月ファイル単位で導出すると
        # 各月の先頭行の間隔が(直前の月に決済があるのに)null になってしまう。
        resolved = add_funding_intervals(resolved, accounting)
    return resolved, accounting


def period_of(path: Path) -> str:
    """dump ファイル名から period("YYYY-MM" / "YYYY-MM-DD")を取り出す。"""
    return path.stem.split("-", 2)[2]


def _write_parquet_atomic(path: Path, df: pl.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".parquet.tmp")
    df.write_parquet(tmp)
    tmp.replace(path)


def normalize_dataset(
    dataset: str,
    symbol: str = DEFAULT_SYMBOL,
    source_dir: Path | None = None,
    out_path: Path | None = None,
    cutoff: datetime | None = None,
) -> dict:
    out_path = out_path or NORMALIZERS[dataset][2](symbol)
    combined, result = scan_dataset(dataset, symbol, source_dir, cutoff)
    result["path"] = out_path.as_posix()
    if combined.is_empty():
        result["rows"] = 0
        return result
    if DATASETS[dataset].series_kind == "event":
        # **イベント系列は全再生成する。**
        # `funding_interval_hours` は直前の決済との差から**導出**した列なので、
        # 追記マージ(既存行を優先して残す)を使うと、後から過去月を足したときに
        # 古い null / 古い間隔が居座る。全 zip から決定的に作り直せば、
        # 「同じ raw なら同じ parquet」が構成上保証される(冪等)。
        before = pl.read_parquet(out_path).height if out_path.exists() else 0
        _write_parquet_atomic(out_path, combined.sort(KEY_COLS))
        result["rows_added"] = combined.height - before
        result["rebuilt"] = True
    else:
        result["rows_added"] = store.merge_parquet(out_path, combined, KEY_COLS)
    result["rows"] = pl.read_parquet(out_path).height
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Binance Vision dump → normalized parquet")
    parser.add_argument("--symbol", default=DEFAULT_SYMBOL)
    parser.add_argument("--datasets", nargs="*", default=list(NORMALIZERS), choices=list(NORMALIZERS))
    args = parser.parse_args()
    for dataset in args.datasets:
        result = normalize_dataset(dataset, args.symbol)
        print(
            f"{dataset}: files={result['files']} raw_rows={result['raw_rows']} "
            f"rows={result['rows']} sealed_dropped={result['sealed_rows_dropped']} "
            f"dup_dropped={result.get('duplicate_rows_dropped', 0)} "
            f"conflicting={result.get('conflicting_duplicates', 0)} -> {result['path']}"
        )


if __name__ == "__main__":
    main()

# A3 ソースレビュー — `optimal-basis-trade-control`(必須。two_leg.py 実装より優先)

- 実施日: 2026-08-17
- 対象: `https://github.com/0xBoringWozniak/optimal-basis-trade-control`
  (**A3** = arXiv `2605.05089` *Dynamic Collateral Control for Permissionless
  Spot Perpetual Basis Trading* の著者公開コード)
- 取得方法: `git clone --depth 1`(2026-08-17)。91 ファイル / data 154 MB / outputs 8.5 MB
- 指示された重点: **notebook 02、notebook 04、`utils.py`**
- **決定ログの制約: A3 の diagnostic threshold を protocol parameter として採用しない。**

---

## 0. 一行で

**採用するのは「取引所データの意味論」と「二脚の会計構造」だけである。**
**閾値・較正値・バッファ格子は一切採用しない。**

---

## 1. リポジトリ構成(実測)

```text
notebooks/
  01_optimal_control.ipynb    静的最適制御 + Monte Carlo バンド
  02_backtests_and_funding_environment_diagnostics.ipynb   ← 重点
  03_basisos_historical_trades.ipynb                       BasisOS 実トレードのスプレッド分析
  04_execution_diagnostics.ipynb                           ← 重点
  utils.py                    REST fetcher / CSV loader / ヘルパ(384 行)
data/      trades / vault_costs / backtests(CSV)
outputs/   図表
```

`utils.py` の定数: `EXPERIMENT_TIME = 2026-03-30 UTC`、
`BINANCE_SPOT_BASE = api.binance.com`、`BINANCE_FUTURES_BASE = fapi.binance.com`、
`HYPERLIQUID_INFO_URL`、`REQUEST_TIMEOUT = 60`、`SLEEP_BETWEEN_CALLS = 1`。

---

## 2. **採用する**意味論(adopted semantics)

| # | A3 の実装 | ango が採用する理由 | 反映先 |
|---|---|---|---|
| **S1** | `fetch_binance_funding_history` は `/fapi/v1/fundingRate` を叩き、**`fundingTime` を決済時刻として扱い、同じ行の `markPrice` を対で保持**する(`df[["timestamp","fundingRate","markPrice"]]`) | ango が X4 で独立に確認した結論(`calc_time` ≡ `fundingTime` ≡ 決済時刻)と**完全に一致**。第三者の実装が同じ解釈をしていることは独立の裏づけになる | protocol §5.2 / §8.1 |
| **S2** | 決済時点の **`markPrice` を funding 額の計算に使う**(代理を使わない) | ango の H10 の推奨(markPrice を primary、`perp_close` は感応度)を裏づける | protocol §8.1 |
| **S3** | `fetch_binance_mark_price_klines` が **`/fapi/v1/markPriceKlines`** を使う | Y38(清算判定は mark price)の入力が実在することの裏づけ | protocol §4.1 / §11.3 |
| **S4** | ページングは `next_start = last_time + 1`(ms)、空バッチで停止、`next_start <= start_ms` なら停止 | **決定的で重複も欠落も出ない**イディオム。ango の取り込みでも同じ不変条件を使う | `binance_vision` / `binance_rest` |
| **S5** | 二脚の会計を **別ウォレットとして持つ**: `SPOT_amount` / `SPOT_cash` と `HEDGE_balance` / `hedge_notional` / `HEDGE_mark_price` / `HEDGE_funding_rate`、そして `net_balance` | **これは ango の M7(ヘッジは PnL 中立だが証拠金中立ではない)そのものである。** 現物ウォレットの含み益は先物ウォレットの証拠金を支えない、という構造を A3 は列の分離で表現している | protocol §9 M7 / §11.4 |
| **S6** | `leverage_t = hedge_notional / hedge_balance` を**時系列で持つ** | ango の §11.3 / §11.4(維持証拠金と追証)に必要な状態量。**スカラーではなく系列**である点が重要 | protocol §11.3 / §11.4 |
| **S7** | `funding_cashflow` と `cum_funding_cashflow` を**分離して保持** | ango の §8(funding 収支)と §17.2 の `funding_capture` に対応。累積を別列にすることで M3(USDT が積み上がる)を追える | protocol §8 / §20 |

---

## 3. **採用しない**もの(NOT adopted)

**決定ログの明示的制約に従い、A3 の diagnostic threshold は protocol parameter にしない。**

| # | A3 の値 | 不採用の理由 |
|---|---|---|
| **N1** | `REL_REBAL_THRESHOLD = 1e-4`(リバランス判定) | **diagnostic threshold**。A3 の可視化のための値であり、ango の事前登録パラメータではない。ango の追証閾値(`MARGIN_TOPUP_TRIGGER`)は**独立に凡庸な値を決め打ちする** |
| **N2** | `BUFFER_GRID_BPS = np.arange(0, 101, 1)` | **diagnostic grid**。ango がこれを採ると、事実上 101 通りの閾値探索を導入することになり §15 の family が壊れる |
| **N3** | `CALIBRATION = {ticker: {alpha_target, alpha_lower, alpha_upper}}` | **銘柄別の較正値**。ango は BTC 単独であり、かつ較正値を外部から持ち込むと「結果を見る前に凍結した」と言えなくなる |
| **N4** | `abs_spread_bps` の p50 / p90 / p95 という分位の選択 | **報告上の選択**であって仮説ではない。ango は §16.2 で自前の CI 手続きを凍結済み |
| **N5** | notebook 01 の最適制御・Monte Carlo バンド | ango の **Non-goals §2-3**(collateral の動的最適制御をしない)に抵触。A3 の主題であり Phase 8.1 の範囲外 |
| **N6** | `data/trades/`(BasisOS の実トレード tape)、`data/vault_costs/` | **私的な運用データ**。再現不能であり、ango の結論の根拠にできない |
| **N7** | `fetch_binance_max_leverage` のフォールバック辞書(`BTCUSDT: 125.0` 等) | **ハードコードされた取引所パラメータ**。ango は §11.3 のとおり **margin tier 表を実測して凍結**する。ハードコードは陳腐化して静かに間違う |
| **N8** | `funding_only_apy(total_funding_pnl, capital_base, days)` の年率化 | ango の primary は **trade 単位の `r_i`**(§17.1)。集計方法が違う。A3 の式を主指標に混ぜない |

---

## 4. 環境上の乖離(記録しておく)

| 項目 | A3 | ango |
|---|---|---|
| Binance ホスト | `api.binance.com` / `fapi.binance.com` | **これらは ango の実行環境から HTTP 451。** `www.binance.com/fapi/...` と `data.binance.vision` を使う(X6) |
| データ取得 | 実行時に REST を叩く | **一括ダンプを CHECKSUM 検証して不変保存**(`mce.binance_vision`)。再現性のため |
| 対象 | BTC / ETH / LINK / DOGE、Binance と Hyperliquid | **BTC 単独・Binance 単独**(§2-4 / §2-6) |

**A3 が `fapi.binance.com` を直接使えているのは、著者の実行環境がジオブロック外だからである。**
ango は同じ経路を使えないので、**同じ意味論を別の経路で得ている**ことを明記する。

---

## 5. two_leg.py への具体的な帰結

**実装前に確定した設計判断:**

1. **ウォレットを2つ持つ**(S5)。`spot_wallet`(BTC + cash)と `futures_wallet`(USDT 証拠金)。
   単一の `equity` スカラーにまとめない。**M7 が表現できなくなるため。**
2. **`leverage_t` を毎バー記録する**(S6)。清算判定と追証判定の入力。
3. **funding は決済時点の `markPrice` で評価し、`funding_cashflow` を独立列にする**(S2 / S7)。
4. **ページングの不変条件を踏襲する**(S4): 空バッチで停止、進捗しなければ停止。
5. **A3 の閾値は一切持ち込まない**(N1–N4)。ango の閾値は `phase8_prereg.py` に
   **独立に**凍結する。

---

## 6. このレビューが言っていないこと

1. **A3 のコードを実行していない。** 読んだだけである。
2. **A3 の結論を検証していない。** 本レビューの対象は意味論と会計構造だけである。
3. **A3 を再現対象にしていない。** A3 は設計参照であり、再現アンカーは A2 である(§3.4)。
4. **A3 のデータを ango へ取り込んでいない。**

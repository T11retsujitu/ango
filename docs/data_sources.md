# データソース調査 (2026-08-15)

BTC の OHLCV / 約定・出来高 / Funding Rate / Open Interest を取得できる公開 API の比較。

注意:
- 「疎通」列は本調査を実施したクラウド実行環境(おそらく米国リージョン)からの実測。**日本のIPからの結果は異なりうる**ため、ローカルPCで再確認すること。
- 「API から取得できること」と「取得データを再配布できること」は別問題。利用規約の判断がつかないものはすべて **要確認** とした。

## 比較表

| API | データ | Historical 遡及(実測含む) | REST/WS | API Key | Rate Limit | 日本からの利用 | 再配布・公開 |
|---|---|---|---|---|---|---|---|
| **OKX v5** (global) | OHLCV, 約定, Funding, OI, Order Book, Long/Short 比 | OHLCV: 上場以来(2020年以前の5分足を実測確認)。Funding: **約3ヶ月**(実測)。OI(5m): **約5日=1440点**(実測) | REST + WS | 公開データは不要 | history-candles 20req/2s 程度(公式Docs参照) | 口座開設は日本居住者不可(OKX Japan誘導)。公開APIへの日本IPアクセス可否は**要確認** | ToS上のデータ再利用規定は**要確認** |
| **Binance** (global REST) | OHLCV, 約定, Funding, OI, 清算, Long/Short 比 | OHLCV: 2017〜(spot)/2019-09〜(futures) | REST + WS | 公開データは不要 | weight制 (~6000/min) | 日本居住者はBinance Japanへ誘導。**本実行環境からは HTTP 451 でブロック(実測)**。日本IPからの可否は**要確認** | 要確認 |
| **Binance Vision** (data.binance.vision 一括ダンプ) | klines, trades, aggTrades, fundingRate(月次), metrics(OI・Long/Short比, 5分粒度) | ほぼ全履歴。**本実行環境から取得成功(実測)** | HTTPS 静的ファイル | 不要 | 実質なし | 静的配信のため技術的には広く到達可能だが、日本居住者の利用可否・規約は**要確認** | 要確認 |
| **Bybit v5** | OHLCV, 約定, Funding, OI, 清算 | OHLCV: 2020頃〜 | REST + WS | 公開データは不要 | IP毎 600req/5s 程度 | 日本からのアクセスは一般に可能とされるが、FSA警告履歴あり・**要確認**。**本実行環境からは 403 でブロック(実測)** | 要確認 |
| **Bitget v2** | OHLCV, 約定, Funding, OI | OHLCV: 2022-01の5分足を実測確認。Funding履歴の深さは**要確認**(遠いページは空) | REST + WS | 公開データは不要 | 20req/s 程度 | FSA警告履歴あり・**要確認**。本実行環境からは疎通OK(実測) | 要確認 |
| **Coinbase Exchange** | OHLCV, 約定(spotのみ。Funding/OIなし) | 2015〜(2020年の5分足を実測確認)。300本/req | REST + WS | 公開データは不要 | 10req/s 程度 | 公開データは利用可能とみられる(要確認) | 要確認 |
| **Kraken** | OHLCV, 約定(spot中心) | OHLC APIは**直近720本のみ**→ 5分足の長期履歴に不適 | REST + WS | 公開データは不要 | 中程度 | 日本から利用可 | 要確認 |
| **CoinGecko / CryptoCompare** 等アグリゲータ | OHLC(粗い粒度中心)。Funding/OIは限定的 | 無料枠では5分粒度の長期履歴が取れない場合が多い | REST | 無料でもKey要のものあり | 無料枠は厳しめ | 利用可 | プランにより明記あり(それでも要確認) |

## 実測サマリ(本環境から)

- 到達可: OKX, Bitget, Coinbase, Kraken, **data.binance.vision(一括ダンプのzip取得成功)**
- ブロック: Binance REST (451), Bybit (403) — いずれも実行環境の所在地(米国とみられる)によるジオブロック
- OKX `history-candles` は 2020-01 以前の 5 分足まで確認。`funding-rate-history` は約3ヶ月、`rubik/.../open-interest-history` (5m) は約5日分しか遡れない

## 推奨と理由

**第一候補: OKX v5 public REST**(本PoCで採用)

1. API Key 不要・無料
2. 5分足 OHLCV を上場以来まで遡れる(1req=100本、ページング明快)
3. Funding / OI も同一 API 系列で取得でき、レスポンス形式が素直で Python から扱いやすい
4. 本実行環境から動作確認済み

制約・注意:
- Funding(約3ヶ月)/ OI(約5日)の遡及が浅い → **定期実行で自前蓄積する**前提の設計にした
- 日本居住者の口座開設は不可。**公開マーケットデータ API への日本 IP アクセス可否とToS上の扱いは要確認**。ローカルPCで `curl https://www.okx.com/api/v5/market/candles?instId=BTC-USDT-SWAP&bar=5m&limit=1` を実行して確認すること

**代替候補:**
- 日本のローカルPCから Bybit に到達できるなら、Bybit v5 は同等機能を持つ(コードは `sources` 層の差し替えで対応する設計)
- 深い Funding / OI / Long-Short 履歴が必要になったら Binance Vision の一括ダンプが最有力(ただし日本居住者の利用規約上の扱いは要確認)

## 利用規約に関する一般的注意

- 各取引所の ToS は、マーケットデータの**個人的な分析利用**と、**加工データのWeb公開・生データ再配布**を区別している(または明記がない)ことが多い
- 生データの再配布は原則不可と想定しておくのが安全
- 集計・加工済み統計(例: 条件該当件数とリターン分布)の公開も、出典表記やレート・データの二次利用条項に触れる可能性があるため、公開前に対象取引所の Terms of Service / Market Data 条項を必ず確認する(**要確認**)
- 本PoC はローカル保存・個人研究の範囲にとどめる

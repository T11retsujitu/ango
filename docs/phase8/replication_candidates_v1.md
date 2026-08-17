# Phase 8.0 — 再現候補一覧と採点(v1)

- 作成日: 2026-08-17
- **機械可読な正**: [replication_candidates_v1.json](replication_candidates_v1.json)
  (本文書の数値は JSON から手で書き写したものではなく、`tests/test_phase8_docs.py` が
  両者の一致を機械的に強制する)
- 調査記録: [literature_review_2026-08-17](literature_review_2026-08-17.md)
- 選定判断: [phase8_selection_memo_v1](phase8_selection_memo_v1.md)
- **forward return / 損益 / バックテストは一切計算していない。Final OOS も開封していない。**

---

## 1. 採点軸(指定どおり)

| 評価軸 | 配点 |
|---|---:|
| 経済的機序の明確さ | 15 |
| データ入手可能性 | 15 |
| コスト・約定の現実性 | 15 |
| 独立 confirmation 確保 | 15 |
| 再実装可能性 | 10 |
| ango 資産の再利用性 | 10 |
| 個人研究者としての実行可能性 | 10 |
| エビデンス品質 | 10 |

**報告収益率の高さには配点していない。** 減点は各候補の `deductions` に列挙する。

---

## 2. 採点結果(降順)

| 順位 | ID | 候補 | family | 機序15 | データ15 | コスト15 | 独立conf15 | 再実装10 | 資産10 | 個人10 | 証拠10 | **合計** |
|---:|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 1 | P8-C1 | **BTC spot–perp funding carry / basis** | A | 14 | 15 | 12 | 9 | 8 | 7 | 8 | 8 | **81** |
| 2 | P8-C2 | hourly BTC ML + cost-aware execution filter | C | 5 | 15 | 10 | 3 | 7 | 8 | 9 | 7 | **64** |
| 3 | P8-C3 | crypto factor zoo(反復 alpha 選択) | D | 9 | 6 | 10 | 8 | 6 | 4 | 5 | 9 | **57** |
| 4 | P8-C4 | funding timing と DEX の no-arbitrage 境界 | B | 13 | 7 | 9 | 8 | 5 | 3 | 4 | 5 | **54** |
| 5 | P8-C5 | 制約付き LLM エージェント + point-in-time factor DSL | F | 4 | 6 | 9 | 5 | 6 | 9 | 7 | 6 | **52** |
| 6 | P8-C6 | L2 流動性状態 × order flow | E | 11 | 4 | 7 | 5 | 5 | 6 | 4 | 7 | **49** |
| 7 | P8-C7 | cross-venue funding spread(Hyperliquid vs CEX) | B | 12 | 8 | 7 | 4 | 6 | 4 | 5 | **1** | **47** |
| 8 | P8-C8 | ETF implied carry と分断された BTC 市場 | A | 12 | 3 | 8 | 7 | 4 | 2 | 3 | 7 | **46** |
| 9 | P8-C9 | 清算カスケードの early-warning | E | 10 | 2 | 5 | 6 | 4 | 4 | 3 | 7 | **41** |

**推薦上位5件 = P8-C1 / P8-C2 / P8-C3 / P8-C4 / P8-C5。**

---

## 3. 候補別の要点(必要データ・再現難易度・減点理由)

### P8-C1 — BTC spot–perp funding carry / basis 【81点・第1位】

> **⚠ v1.1 訂正済み(2026-08-17)。** 当初 89点としたのは、A1 を perpetual の論文だと
> 誤認していたためである。両論文の**全文を取得**して訂正した(JSON `corrections_applied` X1–X6)。
> **訂正後も第1位は変わらない**(2位 P8-C2 は 64点)。

- **再現アンカー(唯一)**: **A2** *Fundamentals of Perpetual Futures*
  (He / Manela / Ross / von Wachter, arXiv `2212.06888` v6 2024-08-21)**`VERIFIED-FULL`**
  - サンプル **2020-01-08 〜 2024-03-11**(1,495日・日次)、venue **Binance**
  - 検定している戦略は **random-maturity arbitrage**:
    futures–spot スプレッドが**コスト階層ごとの理論境界を超えたら建て、
    コスト無しの理論関係へ戻ったら解消する**。**保有期間は内生**であって固定 horizon ではない
  - 報告値: BTC perp で **Sharpe 1.8(リテール水準の高コスト下)/ 最大 3.5(高頻度 MM)**
  - 著者自身の留保:「funding rate arbitrage は、証拠金と取引コストを度外視しても
    無リスクではない。利益確定して手仕舞う予定満期が存在しないからである」
- **経済的文脈(再現対象ではない)**: **A1** *Crypto Carry*(Schmeling / Schrimpf / Todorov,
  Management Science 2026-05-06, DOI `10.1287/mnsc.2024.05069`)**`VERIFIED-FULL`**
  - **⚠ A1 は dated(固定満期)futures の論文であり、perpetual ではない。**
    本文に「Recent papers study so-called perpetual crypto futures **instead of** the standard
    fixed-term futures analyzed in our paper」と明記されている
  - サンプル **2019-03 〜 2024-07**(日次)、venue: Binance / OKEx / FTX / Huobi / BitMEX /
    Deribit / CME、データ提供 Skew・Coinmetrics(**いずれも ango は未保有**)
  - 2019-04〜2024-07 の取引所横断平均 carry は **約 7% p.a.**(spike 時は 40% 超)
  - **bis.org が現在配信している PDF は表紙が 2023-04 だが本文の版は "October 1, 2025"**
- **設計参照(再現対象ではない)**: A3 arXiv `2605.05089` — collateral 制御と rebalancing
- **必要データ**: BTC spot OHLCV / BTC perp OHLCV / funding rate(**決済時刻 + markPrice**)/
  index price / premium index。**全て 2026-08-17 に到達確認済み**。
  **A2 と同一 venue(Binance)で再現できる**のが大きい
- **再現難易度**: **中**。ML なし。ただし A2 の no-arbitrage 境界と閾値 entry/exit の実装、
  および **two-leg 執行モデルの新規実装**(ango の engine は単一銘柄前提)が要る
- **減点**: 唯一のアンカー A2 が **査読前 preprint**・追試なし / 著者コード非公開 /
  ango の cost モデルに margin・collateral 項が無い /
  **独立窓が約17ヶ月しかない**(A2 は 2024-03、A1 は 2024-07 まで使用済み)
- **不採用にしなかった理由**: 訂正後も、機序の明確さ・データ入手可能性・証拠品質の
  3軸で最上位を維持し、2位に 17点差をつけている

### P8-C2 — hourly BTC ML + cost-aware execution filter 【64点・第2位】

- **アンカー**: C1 arXiv `2606.00060`(Bysik / Ślepaczuk)**`VERIFIED-FULL`**
- **必要データ**: Binance BTCUSDT perp 1時間足のみ。**ango は既に 5m を保有(集約可能)**
- **再現難易度**: **中**。XGBoost / LSTM / iTransformer × 27 fold。コード非公開
- **減点(重い)**:
  - **独立 confirmation が無い** — 論文サンプル **2017-12 〜 2026-01** が
    ango の research 窓・validation 窓を完全に覆う
  - **著者自身が「buy-and-hold への統計的に有意な Sharpe 優越は無い」と明記**
  - 3,510 試行から最良構成を報告する構造(選択問題)
  - ango は近縁の機序(cost-aware abstention)を Phase 1A で**既に棄却済み**(J3)
- **評価**: 論文の内部規律(27 fold walk-forward・Holm-Bonferroni・block bootstrap・
  限界の明示)は**高く評価できる**。順位が2位に留まるのは論文の質ではなく
  **ango 側の独立窓が無いこと**が主因

### P8-C3 — crypto factor zoo 【57点・第3位】

- **アンカー**: D1 *Crypto factor zoo (.Zip)*(Mercik / Zaremba / Demir,
  **IRFA 113 (2026)**, DOI `10.1016/j.irfa.2026.105137`)`VERIFIED-META`
- **⚠ seed で指定された DOI `10.1016/j.irfa.2026.104962` は HTTP 404。上記が実在する DOI**
- **必要データ**: 複数銘柄ユニバース(**point-in-time メンバーシップ・上場廃止銘柄を含む**)、
  流動性指標、オンチェーン指標(new-address-to-price)。**ango は BTC 単独で全て未保有**
- **再現難易度**: **高**。手法は標準的だが、ユニバース構築が本体
- **減点**: survivorship bias(事後にユニバースを再構築する構造的リスク)/
  個人には重いデータ要件 / コード・データ非公開

### P8-C4 — funding timing と DEX の no-arbitrage 境界 【54点・第4位】

- **アンカー**: B2 SSRN `6805838`(Erez / Smirnov, 2026-05-20)**`PARTIAL`(本文未読)**
- **機序は全候補中2番目に明快**: funding が**建玉時点で観測可能か**(Drift)
  **事後にしか決まらないか**(dYdX v4)で no-arbitrage 境界の実証的内容が変わる。
  これは ango の [data_contract §3 availability](../data_contract.md) と**同型の問題**
- **必要データ**: Drift / dYdX v4 のオンチェーン履歴(**ango 未保有・取得経路なし**)
- **減点**: 本文未確認 / 追試なしの preprint / 個人には重いオンチェーンデータ
- **note**: 機序の概念(decision-time observability)は**venue を問わず再利用可能**であり、
  P8-C1 の設計へ**そのまま輸入する**(→ 選定メモ §5)

### P8-C5 — 制約付き LLM エージェント 【52点・第5位】

- **アンカー**: F1 arXiv `2604.26747`(Huang / Fan / Hu / Ye, 2026-04-29)`VERIFIED-META`
- **重要な位置づけ**: F1 のアーキテクチャ(frozen DSL + AST + 決定論的エンジン +
  append-only トレース + 選抜ゲート + コスト)は **ango が Phase 2/3 で既に構築済みのものと
  ほぼ同型**。したがって F1 は「実装すべき対象」ではなく
  **ango の既存設計の外部的妥当性確認**として読むのが正しい
- **必要データ**: cross-sectional ユニバース(P8-C3 と同じ問題)
- **減点**: 機序ではなく方法論 / OOS 窓 2024–2026 が ango の validation と封印域に跨る /
  コード非公開

### P8-C6 — L2 流動性状態 × order flow 【49点・第6位】

- **アンカー**: E2 arXiv `2607.09230`(Jeon, 2026-07-10)`VERIFIED-META`
- **ango にとっての意味**: ROADMAP の Branch C が事前に書いていた
  「OFI 単体ではなく **OFI × liquidity state** で検証する」という設計を、
  独立に、より深い板データで裏づけている。**同時に「BTC では効果が限定的」という
  追加の悲観材料でもある**(ETH の方が一貫した改善)
- **必要データ**: tick 粒度 top-20 L2。bookDepth(0.55 MB/日)は列仕様未確認(backlog I5)、
  bookTicker は 199 MB/日(backlog I8)
- **減点**: 独立窓なし / tick L2 の多年スケールは個人に重い / コード非公開

### P8-C7 — cross-venue funding spread 【47点・第7位】

- **アンカー**: B1 SSRN `6993978`(seed S3)**`UNVERIFIED`**
- **⚠ 一次資料に到達できず、タイトル検索でも当該 SSRN ID の論文を発見できなかった。**
  検索スニペットには一致する内容(Hyperliquid の carry が Binance/Bybit を年率約7%上回る等)が
  現れたが、**それをこの SSRN ID に帰属させる根拠を得られなかった**。
  **これらの数値を ango の設計根拠にしない**
- **データ実測(2026-08-17)**: Hyperliquid `fundingHistory` は到達可(**1時間粒度**、
  BTC 履歴は 2023-05〜2023-08 のどこかで開始)。Binance Vision funding は到達可(**8時間**)。
  **Bybit の funding は本環境から取得経路が無い**(v5 REST 403 / public dump に funding 無し)
- **減点(最大)**: **エビデンス品質 1/10** — 一次資料の存在を確認できなかった。
  加えて venue 間の funding 間隔差、送金の即時性・無料性、二重の清算面
- **機序自体は12/15と高い。** H1(人間による SSRN 確認)が解決すれば順位は大きく上がりうる

### P8-C8 — ETF implied carry 【46点・第8位】

- **アンカー**: A4 arXiv `2605.29309`(Mallory, 2026-05-28)`VERIFIED-META`
- **必要データ**: IBIT オプション / CME 先物 / BlackRock 日次保有 / BRRNY。**ango は全て未保有**
- **減点**: 個人には取得困難 / 386 観測という少数サンプル / 追試なし preprint
- **note**: 機序(分断された担保・証拠金システムが裁定を制限する)は
  **A1 と同じ limits-to-arbitrage 系**であり、P8-C1 の解釈の補強材料になる

### P8-C9 — 清算カスケードの early-warning 【41点・第9位】

- **アンカー**: E1 arXiv `2607.27070`(Garcia Seuma, 2026-07-29)`VERIFIED-META`
- **必要データ**: 清算データ。**ango では blocked**(Binance Vision `liquidationSnapshot`
  は 2026-08-16 実測で 404、backlog I7)
- **減点**: データ取得不可 / 7事象という少数サンプル / 執行設計が提示されていない

---

## 4. 採点から除外した文献(理由つき)

| 文献 | 除外理由 |
|---|---|
| E3 *The Quarter-Hour Effect*(arXiv `2607.09426`, Kim & Hansen) | **ango が Phase 1B で既に独立検証済み**(方向性なし・活動構造あり)。再現は完了しており保留中ではない |
| F2 *The Alpha Illusion*(arXiv `2605.16895`)/ F3 *Beyond Agent Architecture*(arXiv `2606.08285`) | **取引仮説ではなく報告規準**。再現対象ではなく、**Phase 8 の報告規律として採用**することを推奨(H8) |
| C2 *Bitcoin Price Prediction: Peer-Reviewed Evidence...*(arXiv `2606.00071`) | サーベイ。再現対象ではないが方法論規準の参照として有用 |
| A5 Ackerer *Perpetual Futures Pricing*(`10.1111/mafi.70018`)/ B3 MDPI IJFS 14(5)103 / B4 MDPI Mathematics 14(2)346 / B5 ScienceDirect `S2096720925000818` | 出版社サイトが本環境から **403**。**内容を確認できていないため採点しない**(存在は記録する) |

---

## 5. 暫定 prior との差分(反証可能な prior として扱った結果)

| | 指示された暫定順位 | 本調査の結果 | 差分の理由 |
|---|---|---|---|
| 1 | Crypto carry / basis | **Crypto carry / basis** | **一致**。機序・データ・証拠の3軸で最上位を確認 |
| 2 | 1時間足 cost-aware execution | **1時間足 cost-aware execution** | **一致**。ただし独立 confirmation 窓が無いという重大な制約が判明 |
| 3 | Cross-venue funding spread | **crypto factor**(prior 4位) | cross-venue の seed 論文が `UNVERIFIED` で証拠品質 1/10 |
| 4 | 複数銘柄 crypto factor | **funding timing / no-arb 境界**(prior に無し) | 機序の明快さで factor を上回った |
| 5 | microstructure / 超短期 price discovery | **制約付き LLM エージェント**(prior に無し) | microstructure は6位。E2 自身が「BTC では効果が限定的」と報告 |

**prior は概ね支持された。** 主要な変更は cross-venue の降格(3位 → 7位)であり、
その理由は**研究テーマの価値ではなく、一次資料の存在を確認できなかったこと**である。
H1 が解決すれば再評価する(→ 選定メモ §10)。

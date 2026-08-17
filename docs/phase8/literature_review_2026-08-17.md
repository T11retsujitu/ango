# Phase 8.0 — 文献レビュー(調査基準日 2026-08-17)

- 作成日: 2026-08-17
- 目的: **論文ランキングではない。** 限られた実験回数・計算資源・データ取得能力の中で、
  ango が次にどの研究を再現すべきかを決めるための一次資料調査。
- 前提: [Phase 7 Tier 0 closeout](../findings/2026-08-17-phase7-tier0-closeout-v1.md)
- 出力先: [replication_candidates_v1](replication_candidates_v1.md) /
  [phase8_selection_memo_v1](phase8_selection_memo_v1.md)
- **本文書は forward return / 損益 / バックテストを一切計算していない。Final OOS も開封していない。**

---

## 0-A. v1.1 訂正記録(2026-08-17。**追記型・元の記述を消さない**)

第1位候補の**両アンカー論文の全文を取得**した結果、本文書の初版に**重大な誤り**が見つかった。
**元の記述は §3 に残し、ここで訂正する。**

| id | 初版の記述 | 訂正 | 根拠 |
|---|---|---|---|
| **X1** | A1 *Crypto Carry* を perpetual carry のアンカーとして扱った | **A1 は dated(固定満期)futures の論文であり、perpetual ではない。** 本文に「Recent papers study so-called perpetual crypto futures **instead of** the standard fixed-term futures analyzed in our paper」と明記。データセットに perp は入っていない | [BIS WP 1087 PDF 本文抽出](https://www.bis.org/publ/work1087.pdf) |
| **X2** | 「BIS WP が 2023-04 なのでサンプル終端は 2023年初以前」と**推定**した | **誤り。** bis.org が配信している PDF は表紙が 2023-04 だが本文は **"This version: October 1, 2025"**。実際のサンプルは **2019-03 〜 2024-07(日次)** | 同上 |
| **X3** | A2 のサンプル・戦略を「不明」とした | **2020-01-08 〜 2024-03-11(1,495日・日次)、venue は Binance。** 戦略は **random-maturity arbitrage**(スプレッドがコスト階層別の理論境界を超えたら建て、コスト無しの理論関係へ戻ったら解消)で、**固定 horizon の carry trade ではない**。BTC で **Sharpe 1.8(リテール高コスト)/ 最大 3.5(高頻度 MM)** | [arXiv 2212.06888v6 PDF 本文抽出](https://arxiv.org/pdf/2212.06888v6) |
| **X4** | H4(`calc_time` の semantics)を未確定の fatal blocker とした | **解決。** Vision の `calc_time` は公式 REST の `fundingTime` と**完全一致**(2024-01 の全行で照合)。すなわち **funding 決済時刻**である | Vision CSV と `https://www.binance.com/fapi/v1/fundingRate` の直接照合 |
| **X5** | funding 間隔を 8時間固定として扱った | **2025-05-02 以降、cap/floor に達した契約は恒久的に1時間 funding へ切り替わる。** 実装は `funding_interval_hours` を**行ごとに読む**必要がある | Binance 公式アナウンス(本タスクの検証エージェント報告。**ango 自身は未確認**) |
| **X6** | Binance REST は 451 で到達不可とした | **`https://www.binance.com/fapi/v1/...` は 200 で到達可**(`api.binance.com` は 451)。**funding 決済時点の `markPrice` が取れる** | 直接実測 |

### 訂正が採点に与えた影響

**P8-C1 は 89点 → 81点。順位は第1位のまま**(2位 P8-C2 は 64点)。
内訳は [replication_candidates_v1.json](replication_candidates_v1.json) の
`corrections_applied` と `score_rationale` を参照。

### この訂正自体の教訓(Phase 8 の規律へ反映する)

1. **出版社の書誌ページと abstract だけでは、論文が「何の商品を扱っているか」すら分からない。**
   A1 は abstract で "difference between futures and spot prices" としか言っておらず、
   dated futures 限定であることは本文でしか分からなかった。
2. **working paper の「日付」は版の日付ではない。** BIS の表紙 2023-04 と
   本文の "October 1, 2025" が食い違っていた。**cover date からサンプル終端を推定してはいけない。**
3. → **Phase 8 では、再現アンカーに採用する論文は `VERIFIED-FULL`(本文取得)を必須とする。**
   `VERIFIED-META` はアンカーの資格として不十分である。
4. **1回の否定的な検索応答は「存在しない」の証拠にならない**(X7)。
   本レビューは「Google Scholar は SSRN 6993978 を収録していない」と報告したが、
   反証パスが同一 URL を再取得して**収録されている**ことを示した。
   最初の応答は不安定なものだった。
5. **文献全体に対する全称否定を書かない。** 「誰もやっていない」は検証不能である。
   **「取得できた範囲では」に限定する**(X7)。

### 0-B. 本レビュー自身に対する反証パス(2026-08-17)

本レビューの調査結果に対して、**独立の敵対的事実確認**を走らせた。
**10件の主張が覆った。** 主なもの:

| 覆った主張 | 訂正 |
|---|---|
| 「Google Scholar は SSRN 6993978 を収録していない」 | **収録している**(`T Lau - 2026 - papers.ssrn.com`)。初回の否定は不安定な応答 |
| 「taker コストを差し引いた検証は誰もしていない」 | **検証不能な全称否定**。「取得できた範囲では」に限定 |
| 「prior art にコード公開は皆無」 | **A3(Krestenko et al.)は公開している**(GitHub + DOI 付きコードアーカイブ) |
| 「コスト明示的な研究は閾値 convergence trade だけ」 | **誤り**。A3 はコスト明示的かつ spot–perp basis を扱う |
| 「Crypto Carry のサンプルは 2020-08〜2025-05」(検索で流通している誤情報) | **誤り**。それは Borri et al. の**自前データ**の期間。A1 は 2019-03〜2024-07 |

**この節の存在自体が結論である**: 一次資料を取得しても、**取得結果の解釈**は独立に
検証しないと誤る。**調査 → 敵対的再検証**を Phase 8 の標準手順とする。

---

## 0. この文書の読み方(重要)

各文献に **verification status** を付ける。ango は今後この status を根拠の強さとして扱う。

| status | 意味 |
|---|---|
| `VERIFIED-FULL` | 一次資料の**本文**を取得し、サンプル期間・コスト仮定・検証設計まで確認した |
| `VERIFIED-META` | 一次資料の**書誌ページ**(arXiv abs / 出版社 / RePEc)を取得し、著者・日付・DOI・abstract を確認した |
| `PARTIAL` | 一次資料に到達できず(403 等)、書誌情報のみ二次的に確認。**内容の主張は未確認** |
| `UNVERIFIED` | **一次資料の存在自体を確認できなかった。** 主張の根拠として使用禁止 |

**捏造防止のための自己規律**: 本文書に書かれた数値・期間・著者名は、上記 status が
`VERIFIED-*` のものだけが「ango が確認した」ものである。`PARTIAL` / `UNVERIFIED` の
数値は「検索結果にそう書かれていた」以上の意味を持たない。

---

## 1. 調査方法(再現可能な形で記録)

### 1.1 手順

指示された検索順序に従った。

```text
1. 2026年の査読済み論文
2. 2026年の arXiv / SSRN / 研究機関 working paper
3. 2025年
4. 2023〜2024年
5. 引用されている基礎研究
6. 有力論文の引用文献と後続研究(arXiv 月次リスティングの走査を含む)
```

seed として与えられた8件をまず検証し、そこから
(a) 同一 arXiv 月次リスト、(b) 検索で共起した論文、(c) ango の ROADMAP §11–§14 に
既に記載されている参照、へ展開した。

### 1.2 実際に使用した検索語(全件)

| # | 検索語 | 目的 |
|---|---|---|
| Q1 | `"Fundamentals of Perpetual Futures" He Hu Xiong SSRN 4301150` | seed 検証(SSRN 403 の迂回) |
| Q2 | `"Crypto Factor Zoo" International Review of Financial Analysis 2026 DOI` | seed の DOI 検証 |
| Q3 | `SSRN 6993978 "The Funding Carry and a Cross-Venue Spread on Perpetual Futures"` | seed 検証 |
| Q4 | `SSRN 6805838 "Funding Timing and No-Arbitrage Bounds in Decentralized Perpetual Markets"` | seed 検証 |
| Q5 | `"funding carry" "cross-venue" perpetual futures spread SSRN 2026 delta-neutral Binance OKX Hyperliquid` | family B 展開 |
| Q6 | `"Funding Carry" "Cross-Venue Spread" Perpetual Futures Hyperliquid Binance Bybit paper author 2026` | Q3 の再試行 |
| Q7 | `arxiv 2026 bitcoin hourly forecasting transaction cost aware walk-forward q-fin.TR` | family C |
| Q8 | `arxiv 2026 cryptocurrency open interest liquidation cascade predictability perpetual futures empirical` | family E |
| Q9 | `arxiv 2026 cryptocurrency cross-sectional momentum reversal factor perpetual futures transaction costs out-of-sample` | family D |
| Q10 | `arxiv 2026 funding rate arbitrage cross exchange cryptocurrency perpetual empirical study transfer latency costs` | family B |
| Q11 | `BIS Working Paper "Crypto Carry" Schmeling Schrimpf Todorov sample period data` | family A のサンプル期間特定 |
| Q12 | `"Crypto carry" Schmeling Schrimpf Todorov sample "2018" OR "2019" to "2022" ... Management Science` | 同上(再試行) |
| Q13 | `arxiv 2026 LLM agent alpha factor discovery reproducible crypto backtest overfitting audit protocol` | family F |
| Q14 | `arxiv 2606.00071 "Bitcoin Price Prediction: Peer-Reviewed Evidence and Social Media Discourse"` | family C の対照(survey) |
| L1 | arXiv 月次リスティング `q-fin.TR/2026-07` の走査 | 後続研究の発見 |

### 1.3 到達できた／できなかった一次資料(2026-08-17 実測)

**この環境からの到達性は文献の質と無関係**である。記録は再現性のために残す。

| ドメイン | 結果 |
|---|---|
| `arxiv.org`(abs / html) | **到達可**。本レビューの検証の主軸 |
| `pubsonline.informs.org` | 到達可(302 → 明示的に再取得) |
| `ideas.repec.org` / `econpapers.repec.org` | 到達可 |
| `bis.org`(PDF) | HTTP は成功するが **PDF のテキスト抽出に失敗**(バイナリのまま) |
| `papers.ssrn.com` / `ssrn.com` | **403 Forbidden**(abstract ページ・Delivery.cfm とも) |
| `sciencedirect.com` / `linkinghub.elsevier.com` | **403 / リダイレクト止まり** |
| `onlinelibrary.wiley.com` | **403** |
| `mdpi.com` | **403** |
| `cepr.org` | **403** |

**帰結**: SSRN seed 3件(4301150 / 6993978 / 6805838)は SSRN 経由で本文を確認できなかった。
うち 4301150 は arXiv 版(2212.06888)が同一論文として存在するため `VERIFIED-META` へ昇格できた。
残り2件は arXiv 版が存在しない。

### 1.4 除外規則

- ブログ・ニュース・SNS・取引所マーケティング記事は**発見用途のみ**。
  本文書の主張の根拠にはしない(Medium / CCXT blog / 取引所 support ページ等は
  検索結果に出たが、候補の根拠として採用していない)。
- 報告収益率の高さは採点に配点しない(§4 の採点軸)。

---

## 2. seed 文献8件の検証結果

### 2.1 検証サマリ

| # | seed として与えられた指定 | 検証結果 |
|---|---|---|
| S1 | Crypto Carry `doi.org/10.1287/mnsc.2024.05069` | **一致・実在**(`VERIFIED-META`) |
| S2 | Fundamentals of Perpetual Futures `SSRN 4301150` | **実在**。SSRN は 403 だが arXiv 2212.06888 で確認(`VERIFIED-META`) |
| S3 | The Funding Carry and a Cross-Venue Spread on Perpetual Futures `SSRN 6993978` | **一次資料に到達できず・タイトル検索でも発見できず**(`UNVERIFIED`) |
| S4 | Funding Timing and No-Arbitrage Bounds in Decentralized Perpetual Markets `SSRN 6805838` | 著者・日付は検索で一致。本文未確認(`PARTIAL`) |
| S5 | ML-Based Bitcoin Trading Under Transaction Costs `arXiv 2606.00060` | **一致・本文まで確認**(`VERIFIED-FULL`) |
| S6 | Crypto Factor Zoo `doi.org/10.1016/j.irfa.2026.104962` | **指定 DOI は 404。実在するのは別 DOI**(下記 §2.2) |
| S7 | From Hypotheses to Factors `arXiv 2604.26747` | **一致・実在**(`VERIFIED-META`) |
| S8 | Implied ETF Carry Rates `arXiv 2605.29309` | **一致・実在**(`VERIFIED-META`) |

### 2.2 seed の誤りとして報告すべき2点

**(1) Crypto Factor Zoo の DOI が違う。**

- 指定された `10.1016/j.irfa.2026.104962` → **HTTP 404**(2026-08-17 実測)
- 実在するのは `10.1016/j.irfa.2026.105137`
  - 正確なタイトル: **"Crypto factor zoo (.Zip)"**(末尾の `(.Zip)` を含む)
  - 著者: Aleksander Mercik, Adam Zaremba, Ender Demir
  - 掲載: International Review of Financial Analysis, Vol. 113 (2026), PII `S1057521926000645`
  - 出典: RePEc/IDEAS の書誌ページで確認(出版社サイトは 403)

**(2) SSRN 6993978 の存在を確認できなかった。**

指定タイトル「The Funding Carry and a Cross-Venue Spread on Perpetual Futures」で
検索しても、当該 SSRN ID を持つ論文は発見できなかった(Q3・Q6)。
SSRN 自体が本環境から 403 のため、**「存在しない」とも「存在する」とも言えない**。

一方で、検索スニペットには**この題目の内容と強く一致する記述**(Hyperliquid の funding が
Binance/Bybit を年率約7%上回る、delta-neutral cross-venue spread が 2024年 14.7% /
2025年 4.6%、Newey-West HAC t = 4〜8、2〜3倍レバで所要資本比 7〜11% 等)が
出現した。ただし**これらをこの SSRN ID に帰属させる一次的根拠を得られなかった**。

**扱い**: `UNVERIFIED`。候補としては残すが、エビデンス品質で大きく減点し、
**Phase 8 の第1候補にはしない**。人間が SSRN へ直接アクセスできる環境で
存在と本文を確認するまで、この論文の主張を ango の設計根拠にしない(→ §7 の人間決定事項 H1)。

---

## 3. 候補文献(family 別)

各 family の全文献。**採否理由を必ず併記する。**

### Family A — Crypto carry / basis / perpetual pricing

| ID | 文献 | status | 一次資料 |
|---|---|---|---|
| A1 | **Crypto Carry** — Maik Schmeling, Andreas Schrimpf, Karamfil Todorov. *Management Science*, Articles in Advance, published **2026-05-06**(accepted 2025-11-13). DOI `10.1287/mnsc.2024.05069`。先行版: **BIS Working Paper No. 1087(2023-04)**、CEPR DP20719、SSRN 4268371 | `VERIFIED-META` | [INFORMS](https://pubsonline.informs.org/doi/10.1287/mnsc.2024.05069) / [BIS WP 1087](https://econpapers.repec.org/paper/bisbiswps/1087.htm) |
| A2 | **Fundamentals of Perpetual Futures** — Songrun He, Asaf Manela, Omri Ross, Victor von Wachter. arXiv `2212.06888`(v1 2022-12-13 / v6 2024-08-21)、q-fin.PR; q-fin.GN。SSRN 4301150 と同一 | `VERIFIED-META` | [arXiv](https://arxiv.org/abs/2212.06888) |
| A3 | **Dynamic Collateral Control for Permissionless Spot Perpetual Basis Trading** — Anatoly Krestenko, Mikhail Butov, Rostislav Berezovskiy, Danila Bolotin. arXiv `2605.05089`(2026-05-06)、q-fin.TR | `VERIFIED-META` | [arXiv](https://arxiv.org/abs/2605.05089) |
| A4 | **Implied ETF Carry Rates and the Limits of Arbitrage in Segmented Bitcoin Markets** — Mindy L. Mallory. arXiv `2605.29309`(2026-05-28)、q-fin.PR | `VERIFIED-META` | [arXiv](https://arxiv.org/abs/2605.29309) |
| A5 | **Perpetual Futures Pricing** — Ackerer. *Mathematical Finance*, 2026. DOI `10.1111/mafi.70018` | `PARTIAL`(Wiley 403) | — |

**A1 の内容(確認できた範囲)**: carry(先物と現物の価格差)は年率 40% を超えることがあり
時間変動が大きい。原因を(i)レバレッジを求める小口・トレンド追随投資家の需要、
(ii)規制・証拠金摩擦による裁定資本の制約、に帰す。BTC と ETH が対象。
**サンプル期間は INFORMS ページにも RePEc ページにも記載が無く、ango は未確認**
(BIS WP が 2023-04 であることから**サンプル終端は 2023 年初以前**と推定されるが、
これは推定であって確認された事実ではない → §7 H2)。

**A2 の内容**: 無摩擦市場での perpetual の no-arbitrage 価格を導出し、取引コストがある
市場での価格帯を与える。実証では暗号資産の理論価格からの乖離が通貨市場より大きく、
通貨間で共変動し、時間とともに縮小する。implied arbitrage 戦略が良好なリスク調整後
リターンを示すと報告。**サンプル期間の具体値は abs ページからは取得できていない**
(PDF 本文抽出に失敗)。

**採用**: A1 + A2 を Phase 8 の第1候補の理論的アンカーとする(→ §5)。
A3 は「実装時の collateral 制御」として設計参照に使う(再現対象ではない)。
**不採用**: A4(IBIT オプション・BlackRock 保有・CME が必要。ango は当該データを持たず、
取得経路も無い)。A5(Wiley 403 で内容未確認、かつ理論寄り)。

### Family B — Cross-venue funding spread

| ID | 文献 | status | 一次資料 |
|---|---|---|---|
| B1 | **The Funding Carry and a Cross-Venue Spread on Perpetual Futures** — SSRN 6993978(seed 指定) | **`UNVERIFIED`** | 到達不可 |
| B2 | **Funding Timing and No-Arbitrage Bounds in Decentralized Perpetual Markets** — Erce Erez, Mikhail Smirnov. SSRN `6805838`、2026-05-20。Drift Protocol と dYdX v4 | `PARTIAL`(SSRN 403) | 到達不可 |
| B3 | **Temporal Dynamics of Market Microstructure in Cryptocurrency Perpetual Futures: Econometric Evidence from Centralized and Decentralized Exchanges** — MDPI *IJFS* 14(5) 103 | `PARTIAL`(MDPI 403) | 到達不可 |
| B4 | **The Two-Tiered Structure of Cryptocurrency Funding Rate Markets** — MDPI *Mathematics* 14(2) 346 | `PARTIAL`(MDPI 403) | 到達不可 |
| B5 | **Exploring risk and return profiles of funding rate arbitrage on CEX and DEX** — ScienceDirect PII `S2096720925000818` | `PARTIAL`(SD 403) | 到達不可 |

**この family は本環境で一次資料に一件も到達できなかった。** これは family の価値が
低いことを意味しないが、**ango が現時点で内容を検証できない**ことを意味する。

B2 の内容(検索由来・未検証): 分散型 perp の no-arbitrage 境界の実証的内容は、
protocol の funding が**建玉時点で既知かどうか**に依存する。Drift は entry 時点の
EMA-TWAP 状態変数から funding が決まる(observable-funding)ため実現 funding が
境界を締める。dYdX は後続の premium サンプルから計算される(forward-looking)ため
clamp されている領域でのみ実証的内容を持つ。
→ **この「funding が決定時点で観測可能か」という区別は、ango の data contract §3
(availability)と同型の問題であり、機序として非常に明快である。**
一次資料を確認できれば候補順位が上がりうる(→ §7 H1)。

**採用**: B2 を候補として残す(順位は中位)。B1 は `UNVERIFIED` のため大幅減点のうえ候補に残す。
**不採用**: B3/B4/B5(内容未確認かつ B2 と主題が重複)。

### Family C — 1〜24時間の BTC 予測 / cost-aware execution

| ID | 文献 | status | 一次資料 |
|---|---|---|---|
| C1 | **Machine Learning-Based Bitcoin Trading Under Transaction Costs: Evidence From Walk-Forward Forecasting** — Andrei Bysik, Robert Ślepaczuk. arXiv `2606.00060`、q-fin.TR; cs.CE; cs.LG | **`VERIFIED-FULL`** | [arXiv abs](https://arxiv.org/abs/2606.00060) / [full text](https://arxiv.org/html/2606.00060v1) |
| C2 | **Bitcoin Price Prediction: Peer-Reviewed Evidence and Social Media Discourse** — Carlos Baquero(INESC TEC / Univ. Porto). arXiv `2606.00071` | `PARTIAL` | [arXiv](https://arxiv.org/abs/2606.00071) |
| C3 | **Train Often, Deploy Selectively: Forward-Gated Model Replacement in Crypto Markets** — Aditya Dutta. arXiv `2607.28577` | `PARTIAL`(月次リスティングで題目のみ確認) | — |

**C1 の抽出項目(本文確認済み。Phase 8 の scoring はこれに基づく)**

| 項目 | 値 |
|---|---|
| asset / venue / 商品 | BTC/USDT、**Binance USD-margined futures**(public REST API) |
| sample | **2017-12-01 〜 2026-01-01**、walk-forward 評価は 2018-01-01 から。**70,872 本の hourly bar** |
| frequency | 1時間 |
| information set | 3層: (i) OHLCV のみ (ii) +TA 10指標(fold 内で選択、窓 3–336h) (iii) +Student-t EGARCH の条件付ボラ3列 |
| target | 次の1時間の対数リターン `ln(P_{t+1}/P_t)` |
| train/val/OOS | **非アンカー ローリング**。train 12ヶ月 / validation 3ヶ月 / test 3ヶ月 / 3ヶ月ステップ = **27 fold**。特徴量構築・スケーリング・目的変数標準化・推定を fold 内で完結 |
| walk-forward | あり(27 fold)。validation で選択後、train+validation で再学習してから test |
| execution | 予測符号でポジション。long-only ∈ {0,1} / long-short ∈ {−1,1} |
| cost | **比例コスト c = 0.001(turnover 単位あたり 10bps)**。「取引所手数料・スプレッド・スリッページ等を含む all-in」と解釈と明記 |
| cost で**しない**こと | 板の深さ・部分約定・maker/taker ルーティング・時変スプレッドを**シミュレートしない**(著者自身が明記) |
| funding | **モデルに含めない** |
| leverage | 記載なし(ポジションは ±1 まで) |
| primary result | 3モデルとも gross では正の構成が存在。**素朴な符号戦略は 10bps コストで失敗**。コスト対応の執行フィルタ(予測の絶対値がコスト由来閾値を超えたときのみ取引)が turnover を大きく減らし、一部構成で収益性を回復。最強の long-only XGBoost は年率 65% 超・Sharpe 1 超 |
| **著者自身の重要な留保** | コスト対応戦略は **buy-and-hold に対する Sharpe の統計的有意な優越を確立しない** |
| statistical test | 10,000 回ブートストラップ、ブロック長 24 / 72 / 168 時間。**Holm-Bonferroni で FWER を制御** |
| 探索規模 | XGBoost 50 試行/fold(計1,350)、LSTM 40(1,080)、iTransformer 40(1,080)。これに feature 3層 × 損失2種 |
| code / data | **コード公開の記載なし**。データは Binance public REST |
| 既知の限界(著者記載) | 板シミュレーションなし / データ欠損近傍でリターンが機械的に平滑化されうる / feature 階層の差と閾値通過頻度の差を分離できない / hourly BTC/USDT 以外への一般化は未検討 / B&H への有意な優越なし |

**採用**: 候補として残す(上位)。ただし §6 の汚染分析で厳しい制約が付く。
**不採用**: C2(サーベイであり再現対象ではない。ただし**方法論規準の参照**として有用 → §5.3)。
C3(内容未確認)。

**注記(未解決の不整合)**: arXiv ID `2606.00060` は 2026年6月の連番だが、
abs ページからの抽出は投稿日を「2026-05-19」と返した。ango はこの不一致を解消していない。
引用時は **ID を正**とし、日付は「2026年5〜6月」と幅を持たせる。

### Family D — Crypto factor

| ID | 文献 | status | 一次資料 |
|---|---|---|---|
| D1 | **Crypto factor zoo (.Zip)** — Aleksander Mercik, Adam Zaremba, Ender Demir. *International Review of Financial Analysis* 113 (2026). DOI `10.1016/j.irfa.2026.105137` | `VERIFIED-META` | [IDEAS/RePEc](https://ideas.repec.org/a/eee/finana/v113y2026ics1057521926000645.html) |
| D2 | Liu & Tsyvinski (2021) / Liu, Tsyvinski & Wu (2022) の3ファクターモデル(market / size / momentum) | 基礎研究として言及のみ。**本レビューでは一次確認していない** | — |

**D1 の内容(確認できた範囲)**: alpha ベースの反復的ファクター選択を暗号資産へ適用。
**36 個のリターン予測ファクター**を評価し、**2〜3個のファクターで有意な portfolio alpha が
すべて消える**。流動性関連変数(turnover volatility、bid-ask spread)と
オンチェーン指標(new-address-to-price 比)が支配的。
**サンプル期間と対象銘柄数は IDEAS ページに記載が無く、ango は未確認**(→ §7 H3)。

**採用**: 候補として残す(中位)。**ango は BTC 単独資産の資産構成であり、
cross-sectional factor には銘柄ユニバース・上場廃止銘柄・オンチェーン指標が新規に必要**
という構造的なハンデがある。

### Family E — Derivatives / market structure

| ID | 文献 | status | 一次資料 |
|---|---|---|---|
| E1 | **Where does the criticality live? Early-warning signals are event-heterogeneous across seven crypto-perpetual liquidation cascades** — Ramon Marc Garcia Seuma. arXiv `2607.27070`(2026-07-29)、q-fin.ST; physics.soc-ph | `VERIFIED-META` | [arXiv](https://arxiv.org/abs/2607.27070) |
| E2 | **When Does Order Flow Matter? State-Dependent L2 Liquidity-State Transitions in Crypto Futures** — Joohyoung Jeon. arXiv `2607.09230`(2026-07-10)、q-fin.TR; cs.LG | `VERIFIED-META` | [arXiv](https://arxiv.org/abs/2607.09230) |
| E3 | **The Quarter-Hour Effect: Periodic Algorithmic Trading and Return Predictability in Cryptocurrency Futures** — Chan Kim, Peter Reinhard Hansen. arXiv `2607.09426` | `VERIFIED-META`(月次リスティング) | [arXiv listing](https://arxiv.org/list/q-fin.TR/2026-07) |

**E1**: BTC の大規模清算カスケード7件(2022〜2025、2025-10-10 を含む)を分 足価格と
5分粒度のレバレッジ/オーダーフローで分析。価格の critical-slowing-down は 7件中5件で
現れるが、突発ニュース型2件では現れない。全事象共通なのは **taker order-flow 分散の圧縮**で、
300-onset の placebo test は通過するものの**個別事象の警報ではなく母集団水準の指標**。

**E2**: 2023〜2026、Binance BTCUSDT/ETHUSDT、tick 粒度の top-20 L2 板と約定フロー。
**事前の L2 流動性状態が主たる予測子で、order flow はその状態モデルの上に乗せたときだけ
増分価値を持つ。** BTC の改善は限定的・断片的で、ETH の方が一貫した改善。
ローリング月次 OOS と blocked permutation test を使用。

> **ango にとっての意味**: E2 は ROADMAP の Branch C が事前に書いていた
> 「**OFI が単体で効く、ではなく OFI × liquidity state として検証する**」と同型の
> 結論を、独立に、より深い板データで示している。ango の Tier 1/2 設計の妥当性を支持するが、
> 同時に「BTC では効果が限定的」という**追加の悲観材料**でもある。

**E3**: ango が Phase 1B で既に独立検証済み(方向性なし・活動構造あり、
[findings](../findings/2026-08-16-phase1b-clock-phase-v1.md))。**再現候補としては完了扱い。**

**採用**: E2 を候補として残す(中位)。
**不採用**: E1(liquidation データが ango では **blocked** — Binance Vision の
`liquidationSnapshot` は 2026-08-16 実測で 404、backlog I7)。E3(実施済み)。

### Family F — LLM quantitative research

| ID | 文献 | status | 一次資料 |
|---|---|---|---|
| F1 | **From Hypotheses to Factors: Constrained LLM Agents in Cryptocurrency Markets** — Yikuan Huang, Zheqi Fan, Kaiqi Hu, Yifan Ye. arXiv `2604.26747`(2026-04-29)、q-fin.PM; q-fin.GN; q-fin.TR | `VERIFIED-META` | [arXiv](https://arxiv.org/abs/2604.26747) |
| F2 | **The Alpha Illusion: Reported Alpha from LLM Trading Agents Should Not Be Treated as Deployment Evidence** — Yuxuan Ye ほか9名. arXiv `2605.16895`(2026-05-16)、cs.CE; cs.AI; cs.CL。**コード公開あり**(`github.com/hj1650782738/Trading`) | `VERIFIED-META` | [arXiv](https://arxiv.org/abs/2605.16895) |
| F3 | **Beyond Agent Architecture: Execution Assumptions and Reproducibility in LLM-Based Trading Systems** — Junyi Yao, Zihao Zheng. arXiv `2606.08285`(2026-06-06) | `VERIFIED-META` | [arXiv](https://arxiv.org/abs/2606.08285) |

**F1**: 逐次仮説探索として定式化。エージェントが append-only の実験トレースを読み、
反証可能な仮説を提案し、実行可能なレシピへ写像する。決定論的エンジンが
固定 split・選抜ゲート・取引コスト・ポートフォリオ検定を強制。
候補行動は **point-in-time factor DSL** に制限され、成功・失敗の双方が監査可能。
**2020–2022 のみで学習した ridge 合成ポートフォリオが、2024–2026 の純 OOS で
年率 44.55% / Sharpe 1.55(片道 5bps 後)。**

> **ango にとっての意味**: F1 のアーキテクチャは ango が Phase 2/3 で既に構築したもの
> (frozen DSL + AST + whitelist compiler + deterministic Judge + append-only artifact +
> search budget 会計)と**ほぼ同型**である。したがって F1 は「新しく実装すべきもの」ではなく
> **ango の既存設計の外部的な妥当性確認**として読むのが正しい。
> 差分は (a) cross-sectional な銘柄ユニバース、(b) point-in-time factor DSL の語彙、
> (c) ridge によるファクター合成、の3点。

**F2 / F3**: どちらも**取引仮説ではなく報告規準**。F2 は6つの構造的妥当性検定
(temporal integrity / real-world frictions / counterfactual robustness / predictive
calibration / numerical execution / multi-agent disaggregation)と最小報告プロトコル
P1–P6 を提案し、再現ハーネスを公開している。F3 は 30 研究を対象に、
アーキテクチャの記述は明快だが**経済的に解釈可能かを判断するための評価前提の記述が不十分**
と結論。

**採用**: F1 を候補として残す(下位。理由は §5 の採点)。
**F2/F3 は再現候補ではなく「Phase 8 の報告規準として採用する対象」**とする(→ §5.3)。

---

## 4. 採点方式

指定された配点をそのまま用いる。

| 評価軸 | 点数 |
|---|---:|
| 経済的機序の明確さ | 15 |
| データ入手可能性 | 15 |
| コスト・約定の現実性 | 15 |
| 独立 confirmation 確保 | 15 |
| 再実装可能性 | 10 |
| ango 資産の再利用性 | 10 |
| 個人研究者としての実行可能性 | 10 |
| エビデンス品質 | 10 |
| **合計** | **100** |

**報告収益率の高さには配点しない。**

明示的減点(該当時に上記軸の内側で減じ、[replication_candidates_v1](replication_candidates_v1.md)
の `deductions` に**必ず列挙**する): 原論文の期間外データを確保できない / コード・データ・数式の
不足 / 非現実的な取引コスト / maker fill の楽観仮定 / survivorship bias / 単一 split /
多重比較未補正 / 報告結果が少数期間へ集中 / 取引所間送金を即時・無料と仮定 /
liquidation・margin・collateral の無視 / 個人には取得困難なデータ / 追試の無い最近の preprint のみ。

採点結果は [replication_candidates_v1.md](replication_candidates_v1.md) と
[replication_candidates_v1.json](replication_candidates_v1.json)(機械可読・正)にある。

---

## 5. データ可用性の実測(2026-08-17、本実行環境から)

**ラベル・将来リターンは一切計算していない。存在・粒度・到達性のみ。**

### 5.1 Binance Vision(一括ダンプ)— レンジ GET で実測

| データセット | パス | 実測 |
|---|---|---|
| spot klines 5m | `data/spot/monthly/klines/BTCUSDT/5m/` | **HTTP 206 / 2017-08 から取得可** |
| spot klines 1h | `data/spot/monthly/klines/BTCUSDT/1h/` | HTTP 206 / **2026-07 も取得可** |
| futures(um) klines 1h | `data/futures/um/monthly/klines/BTCUSDT/1h/` | HTTP 206 / 2026-07 取得可 |
| **futures fundingRate(月次)** | `data/futures/um/monthly/fundingRate/BTCUSDT/` | **HTTP 206 / 2020-01 と 2026-07 の両方で取得可** |
| premiumIndexKlines 5m | `data/futures/um/monthly/premiumIndexKlines/BTCUSDT/5m/` | HTTP 206 / 2026-07 取得可 |
| **indexPriceKlines 5m** | `data/futures/um/monthly/indexPriceKlines/BTCUSDT/5m/` | **HTTP 206 / 取得可**(2024-01 で確認) |
| metrics(日次) | `data/futures/um/daily/metrics/BTCUSDT/` | HTTP 206 / 2026-07-01 取得可 |
| liquidationSnapshot | — | **404(2026-08-16 実測、backlog I7)。取得不可** |

**fundingRate ファイルの実列**(2024-01 の中身を実測):

```csv
calc_time,funding_interval_hours,last_funding_rate
1704067200000,8,0.00037409
1704096000000,8,0.00027213
```

- `calc_time` は epoch ms。1704067200000 = **2024-01-01T00:00:00Z**。
- **funding interval は 8 時間**(00:00 / 08:00 / 16:00 UTC)。
- **timestamp semantics は Phase 8 の事前登録で確定させる必要がある**
  (`calc_time` が「決済時刻」か「計算時刻」か。ango の `data_contract §8` は OKX について
  「`fundingTime` = 決済時刻」と定めており、Binance についても同等の宣言が要る → §7 H4)。

**含意**: **Family A(spot–perp funding carry / basis)に必要なデータは、
すべて本環境から本日取得可能である。** しかも ango は既に `mce.binance_vision`
(公開 CHECKSUM 検証つき・冪等)と `mce.normalize_binance` を実装済みで、
spot klines / fundingRate / indexPriceKlines は**同じ機構の対象パスを増やすだけ**で取り込める。

### 5.2 取引所 API の到達性(2026-08-17 実測)

| venue | エンドポイント | 結果 |
|---|---|---|
| OKX v5 | `funding-rate-history` | **200**(ただし遡及は約3ヶ月。既知) |
| Binance REST | `api.binance.com/api/v3/klines` | **451**(地理ブロック。既知) |
| Bybit v5 REST | `api.bybit.com/v5/market/tickers` | **403**(既知) |
| **Bybit public dump** | `public.bybit.com/` | **200**。ディレクトリは `trading/`(BTCUSDT は **2020-03-25 から**の約定 csv.gz)、`premium_index/`、`spot_index/`、`spot/`、`kline_for_metatrader4/`。**funding rate のダンプは無い** |
| **Hyperliquid** | `api.hyperliquid.xyz/info`(POST) | **200**。`fundingHistory` が動作 |

**Hyperliquid funding history の遡及深さ(実測)**:

| 探索日 | 返却された hourly funding 点数 |
|---|---|
| 2023-05-03 | **0** |
| 2023-08-01 | 24 |
| 2023-11-01 | 24 |
| 2023-12-01 | 24 |

→ **Hyperliquid の BTC funding 履歴は 2023-05 以降 2023-08 以前のどこかで開始し、
粒度は1時間**(返却フィールドは `coin` / `fundingRate` / `premium` / `time`)。
**Binance の 8 時間 funding とは支払間隔が異なる**ため、cross-venue 比較には
間隔の正規化が必須になる(family B の実装難度を押し上げる要因)。

**帰結(family B について)**: Hyperliquid 側は取得可能だが、
(a) 履歴が約3年しかない、(b) Bybit の funding は本環境から取得経路が無い、
(c) 支払間隔が venue 間で異なる、(d) 一次資料が `UNVERIFIED` / `PARTIAL`。
→ family B は**データ的には部分的に可能、根拠的には現時点で最弱**。

### 5.3 ango が現在ローカルに持っているもの

**重要**: 本セッションのコンテナには `data/manifests/*.json`(git 管理)しか存在しない。
Parquet 実体は `.gitignore` 対象で、**Phase 8 の実行時には再取得が必要**である。
manifest が記録している資産:

| manifest | 行数 | 期間 |
|---|---:|---|
| `binance_klines_klines_BTCUSDT_5m` | 631,296 | 2020-01-01 〜 2025-12-31 |
| `binance_premium_index_..._5m` | 629,246 | 2020-01-01 〜 2025-12-31 |
| `binance_metrics_metrics_BTCUSDT_5m` | 560,388 | 2020-09-01 〜 2025-12-31 |
| `ohlcv_okx_BTC-USDT-SWAP_5m` | 288,124 | 2023-11-19 〜 2026-08-16 |
| `funding_rate_okx_BTC-USDT-SWAP` | 280 | **2026-05-14 〜 2026-08-15(約3ヶ月のみ)** |
| `open_interest_okx_BTC-USDT-SWAP_5m` | 1,441 | **2026-08-10 〜 2026-08-15(約5日のみ)** |

**未取得(Phase 8 で新規に必要)**: BTC **spot** OHLCV、Binance **fundingRate**、
Binance **indexPriceKlines**。いずれも §5.1 のとおり取得経路は確認済み。

---

## 6. 外部情報による OOS 汚染(`external_knowledge_contamination`)

### 6.1 なぜこの節が必要か

**論文の結果を読み、その結果を理由に仮説を選んだ時点で、論文が使った期間は
ango が価格データを直接見ていなくても、完全な未知 OOS ではない。**

本レビューを実施した結果、ango(および本タスクを実行したエージェント)は
以下の外部知識を取得した。**これは取り消せない。**

| # | 取得してしまった知識 | 対象期間 | 出所 status |
|---|---|---|---|
| K1 | 暗号資産の carry は年率 40% を超えることがあり、時間変動が大きい。原因は小口レバレッジ需要と裁定資本の制約 | A1 のサンプル(**期間未確認**、2023年初以前と推定) | `VERIFIED-META` |
| K2 | perpetual の理論価格からの乖離は通貨市場より大きく、**時間とともに縮小する** | A2(〜2024年頃) | `VERIFIED-META` |
| K3 | hourly BTC の ML 予測は 10bps コストで素朴符号戦略が失敗し、コスト対応フィルタで一部構成が回復するが、**B&H への有意な優越は無い** | **2017-12 〜 2026-01**(ango の research + validation 窓を完全に覆う) | `VERIFIED-FULL` |
| K4 | cross-sectional crypto factor は **2〜3個で有意 alpha が消える**、流動性系が支配的 | D1(期間未確認) | `VERIFIED-META` |
| K5 | 制約付き LLM エージェントの ridge 合成は 2024–2026 OOS で年率 44.55% / SR 1.55(5bps 片道後) | **2024–2026**(ango の validation 窓 + **封印域の一部**に重なる) | `VERIFIED-META` |
| K6 | BTC では L2 流動性状態が主で order flow の増分は限定的(ETH の方が一貫) | **2023–2026** | `VERIFIED-META` |
| K7 | (検索スニペット由来・**未検証**)Hyperliquid の単一 venue carry は 2024年 約17.9% / 2025年 約3.6%、cross-venue spread は 2024年 14.7% / 2025年 4.6%、2026年も有意 | 2024–2026 | `UNVERIFIED` |

### 6.2 期間の3分類(候補ごと)

指定された3分類を適用する。**ango の現行封印は `final_oos = ts >= 2026-01-01`。**

| 候補 | ① 原論文内サンプル(**独立 OOS と呼ばない**) | ② 原論文が未使用の期間(独立 confirmation 候補) | ③ 選定後に将来蓄積(prospective Final 候補) |
|---|---|---|---|
| **A(carry / basis)** | A1: 〜2023年初(**推定・要確認**)。A2: 〜2024年頃 | **2024-01 〜 2026-08**(A1 の外側)。ango の research(2023-11-19〜2025-07)と validation(2025-07〜2026-01)を含む | **2026-09 以降**(新規に蓄積される分) |
| **C(hourly ML)** | **2017-12 〜 2026-01**(ango の research + validation を**完全に覆う**) | **2026-01 〜 2026-08 のみ**(= ango の現行封印域と一致) | 2026-09 以降 |
| **D(factor zoo)** | 期間未確認(要確認) | 未確定 | 2026-09 以降 |
| **B(cross-venue)** | B1 は未検証だが 2024–2026 を扱うと推定。B2 は 2026 前半まで | 実質ほぼ無い(2026 まで使われている) | 2026-09 以降 |
| **F1(LLM agent)** | train 2020–2022 / OOS 2024–2026 | 2023 年のみ(狭い) | 2026-09 以降 |
| **E2(L2 order flow)** | 2023–2026 | ほぼ無い | 2026-09 以降 |

### 6.3 ango の封印との重なりと、その影響

**重要な事実**: 上表の複数の候補で、原論文のサンプルが **ango の封印域
(`ts >= 2026-01-01`)に踏み込んでいる**(C は 2026-01-01 で終端、F1 は 2024–2026、
B・E2 は 2026 まで)。

**影響**:

1. **ango の `final_oos` は、これらの候補にとって「完全に未知の期間」ではなくなった。**
   ango はその期間の価格データを一度も見ていないが、**その期間に何が起きたかについての
   要約統計を文献経由で受け取ってしまった**。
2. とくに候補 C は深刻である。サンプルが **2017-12 〜 2026-01** で、
   ango の research 窓・validation 窓・封印開始点をすべて覆う。
   **候補 C については、ango が持つどの既存 split も「独立 confirmation」を提供しない。**
3. 候補 A は相対的に軽い。A1 の先行版が 2023-04 の BIS WP であることから、
   **ango の research 開始(2023-11-19)以降は A1 のサンプル外**である可能性が高い。
   ただしこれは推定であり、**A1 のサンプル終端の確認が最優先の未確定事項**(§7 H2)。
   また K1/K7 により「BTC perp の funding carry は平均的に正で、2024年 > 2025年」という
   **符号と大小関係**は既に知ってしまった。したがって ango の 2024–2025 は
   「効果量の発見」には使えず、「機序の再現性確認」にのみ使える。

### 6.4 提案する対処:`external_knowledge_contamination` の別管理

ango の split 規約(`mce.backtest.splits`)は**変更しない**。そのうえで、
Phase 8 の事前登録に**次の3層を追加定義する**ことを提案する。

```text
layer 1  literature_in_sample     : 原論文が使った期間。再現性確認のみ。GO 判定に使わない
layer 2  contaminated_confirmation: 原論文外だが、本レビューで要約統計を知ってしまった期間
                                     → 機序の符号・形の再現確認に使う。効果量の発見には使わない
layer 3  prospective_final        : 事前登録 freeze 日より後に初めて生成されるバー
                                     → Phase 8 の最終判定はここで行う
```

**layer 3 の具体案**: `phase8_prospective_final_start = 2026-09-01T00:00Z`
(Phase 8.1 の事前登録 freeze 予定日より後の月初)。

- これは既存の `final_oos`(2026-01-01)を**開封するものでも、動かすものでもない**。
  既存の封印はそのまま維持する。
- 2026-01-01 〜 2026-08-31 は「既存封印域であり、かつ外部知識汚染がある期間」として
  **二重に触らない**扱いにする。
- Phase 8 の GO 判定を layer 3 に置くことで、**文献を読んだこと自体が判定を汚染しない**
  設計になる。代償は待ち時間である(→ §7 H5 で人間が判断)。

**記録義務**: §6.1 の K1–K7 は Phase 8 事前登録の付録として**そのまま転記**し、
「Phase 8 の設計者が知っていた外部知識のリスト」として凍結する。

---

## 7. 人間が決定すべき未確定事項

| # | 事項 | なぜ ango(エージェント)が決められないか | 推奨 |
|---|---|---|---|
| **H1** | SSRN 6993978(seed S3)の存在確認と本文入手 | 本環境から SSRN が 403。**存在自体が未確認** | 人間の環境で確認。存在しなければ候補 B1 を削除し、その旨を記録に残す |
| **H2** | **Crypto Carry(A1)のサンプル期間の確定** | INFORMS・RePEc とも記載なし、BIS PDF はテキスト抽出失敗 | 最優先。**候補 A の独立 confirmation 窓の定義がこれに依存する** |
| **H3** | Crypto factor zoo(D1)のサンプル期間・銘柄数 | IDEAS ページに記載なし、出版社 403 | 候補 D を上位に上げる場合のみ必要 |
| **H4** | Binance `fundingRate.calc_time` の semantics(決済時刻か計算時刻か) | 公式仕様の確認が必要。**availability 宣言がこれで決まる** | Phase 8.1 の事前登録前に必須 |
| **H5** | `phase8_prospective_final_start` を設けるか、設けるなら日付 | 研究の待ち時間とのトレードオフ。**研究者の時間予算の問題** | §6.4 の案(2026-09-01)を推奨 |
| **H6** | spot leg の執行前提(現物買い / 借入コスト / 証拠金) | ango は取引所口座の実条件を知らない | Phase 8.1 の事前登録で固定 |
| **H7** | Binance Vision / 各取引所 ToS 上の利用可否(日本居住者) | 法務判断。[data_sources.md](../data_sources.md) で継続して「要確認」 | 変わらず要確認 |
| **H8** | F2(Alpha Illusion)の P1–P6 を ango の報告規準として正式採用するか | 研究運用方針の決定 | 採用を推奨(§5.3) |

---

## 8. 本レビューが言っていないこと

1. **「候補 A が儲かる」とは言っていない。** 本レビューは損益を1つも計算していない。
2. **「候補 C の 65% が偽である」とは言っていない。** 著者自身が B&H への有意な優越を
   主張していない、という事実を記録しただけである。
3. **「family B に価値が無い」とは言っていない。** 本環境から一次資料に到達できなかった、
   という**ango 側の制約**である。
4. **「Hyperliquid の carry が 7% である」とは言っていない。** それは `UNVERIFIED` の
   検索スニペット由来であり、ango は確認していない。
5. **網羅的な文献調査ではない。** §1.2 の検索語で到達できた範囲であり、
   到達できなかった出版社サイト(SSRN / Elsevier / Wiley / MDPI / CEPR)の背後には
   未発見の関連研究がありうる。

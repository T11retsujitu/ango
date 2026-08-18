# H15 — A2 の Aave 金利ソースの同定(調査記録)

- 実施日: 2026-08-17
- 契機: 凍結 prereg が `RATE_SOURCE = aave_variable_borrow_apr` /
  `RATE_ASSETS = USDT/USDC/DAI` / `RATE_AGGREGATION = equal_weight_mean` しか
  定めておらず、**Aave は version・network・market が複数あるため一意に定まらない**
- 指示: A2 の全文・付録・脚注・補足・著者コード/データを再読し、
  **どの Aave デプロイ/版/ネットワークとどの系列を使ったか**を確定する
- **結論: 同定できない。停止して報告する。**
- **本調査でリターン・損益・戦略成果を一切計算していない。**

---

## 1. 結論(先に書く)

> **A2 は Aave のどの版・どのネットワーク・どの market・どのデータ提供元を使ったかを
> 一切書いていない。** 記載は「Aave」というプロトコル名と、対象3ステーブルコイン、
> 平均の取り方、そして日次であることだけである。
>
> **したがって A2 の金利系列は、本文からは再現できない。**
> これは **source-fidelity limitation** であり、ango 側の調査不足ではない。

指示のとおり **proxy は1つだけ提案し、選択はしない**(§5)。

---

## 2. A2 が実際に書いていること(全文中の Aave 言及は **7 箇所 / 5 文脈**)

`arxiv.org/pdf/2212.06888v6` の全文を抽出して網羅的に走査した。

| # | 逐語(要約せず引用) |
|---|---|
| 1 | "To measure the deviation from the no-arbitrage price, we also obtain the interest rate data from **Aave, a leading open-source DeFi liquidity protocol**." |
| 2 | "There are three major stablecoins traded on Aave: **USDT, USDC, and DAI**. To get a robust measure of the risk-free rate, we take an **average of the three interest rates**." |
| 3 | "When the perpetual futures price is above the spot, an arbitrageur would short the futures and long the spot, and would finance her position by borrowing in the cash market … Therefore, we use the **borrowing rate** from Aave as the risk-free rate." |
| 4 | "when the perpetual futures price is below the spot … we use the **supply rate** from Aave as the risk-free rate." |
| 5 | Figure 8 caption: "This figure presents the **daily** supply and borrowing rate from Aave. … **Each day, we take the average interest rate of the three crypto stablecoins**. … The sample period is from **2020-01-08** to 2024-03-11." |

### 2.1 A2 が書いて**いない**こと(すべて再現に必須)

| 未記載項目 | なぜ必要か |
|---|---|
| **プロトコル版**(V1 / V2 / V3) | 版ごとに金利モデルもプール構成も違う |
| **ネットワーク**(Ethereum mainnet / L2) | 同じ資産でも rate が異なる |
| **market/instance**(Core / Prime / EtherFi 等) | V3 は Ethereum 上に複数 market を持つ |
| **variable か stable か**(borrow rate) | V1/V2 は両方を提供していた。**凍結 prereg の `variable` は A2 に根拠が無い特定化である** |
| **データ提供元**(subgraph / The Graph / Dune / 商用 API) | 提供元ごとに丸め・欠測・再構成が違う |
| **日次観測の時刻規約** | A2 は価格について "0:00 GMT each day" と述べるが、**金利について同じ規約を明示していない** |
| **単位** | Figure 8 の軸は "Annualized interest rate, percent" だが目盛りは 0.00–0.40。Aave のステーブル金利水準からして **値は小数(0.40 = 40%)** であり、軸ラベルは緩い表記と解される |

### 2.2 著者コード・データ

**存在しない**。v6 PDF(64頁)に replication package / GitHub / Zenodo / Dataverse /
data availability statement のいずれも無い(全文キーワード走査。前回の prior-art
掃き出しでも同結論)。著者ページ `www.songrunhe.com/publication/perp/` は **HTTP 404**。

---

## 3. 唯一の強い手がかり:サンプル開始日 = Aave V1 の genesis

A2 本文:

> "**Our interest rate data starts from 2020-01-08.** Therefore, for coins with
> perpetual data available before the time (BTC and ETH), we begin the analysis
> from 2020-01-08."

すなわち **A2 のサンプル開始日は金利データの開始日に律速されている**。

| 事実 | 出所 |
|---|---|
| **Aave V1 は 2020-01-08 に Ethereum mainnet でローンチ** | Aave 公式アカウント "On January 8, 2020, Aave V1 first launched." / 公式 changelog |
| Aave V2 Ethereum: **2020-12-03** | 公式 changelog |
| Aave V3 Ethereum(Core market): **2023-01-27** | 公式 changelog |
| V3 Prime market: 2024-07-29 / EtherFi market: 2024-09-09 | 公式 changelog(**A2 のサンプル終了 2024-03-11 より後**) |
| **Aave V4 Ethereum: 2026-03-30** | 公式 changelog |

**A2 の金利系列の起点は Aave V1 の稼働開始日と一致する。**
これは「起点が V1 である」ことの強い状況証拠だが、
**サンプル(2020-01-08 〜 2024-03-11)は V1 → V2 → V3 の3世代をまたぐ**。
A2 は **世代をまたぐ接合をどう行ったかを一切述べていない**。

```text
2020-01-08 ── V1 ──┬── 2020-12-03 ── V2 ──┬── 2023-01-27 ── V3 Core ──┬── 2024-03-11
                   │                       │                           │
              A2 の起点                                          A2 の終点
```

---

## 4. ango 側の窓との重大な非対称(**新たに判明**)

A2 のサンプルは V1〜V3 の時代である。**ango の評価窓はそうではない。**

| ango の層 | 期間 | その時期に現存する Aave 世代 |
|---|---|---|
| layer 1(literature in-sample) | 2020-01-01 〜 2025-06-01 | V1 → V2 → V3 Core |
| layer 2(contaminated confirmation) | 2025-06-01 〜 2026-01-01 | **V3 のみ** |
| **layer 3(prospective final)** | **2026-09-01 〜** | **V4(2026-03-30 以降)** |

> **layer 3 は A2 が一度も見ていない Aave 世代(V4)の上で評価されることになる。**
> V4 は "hub-and-spoke liquidity architecture" であり、金利モデルの構造が違う。
> **これは金利系列の連続性に関する未解決の設計問題であり、H15 の一部として登録する。**

---

## 5. 提案する proxy(**1つだけ。選択はしない**)

指示に従い、**「一番引きやすいから」という理由で現行 V3 market を代入することはしない**。
以下を**唯一の提案**として出す。**採否は人間が決める。**

### 提案 P1 — "canonical-Ethereum, version-current"

```text
network  : Ethereum mainnet のみ(L2 を含めない)
market   : その時点の canonical な primary market
           V1 → V2 → V3 Core → V4 の primary hub
rate     : variable borrow APR(A2 の "borrowing rate" に対応)
           ※ Arm R は long-spot-only なので supply rate は使わない(R_PRIME = 0)
assets   : USDT / USDC / DAI の等加重平均(A2 と同じ)
sampling : 各日 00:00 UTC の point-in-time 観測
接合     : 版の切替日で単純に接合し、**接合点を artifact に明示的に記録する**
           (平滑化・補間・遡及再計算をしない)
欠測     : MAX_STALE を超えたら **null**。前方補完しない
```

**この提案の根拠**: A2 の系列起点が V1 genesis と一致することから、
A2 は「その時点で現存する Aave」を追っていたと解するのが最も自然である。

**この提案の弱点(隠さずに書く)**:

1. **A2 がそうしたという証拠は無い。** 状況証拠(起点の一致)だけである。
2. **接合点で系列が不連続になりうる。** V1→V2→V3→V4 で金利モデルが変わる。
3. **`variable` の特定化に A2 の根拠が無い。** V1/V2 には stable borrow rate もあった。
4. **layer 3 は V4 になる**(§4)。A2 が見た世代と別物である。
5. DAI は時期により実質的な性格が変わっている(ここでは扱わない)。

### 明示的に採らなかった代替(記録として残す)

| 代替 | 採らなかった理由 |
|---|---|
| V3 Core のみに固定 | layer 1 が **2023-01-27 以降**に切り詰められ、A2 のサンプルの大半を失う |
| 現行 V4 のみに固定 | layer 1 / layer 2 が**丸ごと消える** |
| 商用アグリゲータの "Aave 平均金利" | 再構成方法がブラックボックスで、再現性が下がる |
| 伝統的金融の無リスク金利(Kenneth-French)に置換 | **既に感応度として凍結済み**であり、primary の置換ではない |

---

## 6. 帰結:H15 は未解決。実験をブロックする

```text
RATE_MARKET_IDENTITY_STATUS = "unresolved_source_fidelity_limitation"
```

- **H13 / H14 と同じ扱い**とする。解決するまで experiment runner を起動しない。
- **Aave の履歴 adapter は実装しない**(指示どおり)。
- **純粋な数学層(ρ・境界・Arm R シグナル)は実装してよい**。
  `r` を**明示的な入力**として受け取る形にすれば、ソース同定と独立に検証できる。

---

## 7. 本調査が言っていないこと

1. **A2 が誤っているとは言っていない。** 論文としては通常の記述水準である。
   再現に必要な粒度が無い、というだけである。
2. **提案 P1 を採用したとは言っていない。** 人間の承認待ちである。
3. **Aave のデータを1バイトも取得していない。**
4. **リターン・損益・戦略成果を計算していない。** Layer 1/2/3 も読んでいない。

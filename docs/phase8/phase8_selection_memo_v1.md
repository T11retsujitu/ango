# Phase 8.0 — 選定メモ(v1)

- 作成日: 2026-08-17
- 前提: [Phase 7 closeout](../findings/2026-08-17-phase7-tier0-closeout-v1.md) /
  [文献レビュー](literature_review_2026-08-17.md) /
  [候補採点](replication_candidates_v1.md)(正: [JSON](replication_candidates_v1.json))
- **本メモは損益を1つも計算していない。バックテスト未実行。Final OOS 未開封。**
- **本メモは事前登録ではない。** freeze は Phase 8.1 の事前登録文書で行う。

---

## 0. v1.1 訂正(2026-08-17。**追記型・元の記述を消さない**)

第1位候補の**両アンカー論文の全文を取得**した結果、本メモ初版に重大な誤りが見つかった。
**結論(第1位の選定)は変わらないが、理由の中身が変わる。**

| id | 初版 | 訂正 |
|---|---|---|
| **X1** | A1 *Crypto Carry* を perpetual carry のアンカーとした | **A1 は dated(固定満期)futures の論文。アンカーから外し「経済的文脈」へ降格。A2 が唯一のアンカーになる** |
| **X2** | 「BIS WP が 2023-04 だからサンプル終端は 2023年初以前」と推定(§2.3 の核心) | **誤り。実際は 2019-03〜2024-07。** 配信 PDF の版は "October 1, 2025" |
| **X3** | A2 の戦略を固定 horizon の carry と想定 | **random-maturity arbitrage(閾値 entry / 閾値 exit、保有期間は内生)。** サンプル 2020-01-08〜2024-03-11、venue Binance、**BTC で Sharpe 1.8(リテール高コスト)/ 最大 3.5** |
| **X4** | H4(`calc_time`)を fatal blocker とした | **解決**(= funding 決済時刻) |

**採点への影響**: P8-C1 は **89 → 81点**。**第1位は不変**(2位は 64点)。
主な減点は独立 confirmation(13 → 9)と証拠品質(10 → 8)。

**§2.3 の書き直し**: 独立 confirmation 窓は **2023-11-19 以降の26ヶ月ではなく、
2024-08-01 以降の約17ヶ月**である(A1 終端 2024-07 と A2 終端 2024-03-11 の遅い方)。
**§7 の layer 1/2 境界もこれに合わせて 2024-08-01 へ変更した。**

さらに、**凍結前の独立敵対監査**(5レンズ中2レンズ完了)が fatal 4件を含む
26件の設計欠陥を検出した。詳細と対応は
[carry_protocol_audit_v1](carry_protocol_audit_v1.md) と
[protocol v1.2 §0-A](carry_replication_protocol_v1.md)。

**この訂正自体が §12-5 の主張の実例である**: 本メモは事前登録ではないので、
**実行前にいくらでも直せる**。直せなくなるのは凍結の後である。

---

## 1. 推薦上位5件

| 順位 | ID | 候補 | 合計 |
|---:|---|---|---:|
| **1** | **P8-C1** | **BTC spot–perp funding carry / basis**(*Crypto Carry* + *Fundamentals of Perpetual Futures*) | **89** |
| 2 | P8-C2 | hourly BTC ML + cost-aware execution filter | 64 |
| 3 | P8-C3 | crypto factor zoo(反復 alpha 選択) | 57 |
| 4 | P8-C4 | funding timing と DEX の no-arbitrage 境界 | 54 |
| 5 | P8-C5 | 制約付き LLM エージェント + point-in-time factor DSL | 52 |

---

## 2. 第1位を選んだ理由

**P8-C1 は、他の候補が1つでも欠いている条件を4つとも満たす唯一の候補である。**

### 2.1 機序が推定量ではなく契約である(15/15)

funding は「推定された予測力」ではなく、**取引所の契約に定められた現金の受け渡し**である。
8時間ごとに、建玉している側に対して実際に資金が動く。
Phase 3 と Phase 7 で ango が繰り返し当たった壁は「効果量が推定で、窓を変えると消える」
というものだった(dev 最大効果の符号反転がその典型)。**funding にはこの失敗モードが無い。**
受け取ったか受け取っていないかは会計の問題であって推定の問題ではない。

そのうえで、
- **A1(Management Science, 査読済み)** が経済的な説明を与える
  (inconvenience yield の変動 + レバレッジ需要 + 裁定資本の制約)、
- **A2(arXiv v6)** が no-arbitrage の理論価格と**取引コストを織り込んだ価格帯**を与える。

**なぜ carry が存在し続けるのか**の説明(limits to arbitrage)まで一次資料にある候補は、
上位5件では P8-C1 だけである。

### 2.2 必要なデータが全部ある(15/15)

2026-08-17 に本環境から実測して、必要な系列がすべて取得可能であることを確認した。

| 系列 | 経路 | 実測 |
|---|---|---|
| BTC spot OHLCV | `data/spot/monthly/klines/BTCUSDT/` | **2017-08 から取得可** |
| BTC perp OHLCV | `data/futures/um/monthly/klines/BTCUSDT/` | 取得可(既に保有) |
| funding rate | `data/futures/um/monthly/fundingRate/BTCUSDT/` | **2020-01 〜 2026-07 取得可** |
| index price | `data/futures/um/monthly/indexPriceKlines/BTCUSDT/` | 取得可 |
| premium index | `data/futures/um/monthly/premiumIndexKlines/BTCUSDT/` | 取得可(既に保有) |

しかも ango は `mce.binance_vision`(公開 CHECKSUM 検証つき・冪等・ledger 記録)を
**既に実装済み**であり、**対象パスを増やすだけ**で取り込める。
「データが無いので別の問題に置き換える」必要が一切無い唯一の候補である。

### 2.3 独立 confirmation 窓が実在する(13/15)

これが P8-C2 との決定的な差である。

```text
A1 の先行版 = BIS Working Paper No. 1087(2023-04)
  → サンプル終端は 2023年初以前と推定(★ H2 で要確認)

ango の research 開始 = 2023-11-19
  → ango が持つ非封印データは、ほぼ全てが A1 のサンプル外
```

対して **P8-C2 の論文サンプルは 2017-12 〜 2026-01 で、ango の非封印データを完全に覆う。**
P8-C2 には既存 split の中に独立窓が存在しない。

**減点2点の理由**: (a) A1 のサンプル終端が推定にとどまる(H2)、
(b) 本レビュー中に「BTC perp の funding carry は平均的に正で 2024年 > 2025年」という
**符号と大小関係**を知ってしまった(K1/K7)。したがって 2024–2025 は
**効果量の発見には使えず、機序の再現確認にのみ使える**(§8)。

### 2.4 エビデンス品質が最上位(10/10)

上位5件で**査読済みジャーナル掲載**は P8-C1(Management Science)と
P8-C3(International Review of Financial Analysis)のみ。
P8-C1 はさらに arXiv 6版の理論論文と BIS working paper に支えられている。
P8-C2 / C4 / C5 はすべて追試の無い preprint である。

### 2.5 Phase 7 closeout が要求した「問題設定の変更」に合致する

Phase 7 closeout §5 の結論は「変えるべきは3つ目の軸=問題設定」だった。

| | Phase 0–7 | P8-C1 |
|---|---|---|
| 予測対象 | 5分後の方向符号 | **方向を予測しない**(delta-neutral) |
| 収益源 | 価格変化の当て | **契約上の funding 授受 + basis の変化** |
| 回転率 | 高(5分グリッド) | **低**(8時間 funding 周期。保有は8h〜数日) |
| コストの効き方 | 往復10bps が地形を支配 | 往復コストは**保有期間で償却される** |

Phase 3 の Interpretation「コストの壁が探索空間全体を支配している」に対する
**直接の応答**になっている。5分足を捨てるのではなく、5分足を**観測と執行シミュレーション**に
使い、意思決定の周期を funding の周期へ移す。

---

## 3. 第1位を選ばなかった場合の代替案

| もし | 代替 | 条件 |
|---|---|---|
| H2 の結果、**A1 のサンプルが 2025年まで伸びていた**と判明したら | P8-C1 の独立 confirmation は 13 → 5 程度へ落ちる。それでも**データ可用性と機序の明快さで首位を維持する見込み**だが、GO 判定は prospective 窓(layer 3)のみに依存させる | H2 |
| H1 の結果、**SSRN 6993978 が実在し内容が確認できた**ら | P8-C7 のエビデンス品質 1 → 6〜8。合計 52〜54 となり**4位相当**へ上昇。ただし Bybit funding の取得経路が無いこと・venue 間の funding 間隔差は残る | H1 |
| **two-leg 執行モデルの実装が想定以上に重い**と判明したら | **P8-C2 へ切り替える**(ango の既存 engine で完結し、Phase 1A の abstention コードが再利用できる)。ただし独立窓が無いため、GO 判定は prospective 窓のみになる | 実装着手後の判断 |
| **spot データの品質ゲートが通らない**(欠測・分割・ティッカー変更等)場合 | perp のみで完結する **funding–premium 整合性の検証**(A2 の no-arbitrage 境界の片側)へ縮退する。**これは別問題への無言のすり替えではなく、縮退であることを明記して行う** | データ取り込み後 |

---

## 4. 原論文のどこまでを忠実再現するか(replication / extension の境界)

**この境界を先に固定することが、事後の「都合の良い読み替え」を防ぐ。**

### 4.1 忠実に再現する(replication)

| # | 再現対象 | 出所 |
|---|---|---|
| R1 | **carry / basis の定義そのもの**: perp と spot の価格差、および funding | A1 / A2 |
| R2 | **carry が大きく、時間変動が大きい**という記述的事実の独立確認 | A1 |
| R3 | **funding は perp–spot ギャップに比例して long が short へ支払う**という機構 | A2 |
| R4 | **取引コストを織り込んだ no-arbitrage 価格「帯」**という考え方(点推定ではなく帯) | A2 |
| R5 | **乖離は時間とともに縮小する**という主張の、ango の窓での符号確認 | A2 |

### 4.2 再現しない(データが無いので、無言で置き換えず明示的に範囲外とする)

| # | 再現しない対象 | 理由 |
|---|---|---|
| N1 | BTC + ETH の cross-section | ango は BTC 単独。ETH を足すことは Phase 8.1 の範囲外 |
| N2 | **dated futures**(限月先物)の carry | ango は dated futures データを持たない。**perp のみに限定することを明記する** |
| N3 | multi-venue(A1 が複数取引所を使っている場合) | ango は Binance 単独。**cross-venue は P8-C7 の課題であって本候補ではない** |
| N4 | 投資家層の識別(小口・トレンド追随投資家の需要) | 投資家粒度のデータが無い |
| N5 | 裁定資本の制約の**構造推定** | 同上 |

### 4.3 ango 独自の変更(extension。**replication の成否と混ぜて報告しない**)

| # | 独自変更 | 理由 |
|---|---|---|
| E1 | **個人研究者スケールの two-leg コスト後経済性**を primary endpoint にする | 原論文は carry の存在と経済的説明が主題であり、個人の執行コスト後収益は主題ではない |
| E2 | **decision-time observability の厳密化**: 決定時点で確定していない funding を使わない | ango の [data_contract §3](../data_contract.md) と P8-C4(B2)の観測可能性の区別を輸入 |
| E3 | **保有 horizon を探索対象として扱い、多重比較補正する** | Phase 7 の教訓(horizon 総当たりの最良値選択を禁じる) |
| E4 | **exposure 一致 random 対照**を必須の baseline に置く | Phase 1A の J3(net 改善が取引削減の機械的効果だった)の再発防止 |
| E5 | **always-on carry** を主要 baseline に置く | タイミング規則は「常時建てっぱなし」を上回らなければ意味が無い |

**報告規律**: R1–R5(replication)の結論と E1–E5(extension)の結論を
**別の節に分けて書く**。extension が失敗しても replication の成否は変わらない、
という関係を文書構造で担保する。

---

## 5. P8-C4 から輸入する設計要素(候補を採らずに知見だけ採る)

P8-C4(B2: Erez & Smirnov)の中心的な区別を、P8-C1 の設計へそのまま持ち込む。

```text
observable funding    : 建玉を決めた時点で、受け取る funding が既に確定している
forward-looking funding: 建玉後の premium サンプルから決まるので、決定時点では未知
```

**Binance USDT-M の funding は後者に近い**(次回 funding は当該インターバル中の
premium index から計算される)。したがって:

> **決定時点 t で「これから受け取る funding」を feature に使ってはならない。**
> 使ってよいのは **t 以前に決済が確定した funding** だけである。

これは ango の `data_contract §8`(OKX funding の as-of backward join、tolerance 9h)と
同じ規律であり、Phase 8.1 では **Binance について同等の availability 宣言を新規に書く**
(→ H4 の確定が前提)。

---

## 6. 必要なデータ取得(Phase 8.1 の最初の実作業)

**本タスクでは取得しない。** Phase 8.1 で以下を行う。

| # | データ | 取得元 | 新規実装 |
|---|---|---|---|
| D1 | BTC **spot** klines 5m(2020-01〜、封印継承で `ts < 2026-01-01`) | `data/spot/monthly/klines/BTCUSDT/5m/` | `mce.binance_vision` に spot パスを追加 |
| D2 | **funding rate**(月次、2020-01〜) | `data/futures/um/monthly/fundingRate/BTCUSDT/` | 正規化 + availability 宣言 |
| D3 | **index price** klines 5m | `data/futures/um/monthly/indexPriceKlines/BTCUSDT/5m/` | 正規化 |
| D4 | (保有済み)perp klines 5m / premium index 5m | — | 再取得のみ |

- **封印の継承**: Tier 0 と同じく `ts >= 2026-01-01` の行は正規化時に落とす
  ([tier0_ingest_v1 §5](../phase7/tier0_ingest_v1.md) と同じ規則)。
- **品質ゲート**: Tier 0 と同じ形式で機械判定し、`ts` グリッド・重複・欠測・
  **spot と perp の同時刻整合**(相対差の分布)を報告する。
- **ラベルは作らない。** 取得タスクの範囲は品質確認まで。

---

## 7. 独立 confirmation 設計

Phase 7 の dev → confirmation 構造を踏襲するが、**外部知識汚染の層を明示的に足す**。

```text
layer 1  literature_in_sample      ts < 2023-11-19
         → A1 のサンプルと重なる。再現性確認のみ。GO 判定に使わない

layer 2  contaminated_confirmation 2023-11-19 <= ts < 2026-01-01
         → A1 のサンプル外だが、本レビューで carry の符号と大小関係を知ってしまった
         → 機序の符号・形の再現確認に使う。効果量の発見には使わない

layer 3  prospective_final         事前登録 freeze 日より後に初めて生成されるバー
         → Phase 8 の GO / NO-GO をここで判定する
```

- layer 1 / layer 2 の境界は ango の既存 `RESEARCH_START` と一致させる(新しい境界を作らない)。
- **layer 3 は既存の `final_oos`(2026-01-01〜)の内側にある。**
  したがって **layer 3 を採用することは Final OOS firewall の改訂(freeze v2)に当たる。**
  これは**エージェントが決めてよい変更ではない**(→ H5)。
- **今回のタスクでは `mce.backtest.splits` を一切変更しない。**

---

## 8. prospective Final 設計(H5 が承認された場合の案)

```text
phase8_prospective_final_start = 2026-09-01T00:00Z(案)
```

- 既存の `final_oos`(2026-01-01)を**開封も移動もしない**。
- `2026-01-01 〜 2026-08-31` は「**既存封印域 かつ 外部知識汚染域**」として
  **二重に触らない**。
- Phase 8 の GO/NO-GO は `ts >= 2026-09-01` の prospective データでのみ判定する。
- 代償は**待ち時間**である。8時間 funding × 90日 = 270 回の funding 決済しか得られない。
  **この検出力の低さを事前に見積もり、MDE を事前登録に書く**(Phase 7 と同じ規律)。

**代替案(H5 で人間が選ぶ)**:

| 案 | 内容 | 長所 | 短所 |
|---|---|---|---|
| **P1** | 上記のとおり layer 3 を新設 | 汚染から独立した判定ができる | 待ち時間・検出力不足 |
| P2 | layer 2 のみで判定し、汚染を明記して弱い結論に留める | すぐ結論が出る | 「文献を読んだから選んだ」バイアスが判定に残る |
| P3 | 既存 `final_oos`(2026-01-01〜)を Phase 8 の最終判定に使う | サンプルが最大 | **封印を消費する。かつ 2026年は K3/K5/K7 で部分的に汚染済み** |

**推奨は P1。** ただし待ち時間の許容は研究者の時間予算の問題であり、人間が決める。

---

## 9. 概算実装工数

ango の既存資産を前提とした見積り。**実測ではなく見積りである。**

| # | 作業 | 見積り | 主なリスク |
|---|---|---|---|
| W1 | spot / funding / index price の取得・正規化・品質ゲート | **中**(既存 `binance_vision` の拡張) | funding の `calc_time` semantics(H4)。CSV の header 有無の世代差 |
| W2 | carry / basis の observable 化(availability 宣言つき) | 小 | as-of join の tolerance 設計 |
| W3 | **two-leg 執行モデル**(spot + perp、4レグ、funding 授受、hedge mismatch) | **大**(本候補の最大コスト) | 既存 engine が単一銘柄前提。新規モジュールになる |
| W4 | two-leg コストモデル(fee / spread / slippage × 2レグ + 資金拘束) | 中 | margin / collateral 項が既存 `costs.py` に無い |
| W5 | baseline 群(always-flat / buy-and-hold spot / **always-on carry** / exposure 一致 random) | 中 | random 対照の exposure 一致の定義 |
| W6 | 事前登録(horizon 探索の多重比較補正・effective N・GO/NO-GO・MDE) | 中 | Phase 7 の prereg 実装が雛形になる |
| W7 | 品質・整合テスト | 中 | — |

**W3 が律速。** ango の `mce/backtest/engine.py` は
「features → strategy → execution → cost → metrics」の単一銘柄パイプラインであり、
**two-leg は新しい実行器として書く**(既存 engine を壊さない)。

---

## 10. 失敗条件(Phase 8 を negative result として閉じる条件)

**結果を見てから決めない。以下を Phase 8.1 の事前登録で凍結する。**

| # | 条件 | 帰結 |
|---|---|---|
| F1 | layer 2 で **always-on carry の net が全 horizon・全コストシナリオで 0 以下** | 「個人スケールの two-leg コストでは carry を回収できない」として **economically negative** で閉じる |
| F2 | layer 2 で net > 0 だが、**exposure 一致 random 対照を上回らない** | Phase 1A の J3 と同じ機械的効果。**棄却** |
| F3 | **break-even cost が片道 5bps(`base_taker`)未満** | コスト水準の想定が少し外れただけで消える。**監視リスト止まり**(恒久ルール5) |
| F4 | layer 2 で符号が **A1/A2 の記述と逆**(carry が体系的に負) | replication 失敗として記録。extension は実行しない |
| F5 | spot / perp の品質ゲートが通らず、**修正が結果を見た後になる** | その時点で停止。ゲートを緩めない |
| F6 | layer 3 で **layer 2 の符号が再現しない** | Phase 7 の h=48 と同じ。**NO-GO**。閾値・horizon の入れ替えによる救済を禁じる |
| F7 | 事前登録した horizon 群のうち **1つだけが GO** で、他が全滅 | 多重比較補正後に生き残るかで判定。単独生存は **conditional hold** 止まり |

**F1〜F7 のいずれかで閉じた場合も、Phase 7 と同じく
「negative result を消さず、追記型で台帳に残す」。**

---

## 11. 人間が決定すべき事項(Phase 8.1 の freeze 前に必須)

| # | 事項 | 影響 | 優先 |
|---|---|---|---|
| **H2** | **Crypto Carry(A1)のサンプル期間の確定** | layer 1 / layer 2 の境界。独立 confirmation の成否 | **最高** |
| **H4** | Binance `fundingRate.calc_time` の semantics(決済時刻か計算時刻か) | availability 宣言。**間違えると leakage になる** | **最高** |
| **H5** | layer 3(prospective final)を設けるか。設けるなら日付と、Final OOS firewall 改訂(freeze v2)の可否 | GO/NO-GO の判定基盤。**firewall の改訂は人間の承認が要る** | **最高** |
| **H6** | spot leg の執行前提(現物買い切りか信用か。証拠金・担保ヘアカット・借入金利) | コストモデルの中身 | 高 |
| H1 | SSRN 6993978 の存在確認 | P8-C7 の再評価 | 中 |
| H3 | Crypto factor zoo のサンプル期間・銘柄数 | P8-C3 を上位に上げる場合のみ | 低 |
| H7 | Binance Vision / 取引所 ToS 上の利用可否(日本居住者) | 全候補共通。継続して**要確認** | 中 |
| H8 | *Alpha Illusion*(F2)の P1–P6 を ango の報告規準として採用するか | 報告規律 | 中(**採用を推奨**) |
| H9 | 対象を BTC 単独に限るか、ETH を足すか | 範囲。**足すなら replication ではなく extension として明記が必要** | 中 |

---

## 12. このメモが言っていないこと

1. **「carry が儲かる」とは言っていない。** 損益を1つも計算していない。
2. **「P8-C2 の論文が悪い」とは言っていない。** 内部規律はむしろ上位である。
   順位差の主因は **ango 側に独立窓が無いこと**である。
3. **「cross-venue に価値が無い」とは言っていない。** 一次資料に到達できなかった、
   という **ango 側の制約**である(H1)。
4. **「P8-C1 が成功する」とは予測していない。** §10 の失敗条件を先に書いたのは、
   失敗したときに事後の言い訳をしないためである。
5. **本メモは事前登録ではない。** horizon・閾値・GO 条件の具体値は
   Phase 8.1 の事前登録で、**結果を見ない状態で**凍結する。

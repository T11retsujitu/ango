# Research Backlog

- 作成日: 2026-08-16(Phase 3 closeout 時)
- 目的: 「今やらないが捨てない」研究項目の一元管理。Phase 3 の negative result により
  **保留になった項目を削除しないため**の台帳。
- 状態語: `active`(現在の主軸) / `hold`(条件付き保留・再評価条件を明記) /
  `blocked`(前提データ・手段が無い) / `parked`(優先度低)

再評価のトリガーは必ず**条件として書く**。「そのうちやる」は書かない。

---

## A. Information-space research(現在の主軸)

| ID | 項目 | 状態 | 備考 |
|---|---|---|---|
| I0 | Tier 0 データ取り込み・observable 化・品質確認 | **完了(2026-08-16)** | [tier0_ingest_v1](phase7/tier0_ingest_v1.md)。ラベル未閲覧 |
| I0b | Tier 0 screening の事前登録 | **完了・凍結(2026-08-16)** | [tier0_screening_preregistration_v1](phase7/tier0_screening_preregistration_v1.md) / `src/mce/tier0_prereg.py`。効果量を見ない状態で確定 |
| I1 | Tier 0-A 集約 aggressive flow の incremental information test(T0-A) | **active(次は実行)** | family 27 test の一部。h = 1/3/12 バー |
| I2 | Tier 0-B derivatives state(T0-B1 = OI / T0-B2 = positioning) | active(同上) | B2 は 2022 の欠測ブロックを避けて dev 窓 2023- |
| I3 | Tier 0-C basis(perp−index premium) | active(同上) | premiumIndexKlines。funding は Tier 0 では未使用 |
| I4 | Tier 1-A aggTrades event-level(signed volume・large-trade imbalance・burst・flow persistence・単位フローあたり価格応答) | hold | **再開条件**: I1 で incremental information が確認され、機序を event 水準で詰める必要が生じたとき |
| I5 | Tier 1-B bookDepth スナップショット(距離別 depth) | hold | **再開条件**: I1/I2 通過後、liquidity state による条件付けが必要になったとき。先に公式仕様の確認 |
| I6 | Tier 2 OKX prospective microstructure(M1 L1 OFI / M2 板枯れ / M3 吸収) | active(収集中) | 事前凍結済み。`T0` から Calibration 60日 + Validation 60日。**定義変更禁止** |
| I7 | liquidation(清算)データ | **blocked** | Binance Vision の該当パスは 2026-08-16 時点 404、OKX 未実装。**再開条件**: 取得手段と履歴深さの確認 |
| I8 | 遡及 L1 tick(bookTicker) | parked | 199 MB/日 ≒ 33ヶ月で 200 GB。**再開条件**: Tier 0/1 で L1 機序の存在が示唆されたとき |
| I9 | cross-venue(lead-lag、mid 乖離、flow divergence、liquidity migration) | parked | **再開条件**: 単一 venue の microstructure 機序が1つ以上確立したとき |
| I10 | options IV / skew / term structure、on-chain、macro、news、social | parked | 5分以下では timestamp・latency・licensing・情報利用可能時刻の問題が大きい |
| I11 | event-level 情報の `available_time` 物理列導入(data contract §3 の予告) | hold | **再開条件**: 公開遅延の異なる系列を observable へ昇格させるとき |

---

## B. Literature replication(役割を変更して継続)

Phase 1 のような「論文 strategy をそのまま増やす」ことは主目的にしない。
先行研究からは次を抽出し、**Ango のデータと Judge で独立に検証する**。

```text
information set → observable → mechanism → target → prediction horizon
→ execution assumption → reported validation method → known failure mode
```

再現目標は「論文で儲かったこと」ではなく、
**その論文が主張する information mechanism が Ango のデータでも存在するか**。

| ID | テーマ | 主な参照 | 優先 | 状態 |
|---|---|---|---|---|
| L1 | trade / order-flow imbalance、taker/maker 分離、adverse selection | R11 Explainable Patterns in Cryptocurrency Microstructure | **高** | I1/I4 と直結 |
| L2 | market impact / 単位フローあたりの価格応答 | R11 ほか(要追加調査) | 高 | I4 |
| L3 | absorption / exhaustion | 既存 M3 の設計元 + 要追加調査 | 高 | I6 と重複しない形で |
| L4 | L1 / L2 imbalance、microprice | 要追加調査 | 中 | I5/I6 |
| L5 | liquidity state と impact の条件付け | 要追加調査 | 中 | I5 |
| L6 | open interest / liquidation / positioning | 要追加調査 | 中 | I2、I7 が blocked のため部分的 |
| L7 | funding / basis と継続・反転 | R? + 既存 33ヶ月 funding 統計 | 中 | I3 |
| L8 | cross-venue price discovery | 要追加調査 | 低 | I9 |
| L9 | clock / periodicity(既検証) | R9 Quarter-Hour Effect | 完了 | 方向性なし・活動構造ありで台帳化済み |
| L10 | cost-aware abstention(既検証) | R10 BTC Trading Under Transaction Costs | 完了 | 棄却済み |

規律:

- literature 由来 hypothesis と Ango 独自 hypothesis を**明示的に区別**して記録する。
- 一次論文・公式データ仕様への reference(arXiv ID / DOI / 公式 docs URL)を必ず保存する。
- 著者報告の performance は独立再現まで信用値として扱わない(ROADMAP R4 の注意)。

---

## C. Search-algorithm research(保留。削除しない)

保留理由(Phase 3 の結果に基づく研究 ROI 判断であり、否定ではない):

> Phase 3 では search algorithm を3通りに変えても validation survivor は
> 観測されなかったため、**同じ OHLCV DSL v1 空間**で searcher tuning を続ける
> 研究 ROI が現時点で低い。

**共通の再開条件**:

```text
information-space screening で incremental information が確認され、
Market Microstructure DSL v2(新しい探索空間)が凍結された時点
```

その時点で新しい bakeoff を設計する:

```text
Information-space screening → promising mechanisms → frozen DSL v2
→ Random baseline → LLM Semantic Search → MCTS / evolutionary / memory-based
```

| ID | 項目 | 状態 | Phase 3 で得た入力 |
|---|---|---|---|
| S1 | improved Genetic Search(population 拡大、fitness sharing、重複ペナルティ、mutation の非局所化) | hold | duplicate 64/102・数個体への崩壊を観測 |
| S2 | trajectory-level mutation / crossover(QuantaAlpha) | hold | AST-level 版は実装済み |
| S3 | LLM + failure memory(FactorMiner) | hold | 「long-only drift-fit は validation で死ぬ」等の種を Arm A/B が記録済み |
| S4 | LLM + positive memory | hold | Phase 3 では positive が0件のため素材が無い |
| S5 | semantic duplicate avoidance / diversity 制御 | hold | LLM は duplicate 0・31 family を達成しており優先度は相対的に低い |
| S6 | grammar-aware MCTS(AlphaCFG) | hold | 探索空間が広がってから最も価値が出る |
| S7 | Bayesian / adaptive search、budget 配分の最適化 | hold | — |
| S8 | evaluation budget の大幅増加(30 → 数百) | hold | **単独では実行しない**(同じ空間の掘り直しになる) |
| S9 | LLM masking の有効性検証(HindsightBench 型 4-arm) | hold | Arm C の未検証点。DSL v2 bakeoff の設計時に同梱すると安価 |

Phase 3 で確認された **LLM の研究資産**(DSL v2 でそのまま再利用する):

- valid candidate generation(拒否 2/32)
- constraint following(凍結メニュー・param 上限の理解)
- semantic diversity(消費32提案で31 family)
- low duplicate rate(0/32)
- plan translator(semantic → AST)・閾値量子化・masking 仕様・feedback firewall・transcript 記録

---

## D. Judge / infrastructure

| ID | 項目 | 状態 | 備考 |
|---|---|---|---|
| J1 | Phase 5 statistical audit(DSR / PBO / SPA / block bootstrap / regime consistency) | hold | **再開条件**: 大量探索や候補 selection を再開した時点。Phase 3 は survivor 0 のため実行対象が無い |
| J2 | Phase 6 sealed Final OOS | 封印継続 | 開封条件は「Research/Validation の探索が完全終了」 |
| J3 | DSL v1 の表現力拡張(ヒステリシス、状態機械、相互作用項) | hold | Phase 3 の代替仮説の1つ。**DSL v2 設計時に合流させる**(v1 は凍結保存) |
| J4 | target 設計の見直し(方向符号以外) | active | Phase 7 protocol §5 に統合済み |
| J5 | 執行モデルの horizon 整合性検証(cost と signal horizon の不整合) | hold | Phase 3 の代替仮説の1つ。**再開条件**: Phase 7 で有望 horizon が特定されたとき |
| J6 | regime 差(research 窓のブル偏り)への対処 | hold | walk-forward 化や split 再設計は **freeze v2** を要する。安易に変更しない |

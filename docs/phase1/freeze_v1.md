# Phase 1 Freeze v1 — Judge 凍結宣言

- 凍結日: 2026-08-16
- 根拠: Phase 0 Exit Criteria 完了(実データ検証済み、EXP-0001〜0006)+
  Phase 1A / 1B の完遂により「Judge が現実的な研究を評価できる」ことを確認
  (ROADMAP「Phase 1 Freeze」)。

## 凍結対象(Phase 2 以降の searcher — Random / Genetic / LLM — は変更できない)

| 対象 | 定義場所 | 凍結内容 |
|---|---|---|
| **Data Contract** | `docs/data_contract.md`, `mce/features.py`(AVAILABILITY), `mce/labels.py` | バー ts=開始時刻・availability 宣言・observable/label 物理分離・`fwd_` 命名規約・rolling 規約(closed="left"/完全窓)・ts一致join |
| **Execution Rule** | `mce/backtest/execution.py` | signal at close[t] → fill at open[t+1]。欠損時は次の存在バー open、遅延 > 30分でキャンセル。同一バー close 執行は構造的に不可能 |
| **Cost Rule** | `mce/backtest/costs.py` | 成分分離(fee/spread/slippage、片道bps)。シナリオ: zero / maker_low(1) / base_taker(5) / stress(10)。funding は PnL 外(感応度分析のみ) |
| **Split Rule** | `mce/backtest/splits.py`, `mce/backtest/data.py` | research: 2023-11-19〜2025-07-01 / validation: 〜2026-01-01 / final_oos: 2026-01-01〜上限なし(封印、sealed API のみ)。loader guard(fwd_拒否・availability検査) |
| **Metric Implementation** | `mce/backtest/metrics.py` | 非複利・固定notional。BARS_PER_YEAR = 105,120。全指標の定義と None 規約 |

## 運用規則

1. **searcher は上記を読み取り専用として扱う。** strategy は
   「observable features → target ∈ {-1,0,+1}」の写像のみを供給できる。
2. 凍結対象の変更が必要になった場合は、人間の判断で **freeze v2** を新規作成し、
   v1 下の実験結果と v2 下の結果を**比較不能**として扱う(サイレント変更の禁止)。
   テストスイート(167件)が契約の実質的な監視役である。
3. **validation split** は Phase 3 の search loop(Research ⇄ Validation)で使用を
   解禁する。**final_oos は Phase 6 まで封印を継続**し、結果を search loop・memory へ
   還流しない(ROADMAP §4.2)。
4. 実験記録は追記専用(`experiments/`)。凍結時点のデータ指紋は
   `data/manifests/*.json` を参照。

## Phase 1 の結果(要約)

- **Phase 1A** — cost-aware abstention: **棄却**(J3。net改善は取引削減の機械的効果。
  副産物: per-bar閾値の断片化問題、高閾値尾部グロス+4.3bps/trade は監視リスト)
  → [findings](../findings/2026-08-16-phase1a-cost-abstention-v1.md)
- **Phase 1B** — clock phase: **方向性なし・活動構造あり**(境界バーのボラ跳ねと
  境界直前の静穏化は cross-exchange で確認。方向効果は最大0.24bpsの帰無)
  → [findings](../findings/2026-08-16-phase1b-clock-phase-v1.md)

## 次フェーズ

Phase 2 — Semantic Schema + DSL + AST(ROADMAP §Phase 2)。
LLM に Python を書かせず、`Semantic Hypothesis → Schema → DSL → AST → Compiler` の
経路のみを許可する。DSL 設計への既知の入力:

- ヒステリシス付き閾値(enter/exit 分離)または最小保有 — Phase 1A の断片化問題より
- Context 候補としての clock activity 構造(境界ボラ・:55静穏)— Phase 1B より
- 複雑度制約の初期値: max_ast_depth 5 / max_features 4 / max_parameters 6 /
  max_holding_bars 48(ROADMAP §2.3)

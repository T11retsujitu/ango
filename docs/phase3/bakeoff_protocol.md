# Phase 3 — Alpha Search Bakeoff プロトコル v1(共通部+Arm A 凍結)

- 凍結日: 2026-08-16(Arm A 実行前に確定)
- 実装: `src/mce/search/`
- 対象: ROADMAP §Phase 3。DSL は [dsl_spec v1(凍結済み)](../phase2/dsl_spec.md)、
  Judge は [freeze_v1(凍結済み)](../phase1/freeze_v1.md)。
- 本版で凍結する範囲: **共通評価パイプライン・budget 会計・選抜規則・Arm A(Random)**。
  Arm B(Genetic)/ Arm C(LLM)/ Baselines の各論は、それぞれの実行前に
  本文書へ**追記**して凍結する(共通部の変更は不可)。

## 1. Budget(凍結)

```text
Arm A  Random     30 evaluations   seed = 20260818
Arm B  Genetic    30 evaluations   (実行前に追記凍結)
Arm C  LLM        30 evaluations   (実行前に追記凍結)
Baselines         10 evaluations   (固定 baseline を同一パイプラインで評価)
------------------------------------------------
Total            100
```

- **1 evaluation = 有効・非重複 candidate 1つの research split 評価**。
  validator 棄却・duplicate は budget を消費しない(が全て記録される)。
- **seed の回し直し禁止**: 各 arm の公式 run は1回。seed を変えた再実行は
  「バグ修正時のみ・findings に理由を記録して」許される。
- 総 draw 数の上限 = budget × 60(サンプラ異常のガード)。

## 2. 共通評価パイプライン(凍結)

candidate(AST)ごとに:

```text
draw → validator(拒否→rejected) → ast_hash dedupe(重複→duplicate)
     → compile → research split で backtest(例外→runtime_failure)
     → research 通過判定 → 通過者のみ validation split で backtest
     → validation 生存判定
```

- コスト: **primary = base_taker(片道5bps)**。selection は primary のみで行う。
  secondary = maker_low(片道1bps)を参考記録(選抜に使わない)。
- 執行: 凍結済み ExecutionConfig(AST の max_holding_bars を反映)。
- validation split はこのパイプラインでのみ使用する(freeze_v1 §運用規則3)。
  final_oos は封印継続。

## 3. 選抜規則(凍結)

- **research 通過**: trade_count ≥ 30 ∧ total_return > 0 ∧ sharpe > 0(primary コスト)
- **validation 生存**: trade_count ≥ 10 ∧ total_return > 0 ∧ sharpe > 0(primary コスト)
- turnover 過多の明示規則は置かない(taker コストでの net > 0 が実質的に処罰する)。
- レジーム一貫性・DSR/PBO は Phase 5 の統計監査で扱う(本段階の生存は
  「暫定生存」であり Final OOS 生存ではない)。

## 4. Arm A — Random Grammar Search(凍結)

役割: **LLM が本当に必要かを判断する最低 baseline**(ROADMAP Arm A)。

制御付き一様サンプリング。パラメータメニュー(`mce/search/grammar.py` に定数として凍結):

| 項目 | メニュー |
|---|---|
| window | 3, 6, 12, 24, 48, 96, 144, 288 |
| threshold: return / trend | ±{0.0005, 0.001, 0.002, 0.005} |
| threshold: ma_slope | ±{0.0001, 0.0002, 0.0005, 0.001} |
| threshold: volatility | {0.0005, 0.001, 0.002, 0.005} |
| threshold: range | {0.002, 0.005, 0.01, 0.02} |
| threshold: volume_z / zscore | ±{0.5, 1.0, 2.0, 3.0} / ±{0.5, 1.0, 2.0} |
| holds_for bars | 2, 3, 6, 12 |
| max_holding_bars | 6, 12, 24, 48(出現確率 0.5) |

構造サンプリング(確率は grammar.py の実装が正): 形式 = long のみ 0.25 /
short のみ 0.25 / 両方 0.5。bool 木は比較 0.45 / clock_is 0.10 / and·or 0.20 /
not 0.10 / holds_for 0.15(深さ残 2 以下では比較 0.85 / clock 0.15)。
feature は 6 op から一様、確率 0.2 で zscore / rolling_mean transform を挟む。
abstain_unless は確率 0.25(深さ ≤ 3)。制約違反サンプルは validator が拒否し
rejected として記録(budget 非消費)。

## 5. 記録(ROADMAP §4.5 準拠)

- `experiments/phase3/<arm>_seed<seed>/candidates.jsonl` — 全 draw の追記専用記録
  (status: rejected / duplicate / runtime_failure / evaluated、AST、hash、
  research / validation metrics)
- `summary.json` — counters: candidate_count / unique_candidate_count /
  duplicate_count / rejected_candidate_count / runtime_failure_count /
  evaluated_count / research_pass_count / validation_count / survivor_count、
  survivors 一覧、config、data manifest sha256、source commit
- 「一番良かった Sharpe だけ報告」の禁止: summary は全 counter と全 survivor を
  機械的に含む。

## 6. 事前予想(2026-08-16 記録)

1. 30 evaluations の獲得に要する draw は 40〜90(param 制約の拒否率 2〜5割)
2. research 通過(taker で net>0・30 trades)は **0〜3 / 30**。
   低 turnover 型(holds_for / max_holding / abstain 付き)に偏る
3. validation 生存は **0 が最頻**(1 出たら Phase 5 監査行きの暫定生存)
4. maker_low 参考値では research 通過が 3〜8 に増える(コストの壁の実測)

## 7. 実行手順

```sh
uv run python -m mce.search.random_search          # budget 30, seed 20260818(凍結値)
git add experiments/phase3 && git commit
```

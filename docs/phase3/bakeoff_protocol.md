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

→ **結果(2026-08-16 確定)**: draw 46 / rejected 16 / research通過 4 / 生存 **0**。
[findings](../findings/2026-08-16-phase3-armA-random-v1.md) 参照。Arm A は凍結済み。

## 7. 実行手順

```sh
uv run python -m mce.search.random_search          # Arm A(実行済み・凍結)
uv run python -m mce.search.genetic                # Arm B(budget 30, seed 20260819)
uv run python -m mce.search.baselines_arm          # Baselines(10固定AST)
git add experiments/phase3 && git commit
```

---

## 8. Arm B — Genetic Search(追記凍結 2026-08-16。Arm B 実行前)

役割: 進化的探索が Random に対して budget 効率で優るかの検証(ROADMAP Arm B)。
QuantaAlpha の trajectory-level 操作は採用せず、初期版は **AST-level operation**。

- **seed = 20260819 / budget 30**(research 評価 30 回。共通規則 §1–§3 は不変)
- 初期集団: 凍結 grammar(§4)から population = **6** 個体をサンプル
- 世代ループ(budget 尽きるまで): 子を1個体ずつ生成・評価し、
  親∪子から fitness 上位 6(hash 重複は除く)を次集団とする(steady-state + elitism)
- 選択: tournament size **3**
- 遺伝操作: crossover 確率 **0.4**(親2の条件スロット移植:
  child[slot_A] ← parent_B[slot_B])、それ以外は mutation(1箇所):
  param 摂動 0.5(凍結メニュー内の隣接値。符号付き閾値は 0.2 で符号反転)/
  比較演算子反転 0.15 / bool 部分木の再サンプル 0.25 / max_holding 付替え 0.1
- 無効・重複の子は budget を消費しない(会計は共通 ledger)。有効な子が
  20 回試して得られない場合は grammar から新規サンプルで代替(多様性注入)
- **fitness(凍結・research primary のみ使用)**:
  `fitness = sharpe(None→−99) − 0.02 × ノード数 − (2.0 if trade_count < 30)`
  complexity penalty と実効N penalty を含む。
  **validation の値は fitness・選択に一切使わない**(記録のみ。§2 の防火壁維持)
- duplicate の子は評価済み record の fitness を再利用(budget 非消費)

### Arm B 事前予想(2026-08-16 記録)

1. GA は Arm A が示した long-only drift-fit を選択圧で増幅し、research 通過は
   **4〜10 / 30** に増える(research 適合は Random より上手くなる)
2. しかし validation 生存は **0 が最頻**(防火壁は選択圧では破れない)
3. duplicate は 3〜15(収束による)。世代後半は同族 AST が支配的
4. survivors/evaluations で Random(0/30)と差がつかない可能性が最も高い

## 9. Baselines arm(追記凍結 2026-08-16)

固定 10 戦略を **DSL AST として凍結**し(`search/baselines_arm.py` の
FROZEN_BASELINES が正)、同一パイプラインで評価する。探索ではなく参照点:

1. always_long(return(1) > −1)
2. always_short(return(1) < 1)
3. momentum_1h(return(12) の符号で long/short)
4. momentum_4h(return(48))
5. momentum_1d(return(288))
6. reversal_1h(momentum_1h の逆)
7. momentum_1h_persist(holds_for 3 本)
8. momentum_1h_hold12(max_holding_bars 12)
9. lowvol_momentum_long(long側のみ: return(12)>0 ∧ volatility(48)<0.002。
   両側条件は max_parameters 6 を超えるため long 側に限定)
10. volume_shock_fade(volume_z(24) > 3 で short)

注意: always_long/short は決済がほぼ発生せず trade_count < 30 のため、
共通規則上 research を通過できない(buy&hold の参照値は EXP-0001 が既に保持)。
これは規則の歪みではなく「規則が hold 戦略を審査対象外とする」ことの明示的記録。

### Baselines 事前予想

research 通過 0〜1 / 10(lowvol_momentum か momentum_1h_hold12 が僅かに可能性)、
生存 0。momentum 系は Phase 0 実測(gross 負)どおり大敗する。

→ **結果(2026-08-16 確定)**: Arm B は draw 102 / duplicate 64 / research通過 **0** / 生存 0。
Baselines は 10 評価すべて research 不通過。
[findings](../findings/2026-08-16-phase3-armB-genetic-baselines-v1.md) 参照。両 arm とも凍結済み。

---

## 10. Arm C — LLM Semantic Search(追記凍結 2026-08-16。Arm C 実行前)

役割: 研究の主質問 — **semantic prior を持つ LLM は Random / Genetic より効率よく
OOS 生存戦略を発見するか**。共通規則(§1–§3)は不変。budget 30。

### 10.1 LLM は何を出力するか(ROADMAP Arm C 準拠)

**LLM は Python も AST も書かない。** 出力は semantic schema の仮説レコード +
`dsl_plan`(構造化された意味レベルの計画)のみで、**deterministic translator**
(`mce/search/plan.py`)が AST へ変換し、既存 validator/compiler/Evaluator を通す。
JSON Schema による structured output で語彙・windowを列挙拘束する。

```json
{
  "hypothesis_id": "H007", "event": "...", "context": ["..."], "quality": ["..."],
  "direction": "...", "action": "...", "hypothesis": "...", "expected_failure_mode": "...",
  "dsl_plan": {
    "signal_family": "clock_conditioned_momentum", "side": "long|short|both",
    "entry": {"feature": "return", "window": 12, "op": "greater", "threshold": 0.002},
    "filters": [{"feature": "volatility", "window": 48, "op": "less", "threshold": 0.002}],
    "clock": {"period": 15, "phase": 0}, "persistence_bars": 3, "holding_bars": 12
  }
}
```

翻訳規則(凍結): entry を比較ノード化 → `persistence_bars` があれば `holds_for` で包む
→ filters と clock を AND → `side="both"` なら short 側は **entry のみ反転**
(op 反転・閾値符号反転)し filters/clock は共通 → `holding_bars` は
`max_holding_bars` へ。制約違反は validator が rejected(budget 非消費)。

**`side="both"` のコスト(凍結 validator の性質)**: 凍結 validator はパラメータを
**出現ごと**に数えるため、`both` は条件木を長短へ複製してコストが倍になる
(entry + filter 1つ + both = 8 params → 棄却)。Arm A/B の grammar も両側を独立に
サンプルするので規則は同一。この性質は LLM プロンプトに明記して無駄な提案を防ぐ。

**探索空間の同一性(重要)**: LLM の閾値は translator が **凍結メニュー
(`grammar.THRESHOLD_MENUS`)の最近傍へ量子化**する。window/holding/persistence/phase は
JSON Schema の enum でメニューに拘束。これにより Arm A/B と**同一の探索空間**を保つ
(LLM だけが連続値を使える不公平を排除)。生値と量子化後の両方を記録する。

### 10.2 Masking(HindsightBench / Profit Mirage / Temporal Leakage 対応)

LLM の parametric hindsight を抑制するため、プロンプトから以下を除去する:

| 実体 | プロンプト上の表現 |
|---|---|
| BTC-USDT-SWAP | `ASSET_X`(a perpetual futures contract on a major digital asset) |
| OKX | 記載しない |
| 暦日付・期間 | bar index のみ(`bars 0..N`)。年・月・イベント名を一切書かない |
| 価格水準 | 記載しない |

マスキング仕様は artifact に記録する。これは
「cutoff より後だから安全」に依存しない設計(R15)であり、LLM が
「2024年のBTCは上昇相場」といった記憶を使えないようにする構造的措置。

### 10.3 Feedback loop(firewall 維持)

各ラウンドで、**research primary metrics のみ**を匿名化して LLM に返す:
`hypothesis_id / signal_family / trades / net_bps_per_trade / sharpe / turnover / 判定`。
**validation の値・final_oos の一切を返さない**(freeze_v1 §運用規則、ROADMAP §4.2)。
棄却理由は機械的ラベル(`below_min_trades` / `net_negative` / `rejected_by_validator` /
`duplicate`)のみ。

### 10.4 決定性についての正直な扱い(重要)

**LLM 呼び出しは決定的ではない。** temperature は Claude Opus 5 以降で
**API から削除されており**(送ると 400)、seed も存在しない。したがって Arm C は
Arm A/B のような bit 再現性を持たない。代わりに:

- 全リクエスト/レスポンスを `llm_transcript.jsonl` へ記録(model ID・prompt sha256 込み)
- **replay mode**(`--replay <dir>`)で記録済み plan を再評価 → 評価側は完全決定的
- artifact に `deterministic: false` と `replayable: true` を明記

この非決定性は結果解釈時の留保事項として findings に必ず記載する。

### 10.5 実行パラメータ(凍結)

| 項目 | 値 |
|---|---|
| model | `claude-opus-5`(artifact に記録。変更時は別 run として記録) |
| budget | 30 evaluations(共通規則どおり rejected/duplicate は非消費) |
| 1リクエストあたり提案数 | 6 |
| 最大API呼び出し回数 | 12(暴走ガード) |
| max_tokens | 16000 |
| temperature | **設定しない**(API から削除済み) |
| structured output | `output_config.format`(json_schema) |
| refusal | `stop_reason == "refusal"` を content 読み取り前に検査し、記録して次ラウンドへ |

### 10.6 Arm C 事前予想(2026-08-16 記録)

1. LLM の valid candidate rate は Random より高い(制約を理解するため)。
   rejected は 30〜60% ではなく 10〜30% 程度
2. duplicate は Genetic(64)より少なく、Random(0)より多い(5〜20)。
   semantic family の再訪が起きる
3. **research 通過は 0〜4**。Random(4)と同程度で、統計的に区別できない
4. **validation 生存は 0 が最頻**(コスト支配の地形は semantic prior では変わらない)
5. 総合: **survivors/evaluations で Random/Genetic と有意差なし → ROADMAP §9.3 により
   「LLM を alpha searcher として使う仮説」は棄却または保留**が最有力
6. ただし diversity(semantic family 数)と valid rate では LLM が優位に立つ可能性が高く、
   それは「探索効率」ではなく「探索の意味的整理」の価値として別途記録する

### 10.7 実行手順

```sh
export ANTHROPIC_API_KEY=...   # または ant auth login
uv run python -m mce.search.llm_search              # 公式run(1回)
uv run python -m mce.search.llm_search --replay experiments/phase3/llm_<model>  # 再評価
```

→ **結果(2026-08-16 確定)**: draw 32 / rejected 2 / duplicate 0 / evaluated 30 /
research通過 **0** / 生存 **0**(API 呼び出し6・提案36・semantic family 31・
refusal 0・plan error 0・runtime failure 0)。
[findings](../findings/2026-08-16-phase3-armC-llm-v1.md) 参照。Arm C は凍結済み。

---

## 11. Bakeoff 完了(2026-08-16)

4 arm(Random / Genetic / LLM / Baselines)すべて実行済み・凍結済み。
主指標 **validation survivors / evaluations は 0/30・0/30・0/30・0/10**。
cross-arm 集計は artifact から機械的に再生成できる:

```sh
uv run python -m mce.phase3_summary --json experiments/phase3/bakeoff_summary.json
```

総括と研究判断: [Phase 3 bakeoff summary](../findings/2026-08-16-phase3-bakeoff-summary-v1.md)。
本プロトコル(共通部・各 arm)はここで凍結完了とし、**再実行・seed 変更・
閾値変更・criteria 緩和を行わない**。新しい情報集合を探索対象にする場合は
Market Microstructure DSL v2 + 新プロトコルとして別途凍結する
([Phase 7](../phase7/information_space_expansion_v1.md))。

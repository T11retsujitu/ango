# BTC Alpha Discovery Research Roadmap
## LLM Quant Researcher × Deterministic Backtesting

- **作成日**: 2026-08-16
- **対象**: BTCを中心とする暗号資産の自動取引・alpha discovery
- **想定実装環境**: Python / ローカルPC / Claude Code
- **主データ**: BTC 5分足 OHLCV（既存データを起点）
- **目的**: 「LLMに売買させる」のではなく、「LLMに検証可能な取引仮説を探索させる」研究基盤を構築する
- **本ドキュメントの用途**: Claude Codeへ開発を依頼する際の設計・ロードマップ・参考文献の共通仕様書

---

# 1. Executive Summary

本研究の主目的は、LLMにBTCのBUY/SELLを直接判断させることではない。

研究課題を以下とする。

> **限られたバックテスト評価回数の下で、semantic priorを持つLLM Research Agentは、Random SearchやEvolutionary Searchよりも、BTC市場でOut-of-Sampleに生存する取引仮説を効率的に発見できるか。**

> **研究軸の更新(2026-08-16 追記。以下の本文は当初設計として保存する)**
>
> Phase 3 Alpha Search Bakeoff が完了し、Random / Genetic / LLM の3 arm すべてで
> validation survivor は **0 / 30** だった([bakeoff summary](docs/findings/2026-08-16-phase3-bakeoff-summary-v1.md))。
> これを受けて主研究軸を、
>
> ```text
> Which search algorithm finds alpha in OHLCV?
>        ↓
> Which information set contains incremental, cost-relevant information beyond OHLCV?
> ```
>
> へ段階的に移す(Phase 7 — Information-Space Expansion)。
> これは **searcher research の破棄ではなく優先順位の変更**であり、保留項目は
> [research backlog](docs/research_backlog.md) に条件付きで残す。中心原則:
>
> > **Do not assume the searcher is the bottleneck. Test the information space
> > before optimizing the searcher further.**

したがって、システムを以下の2層に分離する。

1. **Researcher**
   - 仮説生成
   - semantic search
   - strategy/factor候補生成
   - 過去の成功・失敗から次の候補を提案

2. **Judge**
   - データ分割
   - feature計算
   - 約定
   - transaction cost
   - backtest
   - statistical audit
   - Final OOS

ResearcherはJudgeのルールを書き換えられない。

最重要原則は以下。

> **Build the judge before the strategy search.**

LLM探索を開始する前に、決定論的な評価器を完成・検証・freezeする。

---

# 2. Research Question

## 2.1 Primary Question

同一の候補評価budgetを与えた場合、以下の探索法を比較する。

1. LLM-guided semantic search
2. Random grammar search
3. Genetic / evolutionary search
4. 将来的に MCTS

評価対象は「最高Sharpe」ではない。

主要な比較指標は以下。

- Final OOS survival rate
- evaluations per surviving strategy
- cost-adjusted Sharpe
- Deflated Sharpe Ratio
- Probability of Backtest Overfitting
- regime consistency
- break-even transaction cost
- strategy diversity
- duplicate rate

---

## 2.2 Secondary Questions

### Market hypothesis

BTCには以下のような、単純な5分足方向予測より構造的なalpha候補が存在するか。

- clock phase / quarter-hour
- cost-aware abstention
- multi-horizon trend
- volatility / regime conditioning
- volume anomaly
- short-term reversal
- funding / OI / liquidation state
- order-flow imbalance
- liquidity state

### Meta-research hypothesis

LLMの強みは、数値予測そのものではなく、

- 仮説空間の意味的整理
- mechanism-based hypothesis generation
- 過去のfailureの抽象化
- 重複探索の削減

に存在するか。

---

# 3. Non-Goals

初期PoCでは以下を行わない。

## 3.1 LLM Direct Trader

以下のような構成を主研究対象にしない。

```text
Market Data
    ↓
LLM
    ↓
BUY / SELL
```

理由:

- LLMのparametric hindsight / temporal leakage
- numerical calibrationが弱い
- transaction cost / execution semanticsが曖昧になりやすい
- alpha attributionが困難
- backtest結果がLLMの市場知識に汚染されうる

---

## 3.2 Tick / Full L2から開始しない

データの拡張順序は原則として以下。

```text
5m OHLCV
    ↓
aggTrades / signed volume
    ↓
L1
    ↓
L2
    ↓
queue / maker fill simulation
```

最初からL2 simulatorを作らない。

---

## 3.3 RL Executionから開始しない

signalの存在が確認できていない状態でRL executionを導入しない。

---

## 3.4 LLM Fine-Tuningから開始しない

まずはexternal memoryとprompt-level research agentでLLM searchの価値を確認する。

---

# 4. Core Design Principles

## 4.1 Deterministic Evaluation

LLMは以下を変更できない。

- train / validation / test split
- Final OOS
- transaction cost model
- execution model
- statistical test
- baseline definition
- feature availability rule

---

## 4.2 Final OOS Firewall

研究loop:

```text
Research
    ↓
Validation
    ↺
Research
```

と、

```text
Final OOS
```

を完全分離する。

Final OOSについて以下をResearch Agentへ返してはならない。

- raw data
- return
- Sharpe
- failure reason
- regime result
- trade result

Final OOS結果をmemoryへ保存して次の探索に利用することも禁止する。

---

## 4.3 Time Integrity

すべてのfeatureに最低限以下の概念を持たせる。

```text
reference_time
available_time
```

基本ルール:

```text
feature_time <= signal_time < execution_time
```

5分足の標準約定:

```text
features = bars through close[t]
signal   = after close[t]
fill     = open[t+1]
```

以下は禁止。

```text
signal uses close[t]
fill = close[t]
```

---

## 4.4 Cost First

gross returnより先にnet returnを見る。

初期PoCでも最低限以下を分離する。

- exchange fee
- spread proxy
- slippage proxy
- funding（該当時）
- turnover

market impactは初期段階では感応度分析でもよい。

各strategyについて、

- base cost
- low-cost scenario
- stress cost
- break-even cost

を計算する。

---

## 4.5 Search Budget Must Be Logged

探索回数を必ず記録する。

禁止:

> 1,000個試して一番良かったSharpeだけを報告する

保存するもの:

- candidate count
- unique candidate count
- rejected candidate count
- duplicate count
- runtime failure count
- validation count
- survivor count

---

# 5. Target Architecture

```text
                   Research Literature
                          │
                          ▼
                Mechanism Knowledge Base
                          │
                          ▼
                Semantic Research Planner
                          │
                          ▼
                Falsifiable Hypothesis
                          │
                          ▼
      Event × Context × Quality × Direction × Action
                          │
                          ▼
                         DSL
                          │
                          ▼
                         AST
                          │
                          ▼
                Static Validation / Audit
                          │
                          ▼
                Deterministic Compiler
                          │
                          ▼
                Deterministic Backtester
                          │
                          ▼
                   Research / Validation
                          │
              ┌───────────┴───────────┐
              ▼                       ▼
        Positive Memory          Failure Memory
              └───────────┬───────────┘
                          │
                          ▼
                   Next Hypothesis


==================== FIREWALL ====================


                      Sealed OOS
                          │
                          ▼
                   Final Evaluation
```

---

# 6. Roadmap

# Phase 0 — Deterministic Judge

## Goal

LLMを一切使わず、信用できるbacktest/evaluation基盤を構築する。

## Required Components

```text
data/
features/
backtest/
audit/
experiments/
```

### Data

- BTC 5m OHLCV loader
- timestamp validation
- missing bar detection
- duplicate detection
- timezone = UTC
- immutable raw data
- processed data manifest

### Feature engine

最初は少数に限定。

- return
- rolling return
- realized volatility
- range
- ATR
- volume z-score
- moving-average slope
- clock phase

### Execution

- signal at close[t]
- execute at open[t+1]
- position state
- holding period
- entry/exit
- long / short / flat

### Cost

- maker/taker config
- spread proxy
- slippage
- break-even cost

### Metrics

- return
- annualized return
- Sharpe
- Sortino
- Maximum Drawdown
- turnover
- trade count
- hit rate
- profit factor
- exposure
- break-even cost

## Exit Criteria

Phase 0を完了する条件:

- future barを書き換えるテストで過去signalが変わらない
- cost=0とcost>0で期待通りPnLが変化
- executionを1bar遅らせると結果が変化
- baseline strategiesが再現可能
- random seed固定で同一結果
- unit testが通る

---

# Phase 1 — Evaluator Validation

目的はalpha discoveryではない。

> **Judgeが現実的な研究を評価できるか**

を確認する。

2つの既存研究アイデアを使う。

---

## Phase 1A — Cost-Aware Abstention

参考:

> Machine Learning-Based Bitcoin Trading Under Transaction Costs: Evidence From Walk-Forward Forecasting

元論文はBTC-USDTの**1時間足**であり、5分足実験は厳密な論文再現ではない。

以下の扱いとする。

> **method transfer / replication-inspired experiment**

### Hypothesis

予測方向そのものに弱いedgeしかなくても、

```text
expected edge > estimated trading cost
```

の場合のみ取引することで、turnoverとnet performanceが改善するか。

### Minimal implementation

最初はDeep Learning不要。

- Logistic Regression
- XGBoost

のどちらか、または両方。

### Baselines

- always flat
- buy & hold
- naive sign
- momentum
- random abstention

### Failure

cost-aware ruleが

- turnoverを減らさない
- net resultを改善しない
- validation foldで一貫しない

なら棄却。

---

## Phase 1B — Clock Phase / Quarter-Hour

参考:

> The Quarter-Hour Effect: Periodic Algorithmic Trading and Return Predictability in Cryptocurrency Futures

### Step 1

既存5分足だけでscreening。

features:

```text
minute_mod_15
minute_mod_60
hour_utc
weekday
volatility_regime
volume_regime
```

### Step 2

時計位相ごとに、

- return
- volatility
- volume
- strategy expectancy

を比較する。

### Placebo

必ず以下を比較する。

- actual phase
- shifted phase
- random phase

例:

```text
00/15/30/45
01/16/31/46
02/17/32/47
...
```

5分足では表現可能なphase単位に合わせる。

### Decision

5分足で構造が見える場合のみaggTradesへ進む。

---

# Phase 1 Freeze

Phase 1終了後、

- data contract
- execution rule
- cost rule
- split rule
- metric implementation

をfreezeする。

Phase 2以降のsearcherはこれを変更できない。

---

# Phase 2 — Semantic Schema + DSL + AST

## Goal

LLMにPython strategy codeを自由生成させない。

以下の順序を採用する。

```text
Semantic Hypothesis
       ↓
Schema Plan
       ↓
DSL
       ↓
AST
       ↓
Compiler
```

---

## 2.1 Semantic Schema

AlphaSchemaを参考に、初期空間を以下とする。

### Event

- momentum
- reversal
- volatility shock
- volume shock
- breakout
- clock boundary

### Context

- high volatility
- low volatility
- trend
- range
- high volume
- low volume

### Quality

- persistence
- acceleration
- exhaustion
- divergence
- confirmation

### Direction

- continuation
- reversal

### Action

- long
- short
- flat
- abstain
- exit

---

## 2.2 DSL

初期operatorは意図的に狭くする。

### Raw / derived features

```text
return(window)
trend(window)
volatility(window)
range(window)
volume_z(window)
ma_slope(window)
clock_phase(period)
```

### Transform

```text
rolling_mean(x, window)
rolling_std(x, window)
zscore(x, window)
greater(x, threshold)
less(x, threshold)
and(a, b)
or(a, b)
not(a)
```

### Position logic

```text
long_if(condition)
short_if(condition)
flat_if(condition)
abstain_unless(condition)
```

---

## 2.3 Constraints

初期値例:

```yaml
max_ast_depth: 5
max_features: 4
max_parameters: 6
max_holding_bars: 48
```

禁止:

- negative lag
- arbitrary Python
- arbitrary file access
- dynamic imports
- network access
- access to Final OOS

---

# Phase 3 — Alpha Search Bakeoff

## Research Question

同一evaluation budgetで、どの探索アルゴリズムが最も効率よくOOS-surviving strategyを発見するか。

---

## Search Arms

### Arm A — Random Grammar Search

DSL grammarから一様または制御random sampling。

役割:

> LLMが本当に必要なのかを判断する最低baseline。

---

### Arm B — Genetic Search

- mutation
- crossover
- tournament / rank selection
- complexity penalty
- duplicate rejection

QuantaAlphaのtrajectory-level mutation/crossoverは参考にするが、初期版ではAST-level genetic operationでよい。

---

### Arm C — LLM Semantic Search

LLMはcodeを書かない。

LLM output example:

```json
{
  "hypothesis_id": "H017",
  "event": "clock_boundary",
  "context": ["moderate_volatility"],
  "quality": ["persistence"],
  "direction": "continuation",
  "action": "abstain_unless",
  "hypothesis": "Short-horizon continuation becomes more tradable near quarter-hour boundaries when volatility is moderate.",
  "dsl_plan": {
    "signal_family": "clock_conditioned_momentum",
    "holding_bars": 3
  },
  "expected_failure_mode": "transaction_cost"
}
```

その後、別のdeterministic componentがDSL/ASTとしてvalidateする。

---

## Initial Budget

PoCでは:

```text
Random     30
Genetic    30
LLM        30
Baselines  10
----------------
Total     100
```

MCTSは最初のPoCには入れない。

---

## Phase 3 実行結果(2026-08-16 確定。上記の設計記述は変更しない)

プロトコル: [docs/phase3/bakeoff_protocol.md](docs/phase3/bakeoff_protocol.md)(全 arm 実行前に凍結)。
一次記録: `experiments/phase3/`。総括: [bakeoff summary](docs/findings/2026-08-16-phase3-bakeoff-summary-v1.md)。

| arm | draw | rejected | duplicate | evaluated | research_pass | validation_survivor |
|---|---:|---:|---:|---:|---:|---:|
| Random(A) | 46 | 16 | 0 | 30 | 4 | **0** |
| Genetic(B) | 102 | 8 | 64 | 30 | 0 | **0** |
| LLM(C) | 32 | 2 | 0 | 30 | 0 | **0** |
| Baselines | 10 | 0 | 0 | 10 | 0 | **0** |

**Result**: 現在の探索budget(30 evaluations/arm)と凍結済み OHLCV DSL v1 の範囲では、
どの searcher も validation survivor を発見できなかった。Final OOS は未開封。

**言えないこと**: 「LLM と Random/Genetic が同等」「OHLCV に alpha が無い」
「information set が唯一のボトルネック」。0/30 の片側95%上限は 9.5% であり、
差の検出力はこの設計に無い(詳細と代替仮説は bakeoff summary §6)。

**Definition of Success(§18)との対応**: Case C/D の軌道(OHLCV survivor 不在・
コスト支配)。ただし Case B(LLM ≈ Random)を主張するには検出力が不足しているため、
**LLM の alpha searcher 仮説は棄却ではなく保留**とする(ROADMAP §9.3)。
LLM の valid rate / duplicate rate / semantic diversity の優位は研究資産として残す。

**副次資産**: 100評価分の budget 会計・台帳・cross-arm 集計ユーティリティ
(`python -m mce.phase3_summary`)。

---

# Phase 4 — Memory and Search Improvements

> **状態: 条件付き保留(2026-08-16 追記。以下の設計は削除せず保存する)**
>
> Phase 3 で LLM の alpha searcher としての優位は survival endpoint では示されなかった。
> 同一の OHLCV DSL v1 空間で memory / searcher を改良する研究 ROI は現時点で低いため、
> 本 Phase は **hold** とする。再開条件:
>
> ```text
> information-space screening で incremental information が確認され、
> Market Microstructure DSL v2 が凍結された時点
> ```
>
> 個別項目の状態は [research backlog §C](docs/research_backlog.md) を参照。

Phase 3でLLMに価値がある場合のみ導入する。

---

## 4.1 Failure Memory

FactorMinerを参考に、単なるscoreではなくfailure mechanismを保存する。

例:

```json
{
  "mechanism": "short_term_momentum",
  "context": "high_volatility",
  "failure": "turnover_too_high",
  "evidence": "fails_after_10bp_roundtrip",
  "scope": "research_validation",
  "confidence": 0.91
}
```

LLMにFinal OOS由来のfailureを渡さない。

---

## 4.2 Positive Memory

例:

```json
{
  "mechanism": "clock_conditioned_abstention",
  "observation": "lower_turnover_with_stable_validation_expectancy",
  "scope": "research_validation"
}
```

---

## 4.3 Duplicate Control

以下でduplicateを管理する。

- AST exact hash
- normalized AST
- structural distance
- output correlation
- semantic family

AlphaAgent / QuantaAlphaの考え方を参考にする。

---

# Phase 5 — Statistical Audit

大量strategy探索を開始した時点で必須。

> **状態(2026-08-16 追記)**: Phase 3 の validation survivor が 0 のため、
> 現時点で DSR / PBO / SPA を適用する対象候補が存在しない。**削除ではなく待機**であり、
> 「大量探索や候補 selection を再開した時点で必須」という条件はそのまま維持する
> (Phase 7 の information-space screening でも multiple testing は §Phase 7 の
> 規則で扱う)。survivor を作るために判定基準を緩めることは禁止。

---

## 5.1 Deflated Sharpe Ratio

selection bias、non-normality、multiple trialsによるSharpe inflationを監査。

---

## 5.2 Probability of Backtest Overfitting

strategy selectionによるoverfit probabilityを推定。

---

## 5.3 Reality Check / SPA

候補群全体を試した結果、偶然benchmarkを超えるstrategyが出るdata-snooping問題を扱う。

---

## 5.4 Block Bootstrap

BTC returnのserial dependenceを考慮する。

---

## 5.5 Regime Consistency

最低限:

- bull
- bear
- high volatility
- low volatility

で分解する。

---

# Phase 6 — Sealed Final OOS

Research / Validationで探索が完全終了してから一度だけ実行。

Final OOS結果はsearch loopへ戻さない。

## Final Metrics

Primary:

- cost-adjusted Sharpe
- Final OOS survival
- DSR
- PBO
- break-even cost
- regime consistency
- turnover

Meta-search comparison:

- evaluations / survivor
- duplicate rate
- semantic diversity
- valid candidate rate

---

# Phase 7 — Information-Space Expansion

> 旧題: **Data Expansion Decision**。Phase 3 完了(survivor 0)により
> **Branch B が発火**したため、本 Phase を「次にどの情報集合を検証するか」を
> 体系的に扱う主研究軸へ発展させる(2026-08-16)。当初の Branch A/B/C 設計は
> 下に原文のまま残す。
>
> 詳細プロトコル: [docs/phase7/information_space_expansion_v1.md](docs/phase7/information_space_expansion_v1.md)
> 既存資産のレビュー: [docs/phase7/microstructure_v1_review.md](docs/phase7/microstructure_v1_review.md)

## 7.0 Research Question

> **5分足 OHLCV を baseline としたとき、どの追加情報群が、OHLCV だけでは説明できず、
> かつ取引コスト上意味のある incremental predictive information を持つか。**

```text
H0: information set X は OHLCV baseline を超える有用な incremental information を含まない
H1: X は再現可能な incremental information を含む
```

成功条件を「profitable strategy が見つかった」に**しない**。
まず「OHLCV baseline だけでは説明できない追加情報が存在するか」を測る。

```text
OHLCV baseline → OHLCV + X → incremental information test
→ mechanism validation → cost relevance → OOS validation → retain / reject
```

## 7.1 情報空間の優先順位(2026-08-16 のデータ可用性実測を反映)

素朴な「trades → L1 → L2」順ではなく、**expected research value / data cost** で並べる。
OKX の遡及は funding 約3ヶ月・OI 約5日・板/約定は遡及不可であり、
「derivatives は L2 より履歴検証しやすい」という一般論はこの repository では成立しない。

| Tier | 情報集合 | 主な observable | 取得コスト(実測) |
|---|---|---|---|
| **0** | bar 集約 aggressive flow / derivatives state / basis | taker buy share、約定件数、OI・ΔOI・OI z、long/short ratio、perp−index premium、funding | 合計 ~30 KB/日(Binance Vision、2021〜) |
| **1** | aggTrades / bookDepth | signed volume、large-trade imbalance、flow persistence、burst、距離別 depth | 5–8 MB/日 / 0.55 MB/日 |
| **2** | OKX prospective microstructure(trades / BBO / 400段 L2) | L1 OFI、10bps 板枯れ、吸収 | 収集機構は実装済み。**遡及不可** |
| **3** | 遡及 L1 tick / cross-venue / liquidation / options・on-chain・macro | — | 200 GB 規模、または取得手段が未確定 |

既存の [Microstructure v1](docs/findings/2026-08-16-microstructure-v1-protocol.md)
(M1 = L1 OFI / M2 = 10bps 板枯れ / M3 = aggressive-flow 吸収)は Tier 2 の最初の3仮説として
**そのまま包含**する。事前凍結済みのため、結果を見る前に定義を変更しない。

## 7.2 Target を方向符号に限定しない

`future_return_sign` だけで情報集合の価値を判定しない。候補 target:
return magnitude / volatility / tail move / range expansion / adverse move after entry /
(microstructure 到達後は)spread・depth・impact・adverse selection。

最終的に trade / abstain・maker / taker・執行タイミング・サイジングへ接続しうるかを評価するが、
**本 Phase では execution optimizer / RL / maker queue simulator を新規実装しない**。

## 7.3 Incremental information test の骨子

```text
Model A: OHLCV baseline      → target
Model B: OHLCV baseline + X  → target   (同一 split・同一 target・同一 estimator)
```

- capacity 差を information value と誤認しない(A/B は同一 estimator family、
  さらに X をブロックシャッフルした placebo 対照を必ず置く)
- feature timestamp を厳密に管理し、未来情報を使わない
- 同時刻の機械的関係と将来予測力を分離する
- multiple testing(情報集合 × target × horizon)を family として記録し Holm 補正
- 効果量を必ず報告し、bps/取引へ変換して執行コストと比較する
- **screening は `ts < 2026-01-01` のみ**(別 venue のデータでも Final OOS 期間を封印継承)

## 7.4 Literature の役割変更

論文 strategy をそのまま増やすことを主目的にしない。先行研究からは
`information set → observable → mechanism → target → horizon → execution assumption →
validation method → known failure mode` を抽出し、
**その機序が Ango のデータでも存在するか**を独立に検証する
([research backlog §B](docs/research_backlog.md))。

## 7.5 将来の DSL v2 と search bakeoff

incremental value が確認された機序に限り、

```text
Information-space screening → promising mechanisms → frozen Market Microstructure DSL v2
→ Random baseline → LLM Semantic Search → MCTS / evolutionary / memory-based search
→ statistical audit → sealed Final OOS
```

という新しい bakeoff を設計する。**その時点で search algorithm research を再開する**
([research backlog §C](docs/research_backlog.md))。OHLCV DSL v1 は Phase 3 の探索空間定義として
凍結保存し、結果を見た後に operator / threshold を変更して Phase 3 を再 run しない。

---

## 当初の分岐設計(2026-08-16 実行前の原文。歴史記録として保存)

## Branch A — OHLCVでsurvivorあり

以下を進める。

- DSL refinement
- larger budget
- failure memory
- MCTS
- funding / OI interaction

---

## Branch B — OHLCVでsurvivorなし

OHLCV-only searchを延々拡大しない。

次は:

```text
aggTrades
```

へ進む。

追加features:

- signed volume imbalance
- buy/sell notional imbalance
- trade-count imbalance
- large-trade imbalance
- flow persistence

---

## Branch C — aggTradesでも弱い

L1/L2へ進む。

ただし、

> OFIが単体で効く

ではなく、

> OFI × liquidity state

として検証する。

---

# 7. Proposed Repository Structure

```text
research/
├── README.md
├── ROADMAP.md
│
├── data/
│   ├── raw/
│   ├── processed/
│   ├── manifests/
│   └── loaders/
│
├── features/
│   ├── returns.py
│   ├── trend.py
│   ├── volatility.py
│   ├── volume.py
│   └── clock.py
│
├── dsl/
│   ├── grammar.py
│   ├── ast.py
│   ├── operators.py
│   ├── validator.py
│   └── compiler.py
│
├── backtest/
│   ├── engine.py
│   ├── execution.py
│   ├── costs.py
│   ├── positions.py
│   └── metrics.py
│
├── search/
│   ├── random_search.py
│   ├── genetic_search.py
│   ├── llm_researcher.py
│   └── common.py
│
├── memory/
│   ├── positive.jsonl
│   ├── negative.jsonl
│   ├── hypotheses.jsonl
│   └── lineage.jsonl
│
├── audit/
│   ├── temporal_integrity.py
│   ├── duplicates.py
│   ├── block_bootstrap.py
│   ├── dsr.py
│   ├── pbo.py
│   ├── reality_check.py
│   ├── spa.py
│   └── regime.py
│
├── experiments/
│   ├── phase1_cost_abstention/
│   ├── phase1_clock_phase/
│   └── phase3_search_bakeoff/
│
├── tests/
│   ├── test_temporal_integrity.py
│   ├── test_execution.py
│   ├── test_costs.py
│   ├── test_dsl.py
│   └── test_backtest_determinism.py
│
└── sealed_oos/
    ├── README_DO_NOT_EXPOSE_TO_SEARCHER.md
    └── evaluator.py
```

---

# 8. Experiment Artifact Specification

すべての実験について設定と結果を機械可読で保存する。

例:

```yaml
experiment_id: EXP-0001
created_at: 2026-08-16

data:
  symbol: BTCUSDT
  timeframe: 5m
  timezone: UTC

signal:
  feature_cutoff: close_t
  execution: open_t_plus_1

cost:
  fee_bps: 5
  spread_bps: 2
  slippage_bps: 3

split:
  research: ...
  validation: ...
  final_oos: sealed

search:
  method: fixed
  budget: 1

audit:
  block_bootstrap: true
  dsr: true
  pbo: false
```

各runについて以下を保存する。

- input config
- dataset manifest hash
- source commit hash
- random seed
- candidate AST
- hypothesis
- raw metrics
- cost assumptions
- validation result
- failure reason
- runtime
- model name（LLM使用時）
- prompt hash

---

# 9. Failure Criteria

## 9.1 Strategy Failure

候補strategyについて以下のいずれかを満たす場合はreject候補。

- net expectancy <= 0
- realistic cost > break-even cost
- validation Sharpe <= 0
- trade count insufficient
- turnover excessively high
- performance concentrated in a single narrow regime
- structurally duplicate

---

## 9.2 OHLCV Search-Space Failure

Search methodを問わず、

- robust survivorがほぼ存在しない
- realistic costを超えない
- statistical audit後に全候補が消える

場合:

> OHLCV-only search spaceを深掘りせず、aggTradesへ進む。

---

## 9.3 LLM Search Hypothesis Failure

LLM searchがRandom / Geneticに対して、

- survival rate
- evaluations / survivor
- strategy diversity
- duplicate rate

のいずれにも優位性を示さない場合:

> LLMをalpha searcherとして使う仮説を棄却または保留する。

この場合でもLLMは、

- literature summarization
- mechanism extraction
- experiment documentation

に限定して利用可能。

---

# 10. Implementation Priority

## P0 — Must Have

- deterministic BTC 5m backtester
- next-bar execution
- transaction cost model
- data split
- experiment artifact
- temporal integrity tests
- fixed baseline strategies

## P1

- cost-aware abstention experiment
- clock-phase experiment
- walk-forward evaluation
- break-even cost

## P2

- DSL / AST
- Random Search
- Genetic Search
- LLM semantic search

## P3

- DSR
- PBO
- block bootstrap
- SPA / Reality Check
- regime audit

## P4

- positive/negative memory
- semantic duplicate control
- MCTS

## P5

- aggTrades
- OFI
- funding / OI / liquidation
- L1 / L2

---

# 11. Key References

以下は2026-08-16時点で本ロードマップへ直接影響を与えた主要研究。

---

## R1. AQuA: Recursively Self-Improving Quantitative Trading Research Agents

- **Authors**: Jiacheng Guo, Suozhi Huang, Yunlong Gao, Zihao Li, Jian Ge, Xu Kuang, Mengdi Wang
- **Year**: 2026
- **arXiv**: 2608.12841v1
- **Submitted**: 2026-08-13
- **Source**: https://arxiv.org/abs/2608.12841
- **DOI**: https://doi.org/10.48550/arXiv.2608.12841

### Relevance

最重要な設計参考の一つ。

採用する概念:

- recursive research loop
- validated evidenceを次のproposalへ利用
- sealed sandbox
- fixed data splits
- fixed features / labels
- fixed evaluator
- agent actionをconstrained expression/config diffへ制限

### Do not blindly copy

論文のheadline performanceを本システムの期待値として扱わない。

---

## R2. AlphaSchema: Exploring the Space of Trading Semantics for LLM-Based Alpha Mining

- **Authors**: Jingyang Yi, Jian Yang, Yifei Jin, Yuqi Li, Jian Li
- **Year**: 2026
- **arXiv**: 2607.26642v1
- **Submitted**: 2026-07-29
- **Source**: https://arxiv.org/abs/2607.26642
- **DOI**: https://doi.org/10.48550/arXiv.2607.26642

### Relevance

LLMにfactorをいきなり生成させる前に、

```text
Event × Context × Qualities × Direction × Output
```

というsemantic search spaceを定義する考えを採用。

### Adopt

- semantic planning
- search-space coverage
- exploration / exploitation separation

---

## R3. Hubble: An LLM-Driven Agentic Framework for Safe, Diverse, and Reproducible Alpha Factor Discovery

- **Authors**: Runze Shi, Shengyu Yan, Yuecheng Cai, Chengxi Lv
- **Year**: 2026
- **arXiv**: 2604.09601v2
- **Latest revision**: 2026-04-14
- **Source**: https://arxiv.org/abs/2604.09601
- **DOI**: https://doi.org/10.48550/arXiv.2604.09601

### Relevance

採用する概念:

- domain-specific operator language
- AST sandbox
- deterministic evaluation
- positive / negative RAG
- formula similarity penalty
- persistent diagnostics

本ロードマップの

```text
LLM → DSL → AST → deterministic evaluator
```

の主要出典。

---

## R4. From Hypotheses to Factors: Constrained LLM Agents in Cryptocurrency Markets

- **Authors**: Yikuan Huang, Zheqi Fan, Kaiqi Hu, Yifan Ye
- **Year**: 2026
- **arXiv**: 2604.26747v1
- **Submitted**: 2026-04-29
- **Source**: https://arxiv.org/abs/2604.26747
- **DOI**: https://doi.org/10.48550/arXiv.2604.26747

### Relevance

暗号資産で直接検証された、今回の目的に最も近い研究の一つ。

採用:

- sequential hypothesis search
- append-only experiment trace
- falsifiable hypothesis
- point-in-time factor DSL
- deterministic engine
- success/failure trace

### Important caution

著者報告のperformanceは本プロジェクトで独立再現するまで信用値として扱わない。

---

## R5. FactorMiner: A Self-Evolving Agent with Skills and Experience Memory for Financial Alpha Discovery

- **Authors**: Yanlong Wang, Jian Xu, Hongkang Zhang, Shao-Lun Huang, Danny Dongning Sun, Xiao-Ping Zhang
- **Year**: 2026
- **arXiv**: 2602.14670v1
- **Submitted**: 2026-02-16
- **Source**: https://arxiv.org/abs/2602.14670
- **DOI**: https://doi.org/10.48550/arXiv.2602.14670

### Relevance

failure memoryの主要参考。

採用:

- successful pattern memory
- failure constraint memory
- retrieve → generate → evaluate → distill loop
- redundant searchの削減

---

## R6. QuantaAlpha: An Evolutionary Framework for LLM-Driven Alpha Mining

- **Authors**: Jun Han et al.
- **Year**: 2026
- **arXiv**: 2602.07085v3
- **Latest revision**: 2026-05-18
- **Paper**: https://arxiv.org/abs/2602.07085
- **HTML v3**: https://arxiv.org/html/2602.07085v3
- **Official repository**: https://github.com/QuantaAlpha/QuantaAlpha

### Relevance

採用:

- diversified initialization
- mutation
- crossover
- trajectory lineage
- semantic consistency
- complexity control
- redundancy control

### Important

古いversionのheadline数値とv3の数値を混同しない。

本プロジェクトでは**v3**を基準文献とする。

---

## R7. AlphaAgent: LLM-Driven Alpha Mining with Regularized Exploration to Counteract Alpha Decay

- **Authors**: Ziyi Tang, Zechuan Chen, Jiarui Yang, Jiayao Mai, Yongsen Zheng, Keze Wang, Jinrui Chen, Liang Lin
- **Year**: 2025
- **Accepted**: KDD 2025
- **arXiv**: 2502.16789
- **Source**: https://arxiv.org/abs/2502.16789

### Relevance

採用:

- AST similarity / originality
- hypothesis-factor alignment
- complexity control
- duplicate / crowding prevention

---

## R8. Alpha Discovery via Grammar-Guided Learning and Search (AlphaCFG)

- **Authors**: Han Yang, Dong Hao, Zhuohan Wang, Qi Shi, Xingtong Li
- **Year**: 2026
- **arXiv**: 2601.22119v1
- **Submitted**: 2026-01-29
- **Source**: https://arxiv.org/abs/2601.22119

### Relevance

将来的なMCTS armの主要参考。

採用:

- context-free grammar
- bounded symbolic search space
- grammar-aware MCTS

---

# 12. Crypto / Market-Structure References

## R9. The Quarter-Hour Effect: Periodic Algorithmic Trading and Return Predictability in Cryptocurrency Futures

- **Authors**: Chan Kim, Peter Reinhard Hansen
- **Year**: 2026
- **arXiv**: 2607.09426v2
- **Latest revision**: 2026-07-16
- **Source**: https://arxiv.org/abs/2607.09426
- **DOI**: https://doi.org/10.48550/arXiv.2607.09426

### Relevance

BTCを含むBinance perpetualで、

- one-minute
- five-minute
- quarter-hour

境界の周期構造を分析。

採用:

- clock-phase feature
- placebo phase
- order-flowを4–12h horizonまで見る発想

### Do not copy

quarter-hourで無条件売買するstrategy。

---

## R10. Machine Learning-Based Bitcoin Trading Under Transaction Costs: Evidence From Walk-Forward Forecasting

- **Authors**: Andrei Bysik, Robert Ślepaczuk
- **Year**: 2026
- **arXiv**: 2606.00060v1
- **Submitted**: 2026-05-19
- **Source**: https://arxiv.org/abs/2606.00060
- **DOI**: https://doi.org/10.48550/arXiv.2606.00060

### Relevance

約70,000時間のBTC-USDTと27-fold walk-forwardを使用。

本プロジェクトで採用するのはmodelそのものより、

> forecast → trade conversion

の考え。

特に:

> predicted edgeがcostを超えない場合はtradeしない

というcost-aware abstention。

### Important

元論文は1時間足。

BTC 5分足実験は**exact replicationではなくmethod transfer**として扱う。

---

## R11. Explainable Patterns in Cryptocurrency Microstructure

- **Authors**: Bartosz Bieganowski, Robert Ślepaczuk
- **Year**: 2026
- **arXiv**: 2602.00776v1
- **Submitted**: 2026-01-31
- **Source**: https://arxiv.org/abs/2602.00776
- **DOI**: https://doi.org/10.48550/arXiv.2602.00776

### Relevance

将来aggTrades / L1 / L2へ移行するときの主要参考。

採用:

- OFI
- spread
- adverse selection
- taker / maker separation
- temporal cross validation
- flash-crash robustness

初期PoCには直接実装しない。

---

# 13. LLM Backtest Audit References

## R12. The Alpha Illusion: Reported Alpha from LLM Trading Agents Should Not Be Treated as Deployment Evidence

- **Authors**: Yuxuan Ye et al.
- **Year**: 2026
- **arXiv**: 2605.16895v1
- **Submitted**: 2026-05-16
- **Source**: https://arxiv.org/abs/2605.16895

### Relevance

LLM tradingのbacktest alphaを実運用evidenceとして扱うことへの警告。

本研究ではLLM direct traderを優先しない根拠の一つ。

---

## R13. Profit Mirage: Revisiting Information Leakage in LLM-based Financial Agents

- **Authors**: Xiangyu Li, Yawen Zeng, Xiaofen Xing, Jin Xu, Xiangmin Xu
- **Year**: 2025
- **arXiv**: 2510.07920v1
- **Source**: https://arxiv.org/abs/2510.07920

### Relevance

historical financial agent evaluationにおけるLLM knowledge leakageを扱う。

LLMのmarket-data OOSとmodel-memory OOSを区別する理由。

---

## R14. HindsightBench: A Black-Box Behavioral Audit Protocol for Parametric Hindsight in Time-Indexed LLM Decision Tasks

- **Author**: Haozhe Jia
- **Year**: 2026
- **arXiv**: 2607.18867
- **Source**: https://arxiv.org/abs/2607.18867

### Relevance

date informationによるparametric hindsightを検査。

参考となる4-arm構造:

- revealed
- date-only
- masked
- transplanted / wrong-date

本研究でLLM Research Agentを評価するとき、

- BTCUSDT → ASSET_X
- calendar date → bar index
- named historical events → remove

のようなmaskingを検討する。

---

## R15. Temporal Leakage in LLM Backtesting: Measurement, Validation, and Adjusted Scores

- **Authors**: Zeyu Zhang, Bradly C. Stadie
- **Year**: 2026
- **arXiv**: 2608.02985
- **Submitted**: 2026-08-04
- **Source**: https://arxiv.org/abs/2608.02985

### Relevance

単純な

```text
before model cutoff vs after model cutoff
```

比較だけでは、recencyとleakageを分離できないことを指摘。

本研究では、

> 「cutoffより後だから安全」

だけでLLM-memory OOSを保証しない。

---

# 14. Statistical Audit References

## R16. The Deflated Sharpe Ratio: Correcting for Selection Bias, Backtest Overfitting and Non-Normality

- **Authors**: David H. Bailey, Marcos López de Prado
- **Year**: 2014
- **SSRN**: https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2460551

### Relevance

多数のstrategyを試した後のSharpe inflationを補正する主要手法。

---

## R17. The Probability of Backtest Overfitting

- **Authors**: David H. Bailey, Jonathan M. Borwein, Marcos López de Prado, Qiji Jim Zhu
- **SSRN**: https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2326253

### Relevance

strategy selectionによるbacktest overfittingを評価する。

---

## R18. A Reality Check for Data Snooping

- **Author**: Halbert White
- **Journal**: Econometrica, 68(5), 2000
- **DOI**: https://doi.org/10.1111/1468-0262.00152

### Relevance

大量の候補から偶然winnerが出るdata snoopingへの基礎手法。

---

## R19. A Test for Superior Predictive Ability

- **Author**: Peter Reinhard Hansen
- **Journal**: Journal of Business & Economic Statistics, 23(4), 2005
- **DOI**: https://doi.org/10.1198/073500105000000063

### Relevance

White Reality Checkを発展させたSPA test。

---

# 15. Research-to-Implementation Mapping

| Research | Adopt | Phase |
|---|---|---|
| AQuA | sealed sandbox / fixed evaluator / recursive evidence | 0–4 |
| AlphaSchema | semantic hypothesis space | 2–3 |
| Hubble | DSL / AST / deterministic evaluation | 2 |
| From Hypotheses to Factors | sequential hypothesis + append-only trace | 2–4 |
| FactorMiner | positive/negative experience memory | 4 |
| QuantaAlpha | mutation / crossover / lineage | 3–4 |
| AlphaAgent | originality / complexity / duplicate control | 3–4 |
| AlphaCFG | grammar-aware MCTS | later |
| Quarter-Hour Effect | clock phase market hypothesis | 1 |
| BTC Trading Under Transaction Costs | abstention / cost-aware execution | 1 |
| Crypto Microstructure | future OFI/L1/L2 expansion | 7 |
| Alpha Illusion | deployment-claim caution | all |
| Profit Mirage | LLM leakage caution | 3–6 |
| HindsightBench | model-memory audit | 3–6 |
| Temporal Leakage | cutoff audit caution | 3–6 |
| DSR / PBO / Reality Check / SPA | multiple-testing audit | 5–6 |

---

# 16. Recommended First Development Order for Claude Code

Claude Codeには最初から全システムを作らせない。

実装順序:

```text
1. Repository inspection / requirements
2. Data contract
3. Deterministic backtester
4. Cost model
5. Temporal integrity tests
6. Fixed baselines
7. Phase 1A Cost-Aware Abstention
8. Phase 1B Clock-Phase
9. Freeze evaluator
10. Semantic Schema
11. DSL / AST / compiler
12. Random search
13. Genetic search
14. LLM semantic researcher
15. Equal-budget bakeoff
16. Statistical audit
17. Sealed OOS
```

---

# 17. First Claude Code Task Boundary

最初のClaude Code依頼では、**全実装を開始させないことを推奨**する。

初回タスクは以下までとする。

1. このROADMAP.mdを読む
2. 既存repositoryを調査
3. 現在のデータ構造を確認
4. Phase 0に必要な要件を整理
5. 不足・矛盾・リスクを指摘
6. directory設計を提案
7. Phase 0実装計画を作成

完成コードの大量生成は次のstepに分ける。

---

# 18. Definition of Success

本研究の成功は、

> 「儲かるstrategyが1個見つかった」

だけではない。

以下のいずれかでも研究として成功とする。

### Case A

LLM searchがRandom / Geneticより明確に効率的。

→ LLM Quant Researcherを拡張。

### Case B

LLM ≈ Random / Genetic。

→ LLM alpha searchの価値は限定的。GP/MCTS中心へ移行。

### Case C

全search methodでOHLCV alphaなし。

→ OHLCV search spaceの限界を確認し、aggTradesへ移行。

### Case D

gross alphaは存在するがcost後に消える。

→ alpha predictionではなくexecution / abstentionへ研究対象を移す。

### Case E

clock / order flow / liquidity stateでのみedgeが残る。

→ microstructure dataへ重点移行。

つまり本PoCの目的はprofit maximizationではなく、

> **次にどの研究方向へ進むべきかを低コストで識別すること**

である。

---

# 19. Final Principle

本プロジェクトで最も重要なルール:

> **LLMをJudgeにしない。LLMはResearcherである。**

Researcherは自由に仮説を考えてよい。

しかし、

- 未来を見られない
- 評価ルールを変えられない
- Final OOSを見られない
- transaction costをごまかせない
- statistical auditを回避できない

構造にする。

その上で、

> **LLMによるsemantic researchが、本当にrandom/evolutionary searchより優れているか**

を同一budgetで検証する。

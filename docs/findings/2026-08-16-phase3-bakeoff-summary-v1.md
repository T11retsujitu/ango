# Phase 3 Alpha Search Bakeoff — 総括(negative result として正式に閉じる)

- 確定日: 2026-08-16
- プロトコル: [bakeoff_protocol v1](../phase3/bakeoff_protocol.md)(共通部 §1–§3 は
  全 arm 共通・実行前凍結。各 arm の各論も実行前に追記凍結)
- 一次記録: `experiments/phase3/{random_seed20260818, genetic_seed20260819,
  llm_claude-opus-5, baselines}/`
- arm 別 findings: [Arm A(Random)](2026-08-16-phase3-armA-random-v1.md) /
  [Arm B(Genetic)+Baselines](2026-08-16-phase3-armB-genetic-baselines-v1.md) /
  [Arm C(LLM)](2026-08-16-phase3-armC-llm-v1.md)
- **Final OOS は未開封**。ここで言う「生存」はすべて validation split 上の暫定生存であり、
  Final OOS 生存ではない。

共通条件: BTC-USDT-SWAP 5分足 / OHLCV のみ / frozen DSL v1 / 同一 Deterministic Evaluator /
primary cost = base taker(片道5bps) / 30 evaluated candidates per search arm。

## 1. 会計(機械生成)

```sh
uv run python -m mce.phase3_summary --json experiments/phase3/bakeoff_summary.json
```

| arm | draw | rejected | duplicate | evaluated | research_pass | validation_survivor |
|---|---:|---:|---:|---:|---:|---:|
| random | 46 | 16 | 0 | 30 | 4 | 0 |
| genetic | 102 | 8 | 64 | 30 | 0 | 0 |
| llm | 32 | 2 | 0 | 30 | 0 | 0 |
| baselines | 10 | 0 | 0 | 10 | 0 | 0 |
| **total** | 190 | 26 | 64 | 100 | 4 | 0 |

整合性(同上コマンドの `consistency` ブロック):

- 4 arm すべてが**同一 features manifest**(sha256 `f759f5ed…`)で評価された
- protocol は全 arm `phase3_bakeoff_v1`
- source commit は3つ(`7fe73ce` / `76f60c6` / `6b288a0`)。ただし Arm B→C 間の差分は
  `search/budget.py` の空ファイル生成タイミングのみで、**Evaluator・DSL・backtest は無変更**
  (`git diff 76f60c6 6b288a0 -- src/mce/search/runner.py src/mce/dsl src/mce/backtest` が空)。
  arm 間比較の前提は保たれている。

## 2. Primary endpoint

主指標は事前に「**validation survivors / evaluations**」と定義されている
(research 通過数ではない — bakeoff_protocol §3、ROADMAP §Phase 6 meta-search comparison)。

```text
Random   = 0 / 30
Genetic  = 0 / 30
LLM      = 0 / 30
Baselines= 0 / 10
```

**この30評価 PoC では、survival endpoint について search method 間の差は観測されなかった。**

## 3. Secondary observations(探索の質)

| arm | valid_rate | duplicate_rate | unique/draw | net>0 (taker) | net>0 (maker_low) | net 中央値 | trades 中央値 | exposure 中央値 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| random | 0.652 | 0.000 | 0.652 | 7/30 | 8/30 | −5.218 | 4874.5 | 0.330 |
| genetic | 0.922 | 0.627 | 0.294 | 0/30 | 1/30 | −1.672 | 569.5 | 0.599 |
| llm | 0.938 | 0.000 | 0.938 | 0/30 | 1/30 | −1.656 | 1575.0 | 0.038 |
| baselines | 1.000 | 0.000 | 1.000 | 1/10 | 1/10 | −11.328 | 11402.0 | 0.999 |

- **LLM は valid candidate 生成に優れていた**(拒否 2/32。制約違反は同一機序の param 超過のみ)。
- **LLM は duplicate が少なかった**(0/32)。
- **LLM は semantic family 多様性を示した**(消費32提案で31 family)。
- **Genetic は cost-dominated 地形で強く収束した**(best sharpe −19.6 → −0.93 の単調改善)。
- **Genetic は duplicate concentration を起こした**(64/102、最頻 AST は16回再抽選)。
- **Random の research 通過 4本はすべて long-only の research 期 drift-fit で、
  validation で全滅した**(1本は min_trades ガードが偽 survivor を阻止)。
- したがって **research 通過数は探索性能の主指標として信頼できない**。
- always_long は net +1.29 だが決済0回で規則上審査対象外(protocol §9 に事前明記)。

## 4. Result と Interpretation の分離

### Result(観測された事実のみ)

> **現在の探索budget(30 evaluations/arm)と凍結済み OHLCV DSL v1 の範囲では、
> どの searcher も validation survivor を発見できなかった。**

コスト後 net が正になった候補は、100評価中 8件(taker)にすぎず、
そのうち research gate(trades≥30 ∧ net>0 ∧ sharpe>0)を通ったのは 4件、
validation を越えたのは **0件**。

### Interpretation(事後解釈。Result とは別物)

1. **コストの壁が探索空間全体を支配している。** taker 往復10bps に対し、
   実効N を満たす候補(trades≥30)の break-even cost 中央値は
   random −0.03 / genetic −0.45 / llm −0.06 bps と 0 近傍かマイナス。
   maker_low(往復2bps)へ緩めても net>0 は 100評価中 11件で、地形は反転しない。
2. **research→validation 防火壁は設計どおり機能した。** 単一区間バックテストなら
   Random の4本は「発見」として報告されていた。
3. **探索アルゴリズムの高度化は、この地形では主指標を動かさなかった。**
   exploitation(GA)も semantic prior(LLM)も、床(0)を上げなかった。
4. **LLM の価値は survival ではなく提案の質に現れた**(§3)。これは
   ROADMAP §2.2 の meta-research hypothesis と整合するが、alpha searcher としての
   優位性の証明ではない。
5. LLM 提案36件中34件が、仮説本文で liquidation / inventory / absorption /
   passive・aggressive / book depth / funding / spread / queue といった
   **OHLCV へ集約される前の実体**に言及していた(事後の固定語彙スキャン)。
   探索者は微細構造の機序を語りながら、OHLCV 代理でしか表現できなかった。

## 5. What was learned

- OHLCV DSL v1 × 30 evaluations × 3 searcher で、validation 生存は0(4通りの探索手法で再現)。
- 「research で良く見える候補」は regime drift 適合で作れてしまい、防火壁が必要である
  (Random の4本が実演)。
- 小 population の steady-state GA は cost-dominated 地形で数個体へ崩壊する(duplicate 64)。
- LLM は凍結制約を守り、重複せず、意味的に多様な候補を出せる(実務的に有用な資産)。
- 主指標を「research 通過」ではなく「OOS 生存効率」に置いた設計は正しかった。
- 100評価の budget 会計・台帳・artifact 一式が、後から機械的に再集計できる形で残った。

## 6. What was NOT learned(重要)

以下は**この実験からは言えない**。

1. **「LLM と Random/Genetic が統計的に同等」とは言えない。** 0/30 が3つ並んだだけで、
   各 arm の真の生存率は片側95%上限で **9.5%**(= 1 − 0.05^(1/30))まで許容される。
   3 arm を合算しても 0/90 で上限 3.3%。**statistical insignificance ≠ equivalence。**
2. **「OHLCV に alpha が存在しない」とは言えない。** 検証したのは DSL v1 で表現でき、
   かつ30回引いた候補だけである。
3. **「information set が唯一のボトルネックである」とは言えない。** 少なくとも次の
   代替仮説が同じ結果を説明しうる:
   - OHLCV DSL v1 の表現力不足(ヒステリシス・状態機械・相互作用項・非線形変換が無い)
   - 探索budget不足(30 evaluations / arm)
   - target 設計の問題(方向符号のみ・固定 holding・abstain の粗さ)
   - signal horizon と transaction cost / execution model の不整合
     (5分足 taker 往復10bps で回収できる horizon を検証していない)
   - research 窓(2023-11〜2025-07、約3倍上昇)と validation 窓の regime 差
   - search algorithm の能力不足(memory 無し・duplicate 制御弱・budget 配分単純)
4. **Final OOS の結果は何も分かっていない**(未開封。開封していない事実自体が資産)。
5. LLM の masking が parametric hindsight を実際に抑制できたかは未検証。

## 7. Research decision(優先順位の変更であって否定ではない)

Phase 3 直後に以下へ進まない: Random の改良 / GA の mutation・crossover 改良 /
LLM prompt tuning / failure memory / positive memory / MCTS / DSL v1 への
technical indicator 大量追加 / evaluation budget の大幅増加。

**削除ではなく保留**する(→ [research backlog](../research_backlog.md))。理由:

> Phase 3 では searcher を3通りに変えても validation survivor は観測されなかったため、
> 同じ OHLCV DSL v1 空間で searcher tuning を続ける研究 ROI が現時点で低い。

次に検証する仮説を、**期待情報価値が最も高いもの**として1つ選ぶ:

> **OHLCV に集約される前の market microstructure / derivatives / cross-venue 情報には、
> 5分 OHLCV を条件としても残る incremental information が存在するか。**

Research Question:

> Which additional information sets contain incremental, cost-relevant predictive
> information beyond 5-minute OHLCV?

これは §6.3 の代替仮説を否定するものではなく、**次に検証する順序の決定**である。
検証設計は [Phase 7 — Information-Space Expansion protocol](../phase7/information_space_expansion_v1.md)。

## 8. 凍結事項

- Phase 3 の4 run(Random / Genetic / LLM / Baselines)は**凍結**。再実行・seed 変更・
  閾値変更・criteria 緩和を行わない。
- OHLCV DSL v1(`docs/phase2/dsl_spec.md`)は Phase 3 の探索空間定義として凍結保存する。
  結果を見た後に operator / threshold menu を変更して Phase 3 を再 run しない。
  新しい情報集合を探索対象にする場合は **Market Microstructure DSL v2** として別 version を作る。
- Judge freeze v1・Final OOS firewall は継続。
- ROADMAP の Phase 0〜3 の設計記述は事後改竄しない(実行結果は追記で残す)。

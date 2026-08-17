# Phase 3 Arm C — LLM Semantic Search 公式run(生存者0)

- 確定日: 2026-08-16
- プロトコル: [bakeoff_protocol §10](../phase3/bakeoff_protocol.md)(実行前追記凍結。
  budget 30 / masking / feedback scope / 翻訳・量子化規則とも凍結値。回し直しなし)
- 一次記録: `experiments/phase3/llm_claude-opus-5/`(candidates.jsonl 全32 draw +
  summary.json + llm_transcript.jsonl 全6リクエスト/レスポンス)
- DSL: [dsl_spec v1(凍結)](../phase2/dsl_spec.md)、Judge: [freeze_v1](../phase1/freeze_v1.md)
- model: `claude-opus-5` / mode: live / **deterministic: false・replayable: true**
- **Final OOS は未開封**(封印継続)。本 arm は research 通過が0だったため
  validation split も一度も開いていない。

## 1. 会計(ROADMAP §4.5)

| counter | 値 | 事前予想(§10.6) |
|---|---|---|
| candidate_count(draw) | 32 | — |
| rejected(制約違反) | 2(6.3%) | 10〜30% → **外れ(予想より良い)** |
| duplicate | **0**(0%) | 5〜20 → **外れ(予想より良い)** |
| runtime_failure / plan_error / refusal | 0 / 0 / 0 | — |
| evaluated(=budget) | 30 | 30 |
| research 通過 | **0** | 0〜4 → **的中** |
| validation 評価 | 0 | — |
| validation 生存 | **0** | 0が最頻 → **的中** |
| API 呼び出し | 6(上限12) | — |
| 提案総数 / 消費 | 36 / 32(4件は budget 消化により未使用) | — |
| semantic family(消費32件) | 31 | diversity 優位 → **的中** |

再現コマンド(artifact だけを読む決定論的集計):

```sh
uv run python -m mce.phase3_summary --json experiments/phase3/bakeoff_summary.json
```

## 2. 探索の質(副次指標)

| 指標 | LLM | Random | Genetic |
|---|---:|---:|---:|
| valid rate(validator 通過 / draw) | **0.938** | 0.652 | 0.922 |
| duplicate rate | **0.000** | 0.000 | 0.627 |
| unique candidate / draw | **0.938** | 0.652 | 0.294 |
| research net 中央値(taker) | −1.66 | −5.22 | −1.67 |
| trade_count 中央値 | 1,575 | 4,875 | 570 |
| exposure 中央値 | **0.038** | 0.330 | 0.599 |

- **提案の無駄が最も少ない**: 30評価を得るのに 32 draw(Random 46・Genetic 102)。
  LLM は凍結制約(max_parameters 6・window メニュー・両側条件のコスト倍化)を
  概ね理解して提案した。
- **拒否2件はどちらも同じ機序**: `side="both"` + clock filter で params 7 > 6。
  プロトコル §10.1 が事前に警告した唯一の failure mode を、36提案中2回だけ踏んだ。
- **exposure 中央値 0.038** が最も特徴的。LLM は「常時ポジションを持つ」戦略ではなく、
  条件付きで稀にしか発火しない低 exposure・低 turnover 型を選好した。
  cost-aware であろうとした痕跡はあるが、**それでも research gate は1つも越えていない**。

## 3. research 結果(primary = base_taker)

- 30評価すべて `net <= 0`。net > 0 は **0/30**(Random 7/30、Genetic 0/30)。
- trade_count ≥ 30 を満たす 27件に限れば最良 net は **−0.070**(通過には net>0 が必要)。
- maker_low(片道1bps)参考でも net>0 は 1/30。**コスト水準を1/5にしても地形は正にならない。**
- trade_count < 30 が3件(うち2件は発火0回)。稀な条件を狙った結果の実効N不足。
- research 通過が0なので **validation split は Arm C では一度も開かれていない**
  (validation_count = 0)。防火壁の観点では最も安全な arm。

## 4. 非決定性の扱い(§10.4 の約束どおり)

Claude Opus 5 以降 temperature/seed は API から削除されており、Arm A/B のような
bit 再現性は無い。本 run は次で担保している。

- 全6リクエスト/レスポンスを `llm_transcript.jsonl` に model ID・prompt sha256 込みで記録
- `--replay <dir>` で記録済み plan を再評価すれば **評価側は完全決定的**
- artifact に `deterministic: false` / `replayable: true` を明記

したがって「同じ prompt で再実行すれば同じ 0/30 になる」ことは**保証されていない**。
本 findings の主張は「この1 run で観測された事実」に限定される。

## 5. 結論(result)

> **frozen OHLCV DSL v1・budget 30 evaluations・primary cost = base taker の条件下で、
> LLM Semantic Search は validation 生存戦略を発見しなかった(0 / 30)。**

副次的に、LLM は valid candidate rate(0.938)・duplicate rate(0.000)・
semantic family 多様性(消費32件で31 family)で3 arm 中最良だった。

## 6. 解釈(interpretation — 事後)

1. **主指標では Random・Genetic と同じ床(0)に着いた。** これは「LLM に価値が無い」でも
   「LLM が Random と統計的に同等」でもない。0/30 が3回並んだだけであり、
   30評価では真の生存率が最大 9.5%(片側95%上限 = 1 − 0.05^(1/30))まで
   データと矛盾しない。**差が無いことを示す検出力はこの設計に無い。**
2. **LLM の強みは「探索効率」ではなく「提案の質」に出た。** 制約遵守・非重複・意味的多様性は
   ROADMAP §2.2 の meta-research hypothesis(LLM の強みは仮説空間の意味的整理)と整合する。
   ただしそれは **survival endpoint とは別の軸**であり、混同して「LLM が勝った」と書かない。
3. **提案の機序と観測可能量のズレ**(本 run で最も示唆的な事後観察):
   36提案のうち **34件**が、仮説本文で liquidation・inventory・absorption・passive/aggressive・
   order book/depth・funding・spread・queue といった **OHLCV へ集約される前の実体**に言及していた
   (固定語彙による機械的スキャン。`mce.phase3_summary.MECHANISM_KEYWORDS`。事前登録指標ではない)。
   family 名にもそれが現れている: `liquidation_cascade_continuation` /
   `execution_footprint_impact_decay_fade` / `value_area_inventory_reversion` /
   `low_vol_dislocation_inventory_reversion` / `positioning_exhaustion_fade`。
   **LLM は微細構造の機序を語り、それを volume_z・return・volatility の OHLCV 代理で
   表現させられていた。** これは「LLM がうまく探索できなかった」証拠ではなく、
   「機序の native な観測量が DSL v1 に存在しない」という**探索空間側の事実**である。
   → Phase 7(information-space expansion)の直接の動機。ただしこれは
   **仮説であって証明ではない**(§7 の留保を参照)。

## 7. 限界(limitations)

- **n = 1 run**。model 1つ・prompt 1版・budget 30。LLM search の一般的性能ではない。
- **非決定的**。再実行で同じ提案列は得られない。
- **翻訳器と量子化が LLM の表現力を上限で縛っている**(探索空間の同一性のために意図的に
  そうした)。連続閾値・非メニュー window・複雑な木構造は原理的に提案できない。
- **masking の有効性は検証していない**。銘柄・日付・価格水準は隠したが、
  「LLM が BTC だと推測しなかった」ことの証拠は無い(HindsightBench 的な対照 arm は未実施)。
- feedback は research primary のみ6ラウンド。memory も positive/negative RAG も無い
  (Phase 4 の対象で、本 run には含まれない)。
- research 窓(2023-11〜2025-07)は約3倍上昇のブル一色。**探索対象の地形自体が偏っている。**

## 8. 含意(implication)

- Arm C の記録は凍結(再実行しない)。Phase 3 の3 arm はこれで完了。
- Arm C 単独では ROADMAP §9.3(LLM search hypothesis failure)の判定に必要な
  「Random/Genetic に対する優位性」は survival では示されず、
  **diversity / duplicate rate / valid rate では示された**。
  総括判定は [Phase 3 bakeoff summary](2026-08-16-phase3-bakeoff-summary-v1.md) に分離する。
- 資産として残すもの: plan translator(semantic → AST)、量子化による探索空間の同一化、
  masking 仕様、feedback firewall、transcript 記録形式。
  **Market Microstructure DSL v2 を作る時にそのまま再利用できる。**

# Phase 3 Arm B(Genetic)+ Baselines 公式run(生存者0)

- 確定日: 2026-08-16
- プロトコル: [bakeoff_protocol §8–9](../phase3/bakeoff_protocol.md)(実行前追記凍結。
  seed 20260819 / budget 30 / 遺伝操作・fitness とも凍結値)
- 一次記録: `experiments/phase3/genetic_seed20260819/`、`experiments/phase3/baselines/`

## Bakeoff 中間順位表(主指標: survivors / evaluations)

| arm | evals | research通過 | validation生存 | draw | duplicate | 特記 |
|---|---|---|---|---|---|---|
| Random(A) | 30 | 4(全てlong-only drift-fit) | **0** | 46 | 0 | 通過4は全てvalidationで死亡 |
| Genetic(B) | 30 | **0** | **0** | 102 | **64** | 30評価全てnet≤0。収束による重複多発 |
| Baselines | 10 | 0 | 0 | 10 | 0 | momentum族はnet −5〜−29で大敗 |
| **累計** | **70/100** | 4 | **0** | — | — | 残りは Arm C(LLM)30 のみ |

## Arm B の解剖

- **GAの機構自体は機能した**: best-sharpe は評価順に −19.6 → −1.52 → −0.93 と
  単調改善。tournament・elitism・メニュー内mutationは設計どおり動いている。
- **しかし登る山が存在しなかった**: 30評価の net は最大でも 0.0、net>0 は **0/30**
  (Randomは7/30)。GAは初期集団6個体の負の盆地で局所改善に budget を使い切り、
  Randomが偶然踏んだ long-only drift 域に到達しなかった。
- **重複64/102(予想3〜15を大幅超過)**: 最頻ASTは16回・11回・11回と再抽選され、
  population 6 の steady-state + 局所的mutationが数個のエリートへ崩壊した。
  budget は消費されない(会計は正しく守った)が、探索の実効多様性は
  ユニーク30個 ≪ Random と同数という結果に。
- 最良個体は「volatility(48) > 0.005 で short」(trades 19・net −0.185)。
  実効N不足であり、いずれにせよ通過に値しない。

## 解釈(bakeoff の研究課題に対して)

1. **コスト支配・ほぼノイズの fitness 地形では、exploitation は無価値〜有害。**
   GAの「改善」はノイズの負域内での登坂であり、OOS生存には1歩も近づかなかった。
2. **ただし主指標では Random と同着(0 = 0)。** Randomの research 通過4本は全て
   validation で死んだ偽陽性なので、「research 通過数」は探索性能の指標として
   欺瞞的である。**中間指標でなく OOS 生存効率で比較するという ROADMAP の設計が
   ここで効いている。**
3. **多様性維持と duplicate 制御の必要性が実証された**(Phase 4 の
   diversity/duplicate mechanism 導入の根拠。AlphaAgent / QuantaAlpha の
   問題意識をこの規模でも再現)。改良候補(v2で凍結し直す場合):
   population 拡大、fitness sharing、重複ペナルティ、mutation の非局所化。
4. Baselines は全滅(momentum 族 net −5〜−29)— Phase 0 実測(momentum は
   gross 負)と整合。always_long は net +1.29 だが決済0回で規則上審査対象外
   (プロトコル §9 に事前明記どおり)。

## 事前予想の採点

- Arm B: 「research通過 4〜10」→ **外れ(0)**。「生存0が最頻」→ **的中**。
  「duplicate 3〜15」→ **外れ(64)**。「survivors/evals で Random と差がつかない」
  → **的中(0=0)**。外れ2つはどちらも「GAの収束の速さ・地形の悪さ」を
  過小評価したことによる。
- Baselines: 「通過0〜1・生存0」→ **的中(0・0)**。

## 凍結事項・次へ

- Arm B・Baselines の記録は凍結(再実行しない)。
- failure memory の種: 「小pop steady-state GA は cost-dominated 地形で
  数個体に崩壊する」「research 通過は drift-fit 偽陽性を含む」。
- 次: **Arm C — LLM Semantic Search**(budget 30)。仮説レコード(semantic schema)
  → deterministic な AST 変換 → 同一 Evaluator。HindsightBench 系の
  マスキング(銘柄・日付の匿名化)を検討した上でプロトコル追記凍結する。
- Arm C 終了後、ROADMAP §Definition of Success の Case 判定
  (現時点は Case C/D の軌道: OHLCV survivor 不在)と Phase 7 分岐判断を行う。

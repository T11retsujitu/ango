# Phase 3 Arm A — Random Grammar Search 公式run(生存者0)

- 確定日: 2026-08-16
- プロトコル: [docs/phase3/bakeoff_protocol.md](../phase3/bakeoff_protocol.md)
  (v1 凍結。seed 20260818 / budget 30 / 選抜規則とも実行前凍結・回し直しなし)
- 一次記録: `experiments/phase3/random_seed20260818/`(candidates.jsonl 全46 draw +
  summary.json)
- DSL: [dsl_spec v1(凍結)](../phase2/dsl_spec.md)、Judge: [freeze_v1](../phase1/freeze_v1.md)

## 会計(ROADMAP §4.5)

| counter | 値 | 事前予想 |
|---|---|---|
| candidate_count(draw) | 46 | 40〜90 → **的中** |
| rejected(制約違反) | 16(35%) | 2〜5割 → **的中** |
| duplicate / runtime_failure | 0 / 0 | — |
| evaluated(=budget) | 30 | 30 |
| research 通過 | 4 | 0〜3 → 1 超過(僅差) |
| validation 生存 | **0** | 0が最頻 → **的中** |

30評価の research net(taker)は 中央値 **−4.73**・最大 +1.27・最小 −59.97。
net>0 は 7/30(maker_low 参考でも 8/30)。

## research 通過 4候補の解剖(全て validation で死亡)

| hash | 形 | R net | R Sharpe | R trades | BE | V net | V 判定 |
|---|---|---|---|---|---|---|---|
| a11712ac | **long-only**(ma_slope 逆張り) | +0.17 | 0.21 | 889 | 6.0bps | −0.32 | net負 |
| 547344ad | **long-only**(低range ∨ MA上向き, holds_for) | +0.60 | 0.79 | 418 | 12.1bps | −0.31 | net負 |
| bffd403c | **long-only**(長期MA slope 平滑の逆張り) | +0.96 | 1.19 | 107 | **49.6bps** | −0.15 | net負 |
| 8a54cf1e | **long-only**(高ボラ局面で買い) | +0.15 | 0.85 | 44 | 22.0bps | +0.06 | **trades 1 < 10** |

## 結論

1. **Judge の research→validation 防火壁が設計どおり機能した。** research 通過4本は
   例外なく long-only であり、ブル一色の research 窓(2023-11〜2025-06、約3倍上昇)の
   ドリフトに適合した候補だった。レジームの異なる validation 窓で3本が net 負に転落。
   research 最良候補(net +0.96・Sharpe 1.19・break-even 49.6bps)ですら −0.15。
   **「research で立派に見える」と「OOSで生きる」の乖離の実演**であり、
   単一区間バックテストなら4本とも「発見」と報告されていた。
2. **min_trades ガードが偽 survivor を1本阻止した。** 8a54cf1e は validation net 正・
   Sharpe 2.49 だが trade 1件。ガードが無ければ生存扱いになっていた
   (実効N規律=恒久ルール1のコード化が初仕事をした)。
3. **コストの壁は探索空間全体を支配している。** net 中央値 −4.73、taker で net>0 は
   7/30。ランダムに引いた OHLCV 戦略の大半は turnover コストで即死する。
4. **Arm A のベースラインが確立した: survivors / evaluations = 0 / 30。**
   Genetic(Arm B)・LLM(Arm C)はこの床に対して比較される。ROADMAP の
   meta-comparison(evaluations per surviving strategy)の分母が動き始めた。
5. 事前予想は4項目中3的中(draw数・生存0・拒否率)、research通過のみ
   予想上限を1超過。maker_low 参考(net>0 8/30)も予想レンジ 3〜8 の上限で的中。

## 凍結事項・次へ

- Arm A の結果・記録は凍結(再実行しない)。
- 通過4候補の AST・hash は failure memory の種として記録済み(Phase 4 で
  「long-only drift-fit は validation で死ぬ」という mechanism として抽象化する価値)。
- 次: **Arm B — Genetic Search**(AST-level mutation / crossover、同一 runner・
  同一選抜規則・budget 30)。Baselines arm(10)も同時に実施予定。

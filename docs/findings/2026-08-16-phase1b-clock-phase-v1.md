# Phase 1B — Clock Phase / Quarter-Hour v1(方向性なし・活動構造あり)

- 確定日: 2026-08-16
- プロトコル: [docs/phase1/phase1b_protocol.md](../phase1/phase1b_protocol.md)
  (phase1b_v1。判定基準・事前予想とも実行前に凍結。argmax方式への修正は
  合成データ検証中・実データ実行前 — 経緯はプロトコル§5に記録)
- replication class: **cross_exchange_validation**(原論文 arXiv:2607.09426 は
  Binance perp・1分足を含む。本実験は OKX 5分足のみ)
- データ: research 区間(2023-11-19〜2025-07-01、169,708本)。validation 未使用。
- 一次記録: `experiments/phase1b/clock_phase.json`(全phase・全統計・sha256込み)

## 判定サマリ

| family | 真の境界 | 最大効果 | D1通過 | verdict |
|---|---|---|---|---|
| minute_mod_15 | 0 | 0.108bps(phase 5) | なし | **no_directional_structure** |
| minute_mod_60 | 0 | 0.241bps(phase 25) | なし | **no_directional_structure** |

directional(fwd_return_5m)は全候補で効果 ≤ 0.24bps・FWER p ≥ 0.39・|t| ≤ 1.9。
D1(|t|≥3 ∧ p<0.01 ∧ ≥1bps ∧ 前後半符号一致)に近い候補すら存在しない。

## activity 構造(強く確認)

| 位置 | mean \|return_5m\| | t vs rest |
|---|---|---|
| m15 境界バー(:00/:15/:30/:45 開始) | 10.90bps | **+12.9** |
| m15 境界直前バー(:10/:25/:40/:55 開始) | 9.67bps | **−16.6** |
| m60 hour境界バー(:00) | 11.20bps | +8.3 |
| m60 half-hour(:30) | 11.17bps | +7.7 |
| m60 :55 バー(最静穏) | 8.97bps | **−16.0** |

「境界直前が静まり、境界バーで活動が跳ねる」周期構造が OKX にも明確に存在する
(hour境界は :55 比で約 +25% のボラ)。原論文の periodic algorithmic trading の
**活動成分は cross-exchange で成立**、**方向成分は 5分足粒度の OKX では不成立**。

## 結論

1. **quarter-hour / hour 境界の方向予測性は OKX 5分足に存在しない。** クリーンな帰無。
   最大効果 0.24bps は maker 往復2bps の1/8であり、恒久ルール5以前の水準。
   留保: 原論文の directional 成分は1分粒度の境界近傍に集中する可能性があり、
   「5分足では見えない」と「存在しない」は区別する(粒度限界)。
2. **活動の周期構造は構造として確認**(H2・H4 と同族の、通年安定が期待される
   ボラ・流動性構造)。取引方向の情報ではないが、
   (a) 執行タイミング(静穏な :55 側で執行するとコスト有利の可能性)、
   (b) ストップ/サイズ設計(境界バーのボラ跳ね)、
   (c) 将来の文脈条件(Event × Context の Context 候補)
   として利用価値がある。
3. **ROADMAP 分岐**: `proceed_to_aggtrades_on_directional = false`。
   clock 単体の方向性 alpha 仮説は保留。aggTrades への移行判断は Phase 3
   (OHLCV search 全体の結果)まで持ち越し。
4. データ妥当性(照合完了・2026-08-16): hour_utc / weekday_utc の記述統計は
   既存の確定構造を再現した。
   - **H2 再現**: |return_5m| 上位は 14時UTC 15.72bps(t=+27.1)、15時 14.35(+23.5)、
     16時 13.02、13時 12.76。下位は 4〜6時UTC(7.5〜8.0bps)
   - **H1 再現**: 週末 |ret|(土 6.55 / 日 7.80bps)対 平日(11.1〜12.2bps)の比 ≈ 0.62
     — **33ヶ月追試の H1 比 0.62 と一致**
   - weekday の方向性(fwd5m)は最大 0.26bps で無し。m15×ボラレジームの
     directional も全セル ≤ 0.17bps で交互作用なし
   データ基盤と集計パイプラインの妥当性が独立経路で確認された。

## 事前予想の採点(プロトコル§7)

1. 「activity 構造は m15・m60 とも確認される」→ **的中**(t = ±12〜16)
2. 「directional は no_directional_structure が本命、届いても1〜3bps」→ **的中**
   (実際は最大0.24bpsで予想レンジ下限にも届かず)
3. 「hour_utc は H2 を再現する」→ **的中**(13〜16時UTCが上位、t=+14.8〜+27.1。
   さらに H1 の週末/平日比 0.62 まで一致)
4. 「verdict 予想: 両family no_directional、activity 確認、aggTrades 根拠なし」→ **的中**

## Phase 1 完了

Phase 1A(abstention 棄却)・Phase 1B(方向性なし・活動あり)の両方で、
凍結プロトコル → 決定的実行 → 機械判定 → 台帳化のループが完遂した。
**Judge は現実的な研究を評価できる(ROADMAP Phase 1 の目的達成)。**

→ 次: [Phase 1 Freeze](../phase1/freeze_v1.md)(Data Contract / Execution /
Cost / Split / Metrics の凍結宣言)、その後 Phase 2(Semantic Schema + DSL/AST)。

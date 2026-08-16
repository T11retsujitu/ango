# Phase 1A — Cost-Aware Abstention v1(棄却)

- 確定日: 2026-08-16
- プロトコル: [docs/phase1/phase1a_protocol.md](../phase1/phase1a_protocol.md)
  (phase1a_v1。判定基準・事前予想とも**実行前に凍結**、実行後の変更なし)
- replication class: **method_transfer**(原論文 arXiv:2606.00060 は BTC 1時間足。
  本実験は OKX 5分足であり exact replication ではない)
- データ: research 区間のみ(2023-11-19〜2025-07-01)。walk-forward 15 fold
  (train 120d / test 30d)、embargo 70分。validation は未使用のまま温存。
- 一次記録: `experiments/phase1a/phase1a_maker_low.json` / `phase1a_base_taker.json`
  (features/labels の sha256・source commit 込み)

## 判定サマリ(凍結基準による機械判定)

| シナリオ | 閾値 | J1 turnover減 | J2 net改善 vs sign | J3 vs random | trades | verdict |
|---|---|---|---|---|---|---|
| maker_low(往復2bps) | 2bps | 87% / 中央値比0.60 ✓ | 87% / 中央値+0.101 ✓ | **33% ✗** | 9,923 | **abstention_rejected** |
| base_taker(往復10bps) | 10bps | 100% / 中央値比0.011 ✓ | 100% / 中央値+1.185 ✓ | **60% ✗**(基準2/3) | 239 | **abstention_rejected** |

## グロス/コスト分解(15 fold 合計、非複利)

| arm | gross | cost | net | per-trade |
|---|---|---|---|---|
| model_sign(maker) | **−0.105** | 3.710 | −3.815 | — |
| abstention 2bps | **+0.029** | 1.985 | −1.957 | gross +0.03bps / net −1.97bps(n=9,923) |
| abstention 10bps | **+0.103** | 0.239 | −0.136 | gross **+4.3bps** / net −5.7bps(n=239) |

## 結論

1. **LogReg(OHLCV 5列)の方向予測にエッジ選別力はない。** sign 常時追随は
   グロスでもわずかに負(−0.105)。閾値2bpsの選別後トレードはグロス
   +0.03bps/trade で実質ゼロ。net 改善(J2)は全てコスト削減の機械的効果であり、
   exposure を揃えた無作為対照に 10/15 fold で負けて J3 棄却。
2. **per-bar 閾値 abstention はポジションを断片化する。** fold 2・9 では abstention の
   turnover が sign を上回った(閾値を跨ぐたび exit/re-entry が発生)。random 対照は
   シグナル区間を丸ごと残すため round trip が少なく、この断片化コスト差が J3 敗北の
   主因の一つ。→ **閾値規則にはヒステリシス(entry/exit 閾値分離)か最小保有が必要**。
   将来の DSL 設計(Phase 2)への入力とする。
3. **監視リスト: 高閾値(10bps)選別のグロス +4.3bps/trade(n=239)。** 予測エッジの
   極端な尾部にのみ正のグロスが残るが、taker 往復10bpsに届かず net 負。
   H6 オラクル(+4.07bps/件 < コスト)と同じ「グロスは尾部に在るがコスト未満」の
   構図(ROADMAP Case D の兆候)。実効Nが小さく有意性未検定のため、恒久ルール1により
   採用判定はしない。
4. **Judge 検証としての Phase 1A は成功。** 4 arm(sign / abstention / random対照 /
   buy&hold)× 15 fold × 2 コストシナリオが決定的に実行され、凍結基準が
   「取引削減の価値」と「エッジ選別の価値」を分離して後者を棄却した。
   選別なしの全結果報告・追記専用 artifact も機能。

## 事前予想の採点(プロトコル§7に凍結、検定実行者=ユーザーには実行前非開示)

1. 「J1 はほぼ機械的に成立」→ **的中**(87% / 100%)
2. 「J2 も成立しやすい(取引削減効果込み)」→ **的中**(87% / 100%)
3. 「J3 が本丸で成立確率は五分五分以下」→ **的中**(33% / 60% で両方棄却)
4. 「絶対収益は taker で負、maker でゼロ近傍」→ **半分的中**: taker は合計 −0.136 だが
   fold 正率 60%(ゼロ近傍)。maker は「ゼロ近傍」予想に反し明確に負(−1.957)。
   外れの理由は選別の逆効果ではなく**断片化による round trip 増**(上記結論2)で、
   これは事前に織り込めていなかった。
5. 「taker は取引がほぼ消えて判定不能の可能性」→ **外れ**: 239 trades でガード(30)を
   超え、有効判定が成立した。

## 凍結事項・次へ

- **abstention 単体(現行モデル・現行閾値規則)は棄却。** モデル改良・閾値チューニングの
  再挑戦をここで始めない(Judge 検証という目的は達成済み。探索を始めると Phase 3 の
  search 比較を先食いする)。再挑戦する場合は v2 プロトコルとして再凍結してから。
- ヒステリシス付き執行規則は Phase 2 DSL の operator 候補として記録
  (`abstain_unless` に enter/exit 2閾値)。
- 高閾値尾部のグロス正(結論3)は監視リスト。Phase 1B(clock phase)以降で
  文脈(時間帯・ボラレジーム)との交互作用として再訪しうる。
- 次: **Phase 1B — Clock Phase / Quarter-Hour**(cross-exchange / replication-inspired
  validation。placebo phase 必須)。

# findings — 検証済み結論の台帳

検証(探索→凍結→追試)の結論をプレーンな Markdown で蓄積する。データと同じく
「結論もローカルに蓄積・バージョン管理」する。次の検証は、ここにある凍結仮説と
恒久ルールを前提として設計する。

ファイル命名: `YYYY-MM-DD-<テーマ>.md`(日付は結論を確定した日)。

## 恒久ルール(全ての探索に適用)

1. **実効N二桁前半の発見は監視リスト止まり。** 窓内の再現計算・前後半分割・
   非重複再計算を全て通過しても、同じ窓の中では選択バイアスは洗い流せない
   (実証: 2026-08-16 の H5 棄却)。採用判定は必ず窓外データで行う。
2. **追試は変更禁止の固定仮説で行う。** 閾値・条件は発見時の値で凍結し、
   OOS で再探索・再最適化をしない。判定基準(生存/棄却/レジーム依存)は
   追試の前に機械的ルールとして書き下す。
3. **事前予想は追試の前に記録する。** 検定を実行する者(人・エージェント)には
   予想を見せない。
4. **重複リターンの実効N補正。** fwd_return_1h/4h は連続バーで重複する。
   有意性は n/12・n/48 換算で評価する。
5. **効果量は必ず執行コストと比較する。** 基準: OKX perp taker 5bps/片道
   (往復10bps)。コスト未満の効果は「統計的事実」であって「エッジ」ではない。
6. **レジーム分類器を後付けするときの3原則**(選択の上に選択を重ねないため):
   - 事後の月ラベルによる成績は**オラクル上限**として明示し、リアルタイム分類器は
     「オラクル比の回収率」で評価する
   - 分類器の定義は**先に凍結**し、対象仮説の OOS 成績で調整しない。
     凡庸で標準的な定義(例: 20日リターンの符号、実現ボラの中央値比)を決め打ちし、
     駄目なら駄目と報告する。評価は walk-forward(分類も含め各時点で過去情報のみ)
   - レジームは月ラベルでなく**バー時点のローリング量**で定義する
     (月単位は N=34 しかなく、表示用と割り切る)

## 現在の研究軸(2026-08-17 更新)

**Phase 7 Tier 0 は economically negative result として閉じた**
([closeout](2026-08-17-phase7-tier0-closeout-v1.md))。Tier 0 の集約情報に弱い
incremental information は検出されたが、方向情報は確認できず、GO 2件は Y2/Y3 で
現行の執行設計へ接続できなかった。**Tier 1 へは進まない**(否定ではなく研究 ROI 上の
優先順位変更。Tier 1 仮説は backlog に `hold` で残す)。**Final OOS は未開封のまま。**

Phase 3(探索軸)と Phase 7(情報軸)が同じ壁に当たったため、次に変えるのは
**問題設定そのもの**である。「5分ごとに短期方向を当て taker で売買する」から離れ、
**先行研究から再実装・独立検証する価値の高い仮説を選ぶ**(Phase 8.0)。
5分足は捨てず、観測・執行シミュレーション基盤として維持する。

- Phase 7 総括: [Tier 0 closeout](2026-08-17-phase7-tier0-closeout-v1.md)
- 文献調査: [Phase 8.0 literature review](../phase8/literature_review_2026-08-17.md)
- 候補採点: [replication candidates v1](../phase8/replication_candidates_v1.md)
- 選定: [Phase 8.0 selection memo](../phase8/phase8_selection_memo_v1.md)
  — 第1位 **BTC spot–perp funding carry / basis**(**81**/100。訂正前 89)
- 再現プロトコル(**未凍結・v1.2**):
  [Phase 8.1 carry replication protocol](../phase8/carry_replication_protocol_v1.md)
- 凍結前 独立監査: [carry protocol audit v1](../phase8/carry_protocol_audit_v1.md)
  — 論文全文の取得で**アンカー論文の読み違い6件**、敵対監査で **fatal 4件を含む26件**を
  **一度も実行しないまま**検出・訂正した

### 履歴(2026-08-16 時点の研究軸。書き換えない)

Phase 3 Alpha Search Bakeoff は **validation survivor 0/30 × 3 arm** で完了した。
主軸を「OHLCV 空間で search algorithm を改善する」から
**「OHLCV を超える incremental information を持つ情報集合はどれか」**へ移した。

- 総括: [Phase 3 bakeoff summary](2026-08-16-phase3-bakeoff-summary-v1.md)
- 当時の設計: [Phase 7 — Information-Space Expansion](../phase7/information_space_expansion_v1.md)
- 既存 microstructure 資産の棚卸し: [microstructure v1 review](../phase7/microstructure_v1_review.md)
- 保留項目(削除しない): [research backlog](../research_backlog.md)

## 台帳

- [2026-08-17 Phase 7 Tier 0 — closeout(economically negative result)](2026-08-17-phase7-tier0-closeout-v1.md)
  — Result と Interpretation を分離。**Tier 0 に弱い incremental information はあった**が
  **方向情報は確認できず**、statistical GO と economic usability は別物。
  **GO 2件は棄却ではなく保留**(昇格権を行使しないという決定)。Tier 1 へ進まないのは
  研究 ROI 上の優先順位変更であり microstructure の否定ではない。**Final OOS 未開封のまま終了。**
  再開条件4つと未検証仮説を明記。
- [2026-08-17 Phase 7 Tier 0 incremental information test — confirmation(最終判定)](2026-08-17-phase7-tier0-confirmation-v1.md)
  — 昇格5件中 **GO 2件**。dev 最大効果(+5.45e-03・安定性6条件全通過)は
  **符号反転して脱落**。GO 2件はどちらも Y2/Y3 で方向を含まず、bootstrap CI は 0 を含む。
- [2026-08-17 Phase 7 Tier 0 incremental information test — dev](2026-08-17-phase7-tier0-screening-v1.md)
  — 27 test 中 16 が Holm 有意。ただし有意性は Y2/Y3(値動きの大きさ)に集中し、
  方向 Y1 は 9/9 非有意。confirmation へ進むのは 5 test。**GO はまだ0**。
- [2026-08-16 Phase 3 bakeoff 総括](2026-08-16-phase3-bakeoff-summary-v1.md)
  — **主指標 validation survivors / evaluations = 0/30・0/30・0/30**(Random / Genetic / LLM)。
  探索手法間に survival の差は観測されず。ただし 0/30 の片側95%上限は 9.5% で
  **同等性の証明ではない**。コストの壁が地形を支配(100評価中 net>0 は8件)。
  research 通過は探索性能の指標として欺瞞的。→ information-space へ軸を移す判断。
- [2026-08-16 Phase 3 Arm C LLM semantic search](2026-08-16-phase3-armC-llm-v1.md)
  — **生存者0/30**。valid rate 0.938・duplicate 0・semantic family 31 と提案の質は3 arm 最良、
  exposure 中央値 0.038 の低 turnover 型を選好したが research gate 通過は0。
  提案36件中34件が板・約定フロー・OI 等 **OHLCV 集約前の実体**に言及していた(事後スキャン)。
  非決定的(replayable)。
- [2026-08-16 5分足の傾向調査と33ヶ月追試](2026-08-16-5m-tendencies-33mo-retest.md)
  — 生存4(ボラ・流動性構造)/ 棄却1 / レジーム依存1 / 判定不能1 / ヌル生存2。
  Funding キャリーの33ヶ月定量化。
- [2026-08-16 Phase 3 Arm B genetic + baselines](2026-08-16-phase3-armB-genetic-baselines-v1.md)
  — **生存者0/30・0/10**。GAはbest sharpe −19.6→−0.93と機構は機能したが
  登る山が無く(net>0は0/30)、重複64で数個体に崩壊。主指標でRandomと同着0。
  「research通過はdrift-fit偽陽性を含む」ことがOOS生存効率比較の正しさを裏付けた。
- [2026-08-16 Phase 3 Arm A random search](2026-08-16-phase3-armA-random-v1.md)
  — **生存者0/30**(凍結seed・凍結選抜)。research通過4は全てlong-onlyのドリフト適合で
  validation防火壁が全滅させた(min_tradesガードが偽survivor 1本を阻止)。
  Random armのベースライン確立。
- [2026-08-16 Phase 1B clock phase v1](2026-08-16-phase1b-clock-phase-v1.md)
  — **方向性なし**(最大効果0.24bps・FWER後有意なし)/**活動構造あり**(境界バーの
  ボラ跳ね t=+12.9、境界直前の静穏 t=−16.6 は cross-exchange で確認)。
  aggTrades 進行の directional 根拠なし。Phase 1 完了 → Judge 凍結
  ([freeze v1](../phase1/freeze_v1.md))。
- [2026-08-16 Phase 1A cost-aware abstention v1](2026-08-16-phase1a-cost-abstention-v1.md)
  — **棄却**(J3: exposure一致random対照に負ける。net改善は取引削減の機械的効果のみ)。
  副産物: per-bar閾値の断片化問題(ヒステリシス必要)、高閾値尾部グロス+4.3bps/trade
  (コスト未満・監視リスト)。Judge検証としては成功。
- [2026-08-16 レジーム分類器 v1](2026-08-16-regime-classifier-v1-spec.md)
  — 分類器は機能(C2で回収率71%、C4は2024-11を完全回避)だがオラクル上限が
  +4.07bps/件・t=+0.95 と薄く、**H6 は監視リスト入り**。
- [2026-08-16 深いイベント定義 v1](2026-08-16-deep-events-v1-protocol.md)
  — ボラ正規化した5族952候補を探索。15〜25bpsの見かけ上の効果は出たが、最大でも
  t=1.03でG3通過0件。**凍結候補なし・候補別ホールドアウト追試なしで終了**。方向条件の
  深掘りを止め、ボラ・流動性の予測と運用へ移る根拠。
- [2026-08-16 First-touch v1](2026-08-16-first-touch-v1-protocol.md)
  — H6の途中経路を次足open・固定barrierで一度だけ確認。Validation B3はgross
  +0.24bps、15bps後 −14.76bps、全6設定が赤字。独立再実装も792件すべて一致。
  **Validation棄却・Final未開封。OHLCVのみの方向アルファ探索を終了**。
- [2026-08-16 Microstructure v1](2026-08-16-microstructure-v1-protocol.md)
  — prospectiveに集める約定・BBO・400段板を使い、M1=L1 OFI、M2=10bps板枯れ、
  M3=aggressive-flow吸収の3仮説だけを検定する事前仕様。**定義凍結・収集中・未検定**。
  24h soak後、60日Calibration→60日Validation→通過仮説だけ将来60日Finalとする。

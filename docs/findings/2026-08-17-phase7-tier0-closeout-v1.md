# Phase 7 Tier 0 — closeout(economically negative result として閉じる)

- 確定日: 2026-08-17
- 種別: **closeout(追記型)**。既存の Phase 7 文書を書き換えるものではない
- 直接の前提となる一次記録(**いずれも改変していない**):
  - 事前登録: [tier0_screening_preregistration_v1](../phase7/tier0_screening_preregistration_v1.md)(v1.1・凍結)
  - dev: [2026-08-17-phase7-tier0-screening-v1](2026-08-17-phase7-tier0-screening-v1.md)
  - confirmation: [2026-08-17-phase7-tier0-confirmation-v1](2026-08-17-phase7-tier0-confirmation-v1.md)
  - artifact: `experiments/phase7/tier0_screening_{dev,confirmation}_v1.json` / `tier0_freeze.json`
- **Final OOS(`ts >= 2026-01-01`)は未開封のまま閉じる。**

本文書は新しい検定を1つも実行していない。既存 artifact の**再評価・再探索・再集計も行っていない**。
行うのは「既に確定した結果に対する研究上の意思決定の記録」だけである。

---

## 1. Result(観測された事実のみ)

事実は dev / confirmation の各 findings と artifact が正であり、以下はその要約である。

| 段階 | 観測 |
|---|---|
| dev(2020-01〜2025-01 側) | 27 test 中 **16 が Holm 補正後に有意** |
| dev / 方向 Y1 | **9 test 中 0 が有意**。ΔR² は 8/9 で負 |
| dev / 昇格 | `dev_pass_pending_confirmation` **5 test**(全て Y2/Y3、全て T0-A / T0-B1) |
| confirmation(2025-01-01〜2026-01-01) | 昇格5件中 **GO 2件**(T0-A h=12 Y2 / T0-B1 h=12 Y3) |
| GO 2件の day-cluster bootstrap 95% CI | **どちらも 0 を含む** |
| dev 最大効果(T0-B1 h=48 Y2, ΔR²=+5.450e-03) | confirmation で **−5.154e-04 と符号反転** |
| 同 h=48 Y3 | +2.602e-03 → **−3.763e-04 と符号反転** |
| Y1 の十分位スプレッド edge(上限指標) | 全 cell で往復コスト 10bps 未満 |
| 独立再現 | 別環境・別ダウンロードで昇格5件の `n_eff` / ΔR² / p が**全桁一致** |
| Final OOS | **未開封** |
| Tier 1(aggTrades / bookDepth) | **未検証** |

Result 層で言えるのはここまでである。以下は解釈であり、事実と同じ強さを持たない。

---

## 2. Interpretation(事後解釈。Result とは別物)

### 2.1 Tier 0 の集約情報には「弱い incremental information」があった

16/27 が Holm 有意、confirmation でも 2 件が符号一致かつ dev の 50% 以上を保った。
これは **「Tier 0 に情報が無い」という主張の否定**である。情報は検出された。

ただし、その情報は次の性質を持つ。

1. **方向ではない。** GO 2件は Y2(実現ボラ)と Y3(下方到達幅)であり、符号を含まない。
2. **不確実性が大きい。** GO 2件とも day-cluster bootstrap CI が 0 を含む。
   placebo に対する有意性(p=0.0033)と、ΔR² 自体のサンプリング不確実性は別物であり、
   後者では 0 と区別できない。
3. **confirmation の p は family 補正を通らない。** 27 test の Holm 後は
   `27 × 0.0033 = 0.089 > 0.05`。事前登録 §17-4 は判定に p を使わないので GO は成立するが、
   「confirmation でも有意だった」とは言えない。
4. **効果は縮んだ。** dev 比 66.6% と 57.4%。dev の効果量を将来の期待値と見なせない。

### 2.2 方向情報は確認できなかった

Y1 は 9/9 で Holm 非有意、ΔR² は 8/9 で負、唯一の正も MDE 未満。
上限指標である十分位スプレッド edge ですら全 cell で往復コスト 10bps に届かなかった。

正しい読み方は事前登録 §20 のとおり:

> **Tier 0 の粒度・線形 ridge・この凍結変換の下では、各 cell の MDE を超える方向情報を検出できなかった。**

これは「方向情報が存在しない」ではない。**統計的非有意は同等性の証明ではない。**

### 2.3 statistical GO と economic usability は別物である

本 closeout の中心的な区別である。

```text
statistical GO   = 事前登録 §17 の昇格条件を満たした(= Tier 1 機序検証へ進む資格)
economic usability = 現行の執行設計で、コスト後の損益を作れる
```

**GO 2件は前者を満たし、後者を満たしていない。** 理由は単純で、Y2/Y3 は方向を含まず、
ango の現行 backtest 設計(signal at close[t] → fill at `open[t+1]`、単一銘柄・方向ポジション)は
方向を入力として要求するからである。ボラ予測から損益を作るにはサイジング・オプション等の
別の執行設計が要り、それは Phase 7 の範囲外だった(dev findings §7-2)。

したがって Phase 7 Tier 0 の総括は「statistically positive, **economically negative**」である。

### 2.4 GO 2件は「棄却」ではなく「保留」

**GO 2件を棄却したのではない。** 昇格権を得た有効な結果であり、artifact も判定も維持する。
今回行うのは「その昇格権を**現時点では行使しない**」という意思決定であって、結果の否定ではない。

同様に、符号反転した h=48 の 2 件も「情報が無い」ことを示したのではない
(confirmation findings §4-3: 「dev の推定が過大だった」「2025年のレジームが違う」
「n_eff 2,185 では推定が不安定」のいずれとも整合し、今回の設計では区別できない)。

### 2.5 Tier 1 へ進まない理由は研究 ROI 上の優先順位変更であり、microstructure の否定ではない

**否定していないこと**(明示):

- microstructure 全体に情報が無い、とは言っていない
- Tier 0 に incremental information が一切無い、とは言っていない(2.1 のとおり検出された)
- aggTrades / 板に情報が無い、とは言っていない(**未検証**)
- BTC に alpha が存在しない、とは言っていない
- longer horizon に alpha が存在しない、とは言っていない

Tier 1 へ進まない理由は次の3点である。

1. **昇格の根拠が弱い。** Tier 1 は Tier 0 の GO の機序を event 水準で詰める作業だが、
   その GO 自体の CI が 0 を含む。土台が確定していない状態で 5–8 MB/日 × 数年の
   データ取得と実装コストを投じる期待情報価値が低い。
2. **昇格しても方向にならない。** Tier 1 で Y2/Y3 の機序が判明しても、
   現行の執行設計に接続する経路が無い。接続には執行設計の新規開発が必要で、
   それは「Tier 1 を先にやる」ことの正当化にならない。
3. **同じ問題設定に留まる。** Tier 0/1 はどちらも「5分ごとに短期方向を当て、taker で売買する」
   問題設定の中の情報追加である。Phase 3(searcher を3通り変えて survivor 0)と
   Phase 7 Tier 0(情報集合を変えて方向 null)は、**探索軸と情報軸の両方で同じ壁**に当たった。
   次に変えるべきは3つ目の軸、すなわち **問題設定そのもの**である。

これは Phase 3 closeout と同じ形の判断である(否定ではなく優先順位)。
Tier 1 仮説は [research backlog](../research_backlog.md) I4 / I5 に `hold` として残す(削除しない)。

### 2.6 Final OOS を開けない判断

開封しない。理由:

1. **開封しても意思決定が変わらない。** GO 2件は方向を含まず、現行執行設計では
   損益が定義できない。Final OOS で何が出ても「Tier 1 へ進むか」の答えは変わらない。
2. **封印の価値は1回しか使えない。** ROADMAP §4.2 の firewall は「Research/Validation の
   探索が完全終了してから一度だけ」開く設計である。方向仮説を持たない段階で消費する
   合理性が無い。
3. **未開封であること自体が資産である**(Phase 3 closeout §6-4 と同じ立場)。

**したがって Phase 7 は Final OOS 未開封のまま閉じる。**

---

## 3. 未検証のまま残す仮説(削除しない)

| ID | 仮説 | 状態 | backlog |
|---|---|---|---|
| I4 | Tier 1-A aggTrades event-level(signed volume・large-trade imbalance・burst・flow persistence) | **未検証・hold** | [I4](../research_backlog.md) |
| I5 | Tier 1-B bookDepth(距離別 depth) | **未検証・hold** | I5 |
| I6 | Tier 2 OKX prospective microstructure(M1 OFI / M2 板枯れ / M3 吸収) | **収集中・定義凍結済・未検定** | I6 |
| I7 | liquidation データ | **blocked**(Binance Vision 該当パス 404) | I7 |
| I8 | 遡及 L1 tick(bookTicker、199 MB/日) | parked | I8 |
| I9 | cross-venue(lead-lag、mid 乖離、liquidity migration) | parked | I9 |
| — | GO 2件(T0-A h=12 Y2 / T0-B1 h=12 Y3)の Tier 1 機序検証 | **保留(棄却ではない)** | 本文書 §4 |
| — | Y2/Y3 を損益へ接続する執行設計(サイジング・オプション等) | 未設計 | 本文書 §4 |
| J7 | `log(0)` 汚染に強い Z20d v2 | hold(v1 では直さない) | J7 |

**Phase 7 Tier 0 の凍結事項**: 27 test の artifact・判定・事前登録は凍結する。
seed 変更・閾値変更・horizon 入れ替えによる救済・再集計を行わない(事前登録 §17 NO-GO 節)。

---

## 4. Phase 7 を再開する条件(「そのうち」と書かない)

以下のいずれかが満たされた時点で、Phase 7 Tier 0/1 を再開する。

1. **執行経路ができたとき。** Y2/Y3(ボラ・下方到達幅)をコスト後の損益へ変換できる
   執行設計(ポジションサイジング、abstention、barrier 設計、オプション等)が
   別 Phase で確立し、その入力としてボラ予測の incremental information が必要になったとき。
2. **Phase 8 で低回転域の構造的機序が確立したとき。** その機序の**条件付け変数**として
   flow / OI / positioning が必要になったとき(例: funding carry の建て玉タイミングを
   OI・positioning で条件付ける)。この場合、Tier 0 の GO 2件は「条件付け変数の候補」
   として再利用される。
3. **GO 2件の CI 下限を確定できる新しいデータが増えたとき。** confirmation 窓が1暦年しか
   無いことが CI の広さの主因である(confirmation findings §2-1)。**封印を開けずに**
   confirmation 窓を延ばせる条件が生じたとき(= 現在の封印境界 2026-01-01 が
   別の設計で更新され、2026年前半が非封印になったとき)。
4. **Tier 2(OKX prospective microstructure)の M1/M2/M3 が Validation を通過したとき。**
   その場合 Tier 1 は「同じ機序を深い履歴で追試する」役割に変わり、期待情報価値が上がる。

いずれも満たされない限り、Tier 1 のデータ取得・実装は行わない。

---

## 5. Phase 8 へ移る理由

Phase 3 と Phase 7 Tier 0 は、**独立に変えた2つの軸で同じ壁**に当たった。

```text
Phase 3 : 探索アルゴリズムを 3 通り変えた  → validation survivor 0/30 × 3
Phase 7 : 情報集合を OHLCV+Tier0 へ広げた → 方向 Y1 は 9/9 非有意、GO は方向を含まない
```

両者に共通して固定されていたのは **問題設定**である。

```text
「5分ごとに BTC の短期方向を当て、taker で往復 10bps を払って売買する」
```

Phase 3 closeout §4 の Interpretation は「コストの壁が探索空間全体を支配している」だった。
Tier 0 の Y1 十分位スプレッド(上限指標)が全 cell で 10bps 未満だったことは、
**情報を足してもこの壁の高さは変わらなかった**ことを意味する。

したがって次に変えるのは3つ目の軸、**問題設定**である。

- **捨てないもの**: 5分足データ、Judge、data contract、split 規約、manifest、
  Tier 0 の観測量、Phase 7 の全 artifact と negative result。
  5分足は今後も**観測・執行シミュレーション用**として維持する。
- **離れる対象**: 「5分ごとに短期方向を当て、taker で売買する」問題設定。

Phase 8 の探索方向は、こちらで新戦略を発明することではなく、
**2026-08-17 時点までの先行研究から、個人研究者が再実装・独立検証する価値の高い仮説を選ぶ**
ことである(Phase 8.0 = 文献探索と候補選定)。

- Phase 8.0 文献レビュー: [literature_review_2026-08-17](../phase8/literature_review_2026-08-17.md)
- 候補一覧と採点: [replication_candidates_v1](../phase8/replication_candidates_v1.md)
- 選定メモ: [phase8_selection_memo_v1](../phase8/phase8_selection_memo_v1.md)

---

## 6. この closeout が言っていないこと

1. **「Tier 0 に incremental information が無い」とは言っていない。** 検出された(§2.1)。
2. **「microstructure に情報が無い」とは言っていない。** Tier 1/2 は未検証(§3)。
3. **「GO 2件を棄却した」とは言っていない。** 保留である(§2.4)。
4. **「BTC に alpha が無い」「longer horizon にも無い」とは言っていない。**
   longer horizon は**まだ一度も検定していない**(Phase 8 の対象)。
5. **「Phase 8 の方が有望である」とは証明していない。** これは期待情報価値に基づく
   優先順位の決定であり、Phase 7 の否定でも Phase 8 の保証でもない。
6. **Final OOS の結果は何も分かっていない**(未開封)。

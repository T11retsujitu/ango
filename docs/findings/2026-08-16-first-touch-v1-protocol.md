# First-touch v1 — H6価格経路の一回限り確認検定

- 凍結日: 2026-08-16
- 状態: **Validation棄却・Final未開封・OHLCV方向探索終了**
- 目的: terminal returnでは戦略にならなかったH6について、イベント後の価格経路
  (TP/SL先着順)にコスト控除後エッジが残るかを一度だけ確認する。
- 非目的: 新しいOHLCVイベント探索、H6/レジーム閾値の調整、深いイベントv1の
  near-miss救済、主barrier失敗後の別barrier選択。

## 既知情報と検定上の位置づけ

33ヶ月の月次地形、H6 terminal return、レジーム分類器、深いイベントv1の結果は既知。
したがって完全にpristineなOOSではない。ただし、下記の固定イベントとbarrierについて
first-touch損益は未計算であり、v1内で結果閲覧後の変更を禁止する。

添付案の `z1>=3 ∧ vr>=3 ∧ range>=2 ∧ wick ∧ drift` は全期間raw 11件、wick/driftを
外した `z1>=3 ∧ vr>=3 ∧ range>=2` でも4h cooldown後186件しかなく、事前にN不足。
より深い条件の追加は行わず、十分なNを持つ既存H6だけをconfirmatory対象にする。

## 固定イベント集合

H6イベント足 `t`:

```text
return_5m > 0
AND volume_ratio_20 >= 3
AND (high-low) >= 2 × 直前20本の平均(high-low)
```

- 直前20本平均は現在足を含めず、20本完全に揃う場合だけ有効。
- シグナル確定はイベント足終了時 `t+5m`。
- entryは必ず次足 `t+5m` のopen。イベント足closeでの約定は禁止。
- 各時系列確認窓内でH6イベントを時刻順に走査し、採用済みイベント足との開始時刻差が
  4時間未満の後続eventを捨てる固定greedy cooldownを、価格pathを見る前に適用する。
  これはentry同士の時刻差を4時間以上にすることと同値である。窓境界でcooldownをresetし、
  全barrierで同じイベント集合を使う。
- 売買方向はshortのみ。
- timestampが正確な5分グリッドを外れるentry/pathは `invalid_gap` として除外する。

raw H6は4,449件。境界purge・固定cooldown後の事前件数はDevelopment 946、Validation 792、
Final 546。これは件数だけの事前監査であり、path/PnLは未閲覧。

## 時系列確認窓

| 窓 | 期間 | 用途 |
|---|---|---|
| Development | feature warmup後〜2024-12-31 23:55 UTC | 記述統計。採否には使わない |
| Validation | 2025-01-01 00:00〜2025-12-31 23:55 UTC | 固定主設定の第1確認 |
| Final | 2026-01-01 00:00〜データ末尾 | 固定主設定の第2確認 |

- eventだけでなく、entryから最大4h timeoutまでの利用時刻が同じ窓内に収まるものだけを使う。
- 境界前4時間をpurgeし、別窓のOHLC pathを一切ラベルへ混ぜない。
- まずDevelopment/Validationだけを固定実行する。Validationが下記の共通ゲートを満たす
  場合だけFinalを一度開く。Validation落ちならFinalを開かず棄却する。

## 固定barrier

主設定は **B3のみ**。他5設定は感応度であり、B3失敗を救済できない。

| ID | Stop | Take profit | Timeout | 役割 |
|---|---:|---:|---:|---|
| B1 | 15bps | 30bps | 1h | 感応度 |
| B2 | 20bps | 30bps | 1h | 感応度 |
| **B3** | **20bps** | **40bps** | **2h** | **主設定** |
| B4 | 30bps | 50bps | 2h | 感応度 |
| B5 | 30bps | 60bps | 4h | 感応度 |
| B6 | 40bps | 80bps | 4h | 感応度 |

- entry時刻を `e` とし、監視足は `[e,e+H)` のH/5分本、timeoutは `open(e+H)`。
- shortのTPは `entry×(1-TP)`、SLは `entry×(1+SL)`。
- 各足ではopen gapを先に判定する。SL gapはopenの不利な価格、TP gapはbarrier価格で約定。
- 同じ5分足でTP/SL両方へ到達した場合、主解析は**SL先着**。TP先着と曖昧足除外も
  bounds感応度として全件報告するが、悲観側で不成立なら昇格不可。
- Fundingは最大4h保有かつ長期OKXデータが無いため主計算へ入れない。この省略で救われる
  薄い結果は採用しない。

## コスト・経路・集計

- 全entry/exitをtaker扱い。往復10bps(feeのみ)、12bps(fee+slippage 2bps)、
  **15bps(stress、主判定)**を全報告。maker仮定での救済は禁止。
- 主指標: B3の15bps控除後mean PnL/取引。
- 補助: gross/net中央値、勝率、TP/SL/timeout率、保有時間、profit factor、最大逐次DD、
  月次成績、ambiguous率、固定horizon excursion。
- exit足内はtouch後の順序が不明なので、exit足全体のhigh/lowを取引中MFE/MAEに使わない。
- ISO UTC週をclusterとして20,000回pairs bootstrap(seed=20260816)し、週内tradeを全て保持した
  event-weighted meanの95% percentile CIを作る。
- leave-one-month-out(LOMO)を全月について報告する。

## Matched control

「barrierを置いたこと自体」とH6固有効果を分ける。accepted H6各eventに同一窓の
非H6時刻を1:1対応させる。同じUTC weekday/hour、事前20日実現ボラquartile、
`trail_abs_1h=sum(abs(return_5m),直近12本・現在足含む)` quartileを一致させる。quartile境界は
Developmentだけで一度固定。full 4h pathを要求し、全raw H6の±4h、control再利用、control同士の
4h未満重複を禁止する。候補のうち時刻距離最小、同距離なら過去側を選び、future outcomeを
使わない。coverage 80%未満はpass不可。同じB3を適用し、paired H6−control差を週cluster
bootstrapで報告する。

## 事前判定ルール

B3が次を**全て**満たす場合だけprovisional pass。それ以外はv1棄却。

1. Validation/Finalそれぞれ `n >= 150`、entryを含むUTC週cluster `>= 24`
2. ValidationとFinalの15bps後meanがともに `> 0`
3. Finalの週cluster bootstrap 95% CI下限が `> 0`
4. Validation/FinalそれぞれH6−matched control差が `> 0`、Finalの差の95% CI下限が `> 0`、
   control coverage `>= 80%`
5. Validation/FinalのLOMOが全月で `> 0`
6. 6 barrier中4つ以上がValidation/Finalの両方で15bps後プラス
7. ambiguousの悲観側(SL先着)で上記を満たす

passしても既知期間を使うため即戦略採用せず、将来のprospectiveデータで再確認する。
一項目でも不合格ならfirst-touch v1を棄却し、**OHLCVのみの方向アルファ探索を終了**する。
以後は約定フロー・BBO・板を使うマイクロストラクチャー研究へ移行する。

## 結果

固定仕様を `scripts/first_touch_v1.py validation` で一度実行した。FinalはValidation通過時
だけ実行可能なguardがあり、不通過のため候補別first-touchを計算していない。

### 主設定B3

| 窓 | n | gross平均 | 10bps後 | 12bps後 | **15bps後** | 週cluster 95% CI(15bps後) |
|---|---:|---:|---:|---:|---:|---:|
| Development | 946 | −0.40bps | −10.40 | −12.40 | **−15.40** | [−17.08, −13.71] |
| Validation | 792 | +0.24bps | −9.76 | −11.76 | **−14.76** | [−16.87, −12.74] |

ValidationのB3はTP 26.4%、SL 58.1%、timeout 15.5%、同足両到達1.6%。同足をすべて
TP先着とする非現実的な上限でも15bps後 −13.77bps、同足除外でも −14.42bpsで、
bar内順序では結論が変わらない。

### 感応度・対照・頑健性

- ValidationのB1〜B6は**全6設定が15bps後マイナス**。最良のB6でも −12.57bps、
  B3は −14.76bps。barrier台地は0/6。
- B3の全月LOMO最小値は −15.06bpsで、最良月依存を除いてもプラスにならない。
- matched control coverageは56.2%で事前80%基準未達。さらに対応できた445組でも
  H6−controlは **−4.03bps**、週cluster 95% CI [−7.52, −0.56]。H6は通常時shortより
  良いどころか有意に悪い。
- DevelopmentもB1〜B6全てマイナス、B3は15bps後 −15.40bpsで同じ構図。

### 機械判定

| Gate | 結果 |
|---|---|
| n≥150・週cluster≥24 | 通過 |
| B3 15bps後プラス | **不通過** |
| matched control | **不通過** |
| 全月LOMOプラス | **不通過** |
| 6設定中4設定以上プラス | **不通過(0/6)** |
| 悲観的ambiguous規則 | 適用済み。楽観側でも不成立 |

**判定: first-touch v1棄却。Finalを開かず、OHLCVのみの方向アルファ探索を終了する。**

terminal returnがゼロでも途中経路に利確機会が残る、という最後の穴を検証したが、B3の
grossはValidationで+0.24bpsにすぎず、taker feeだけで完全に消えた。今後、OHLCV条件・
barrier・レジームを追加して救済しない。研究対象を約定フロー・BBO・板へ移す。

再現物は `data/analysis/first_touch_v1/` に保存。Validation manifest SHA-256は
`f6ca6f9adff886f403cb7cdb8356e5b221e3c91230085e21f16b4d10db5b5836`。

独立監査ではnormalized OHLCVだけからH6とB3を再実装し、Validation 792件について
event/entry/exit時刻、価格、status、ambiguous、gross returnが全件一致した。なお、cooldownを
「採用entryから次のevent足開始まで4時間」と5分長く解釈した感応度では785件となるが、
15bps後は−14.69bpsで棄却判断は変わらない。

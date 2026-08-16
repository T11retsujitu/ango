# Phase 7 Tier 0 — Incremental Information Test 事前登録 v1.1

- 登録日: 2026-08-16(v1) / 改訂: 2026-08-16(v1.1)
- 状態: **凍結(ラベル未閲覧の状態で確定)**
- protocol id は v1 と同一(`phase7_tier0_screening_v1`)。**本文書が唯一の凍結文書**であり、
  v1 の文面は git 履歴にのみ残す(凍結文書を2つ並存させない = protocol shopping の防止)
- 上位設計: [information_space_expansion_v1](information_space_expansion_v1.md) §6
- データ契約: [tier0_ingest_v1](tier0_ingest_v1.md)(取り込み・品質確認は完了済み)
- 恒久ルール: [docs/findings/README.md](../findings/README.md)
- 統計手順の先例: [Microstructure v1 §7](../findings/2026-08-16-microstructure-v1-protocol.md)
- **機械可読な凍結仕様**: `src/mce/tier0_prereg.py`(protocol id `phase7_tier0_screening_v1`)。
  本文書の「変えてはいけない値」はコード側にも定数として固定してあり、
  `tests/test_tier0_prereg.py` が文書・定数・実データ列の三者を機械照合する。

> **この文書は効果量を一切見ない状態で書かれている。**
> 本文書の凍結後に初めてラベル(将来リターン)を生成し、検定を実行する。
> 実行後に本文書の定義・閾値・family・go/no-go を変更しない。変更が必要と判断した場合は
> v2 として別文書を作り、**その時点以降に開く窓**でのみ適用する。
>
> **改訂規則**: ラベルを1つも計算していない間の改訂は許される(査読で欠陥が見つかった場合など)。
> ただし改訂は必ず git 履歴に残し、**最初のラベル生成コマンドを実行した時点で凍結が確定**する。
> 以後の変更は v2 扱いとする。凍結確定時刻は screening artifact に記録する。

---

## 0. v1 → v1.1 の差分(独立監査の指摘による。ラベル未生成)

3レンズ(leakage / 統計妥当性 / 実現可能性)の独立監査で fatal 3件・major 多数が出たので、
**ラベルを1つも計算していない段階で**改訂した。family(27)・target の3本立て・窓・Holm・
コスト基準・封印は変更していない。

| # | 項目 | v1 | v1.1 | 理由 |
|---|---|---|---|---|
| 1 | 主帰無 | X 全体の巡回シフト | **A-projection placebo `Bp`**(§12.1) | 素朴シフトは `corr(X, A)` まで壊す。ridge では A と相関した列を足すだけで正則化の幾何が変わり当てはまりが改善しうる |
| 2 | baseline A | 19 列 | **27 列**(§3) | 交互作用の**二次項**・曜日構造・価格水準 z が A に無く、B だけが表現できた(strict nesting の穴) |
| 3 | flow の列 | `avg_trade_notional` | **`avg_trade_size`** + A に `z20d_log_close` | `log(avg_trade_notional) = log(VWAP) + log(avg_trade_size)`。z-score が価格水準を密輸し OHLCV 情報が flow の手柄になる |
| 4 | fold 初期学習 | 12ヶ月 | **6ヶ月** | T0-B2 の OOS が 2024 暦年のみになり「2暦年以上で正」が**原理的に満たせなかった** |
| 5 | placebo 最小シフト | 7 日 | **30 日** | 20日 z-score の記憶長より短いシフトは帰無が本物と部分整列する |
| 6 | placebo の alpha | 未規定 | **replicate ごとに内側 CV を再実行** | 観測 B の alpha を流用すると帰無だけ罰則不足になり反保守的 |
| 7 | 入力の裾 | クリップ禁止 | **標準化後に全列 ±10 対称クリップ**(A・X・placebo 同一) | z-score の極端な裾1本が二乗誤差の判定を支配しうる |
| 8 | test block の端 | purge 無し | **各 block 末尾 h+1 バーを purge** | block 間で target 窓が重なり leave-one-block-out が汚染される |
| 9 | 公開遅延 | 報告のみ | **+1バー遅延での頑健性を T0-B1/B2 の昇格必要条件**(§17) | metrics の公開遅延は未実測。5分バッファは仮定であって計測ではない |
| 10 | 変換由来の疑陽性 | 対策なし | **OHLCV-only sham 集合 `S0`**(§12.4)を T0-A の昇格条件に | 「新しいデータ」か「新しい変換」かを分離する対照が無かった |
| 11 | 機序の符号 | ridge 係数の符号 | **A 残差化した偏相関の符号**(§15) | 共線性下で多変量 ridge 係数の符号は同定されない |
| 12 | Y2 の区間 | close 基準 | **open 基準**(§5) | 保有区間から1バーずれていた |

---

## 1. 検定する仮説

情報集合 X ごとに独立に:

```text
H0: X は OHLCV baseline A を超える有用な incremental information を含まない
H1: X は再現可能な incremental information を含む
```

**成功条件は「儲かる戦略が見つかること」ではない。** 本フェーズは
「どの情報集合に、コスト上意味を持ちうる追加情報があるか」を測る screening である。

同時に、これは **OHLCV の限界の証明でもない**。Phase 3 の 0 survivor は
information set 以外の代替仮説(DSL 表現力・budget・target 設計・horizon とコストの不整合・
regime 差・searcher 能力)を否定していない([bakeoff summary §6](../findings/2026-08-16-phase3-bakeoff-summary-v1.md))。

---

## 2. データ(凍結済み・追加取得しない)

| 項目 | 値 |
|---|---|
| venue | Binance USDT-M perpetual(`futures/um`) |
| symbol | `BTCUSDT` |
| features | `data/features/binance_BTCUSDT_5m.parquet`(631,296 行 / 2020-01-01 〜 2025-12-31 / 5分足 UTC) |
| replication class | `cross_exchange_validation`(Judge を凍結してある OKX とは別 venue) |
| 封印 | `ts >= 2026-01-01` は venue を問わず開かない(Final OOS firewall の暦継承) |

**venue についての制約(結論の格上げ禁止)**: 価格系列は OKX と同一資産として整合する
(重なり 225,332 本で close 相対差 中央値 3.46bps・5分リターン相関 0.9983)が、
**出来高水準は約 2.2 倍違う**。したがって flow の絶対量は venue 間で移送できない。
本検定で得られる結論は「Binance で情報が存在するか」であり、
OKX 執行を前提とした主張へ直接は格上げしない(格上げは Tier 2 の prospective 検定で行う)。

---

## 3. Baseline information set A(凍結)

A は **stationary な派生 observable のみ**とする。生の価格水準・出来高水準は
2020〜2025 で桁が変わる非定常量なので **A にも X にも入れない**。

| A の列 | 由来 | availability |
|---|---|---|
| `return_5m`, `return_1h`, `volume_ratio_20`, `drift_20d`, `realized_vol_20d` | 既存 features のまま(5列) | close/start |
| `rv_12`, `rv_48` | 窓 `[t-12, t)` / `[t-48, t)` の5分対数リターン二乗和の平方根(**現在バーを含まない**) | start_of_bar |
| `hl_range_z20d` | `Z20d( (high-low)/close )` | close_of_bar |
| `log_volume_z20d` | `Z20d( log(volume+1) )`(**base 建て**。quote は使わない) | close_of_bar |
| `z20d_log_close` | `Z20d( log(close) )` — 価格水準ガード | close_of_bar |
| `norm_move_1` | `clip( return_5m / rv_12, -10, +10 )`(`rv_12` が 0/null なら null) | close_of_bar |
| `norm_move_1_sq` | `norm_move_1 ^ 2` | close_of_bar |
| `z20d_return_1h` | `Z20d( return_1h )` | close_of_bar |
| `z20d_return_1h_sq` | `z20d_return_1h ^ 2` | close_of_bar |
| `tod_sin_k`, `tod_cos_k`(k=1,2,3) | `sin/cos( 2*pi*k*(60*hour_utc + minute_mod_60)/1440 )` | start_of_bar |
| `dow_sin_k`, `dow_cos_k`(k=1,2) | `sin/cos( 2*pi*k*weekday_utc/7 )` | start_of_bar |
| `is_weekend`, `is_quarter_hour`, `is_hour_boundary` | `weekday_utc>=5` / `minute_mod_15==0` / `minute_mod_60==0` | start_of_bar |

A は 27 列。**A の定義は全 test で共通**であり、X ごとに変えない。

**交互作用の因子と*その二乗*を A に入れる理由(strict nesting の担保)**:
X 側の交互作用 `signed_imb * norm_move_1` は、`signed_imb` が同一バーの `return_5m` と
機械的に相関するため、A への線形射影が

```text
E[signed_imb | A] * norm_move_1  ~  a * norm_move_1 + b * norm_move_1^2 + ...
```

を含む。**`norm_move_1^2` が A に無いと、この二次成分は A-projection placebo でも消えない**
(線形射影の残差 E 側に落ちてシフトで壊れる一方、観測 B は保持する)。しかも
`norm_move_1^2` はボラ系 target の素直な予測子なので、そのまま偽陽性になる。
`norm_move_1` / `z20d_return_1h` とその**二乗**を A に入れることで、
B の増分は純粋な交差項だけになる。

**曜日ハーモニクスを入れる理由**: placebo は日単位シフトなので曜日整列は壊れる。
A が曜日構造を持たないと、X の活動量 z-score が曜日プロファイルの代理として
`dR2 > 0` を作れてしまう。

**`z20d_log_close` を入れる理由**: 価格水準の 20日 z-score(平均回帰の代理)を
X 側の列だけが単独で運ぶ状態を作らない。

`rv_12` / `rv_48` の窓が**現在バーを含まない**のは意図的である。含めてしまうと
`norm_move_1 = return_5m / rv_12` の分母に分子が入り、値が構造的に `[-1, +1]` へ
押し込まれて「直近ボラに対して今どれだけ動いたか」という意味を失う
(`realized_vol_20d` の左閉右開規約と同じ扱い)。

**時刻ハーモニクスを3次まで入れる理由**: X 側の参加量(`trade_count` の z-score)は
日内の活動プロファイルを細かく表現できるのに対し、A が1次ハーモニクスしか持たないと、
Y2(ボラ)の `dR2` が「X の情報」ではなく「時間帯の形」を拾ってしまう。
A 側の時刻表現を先に厚くしておく。

**A を意図的に強くしてある**理由: target に volatility 系(Y2)・path 系(Y3)を含むため、
「直近のボラを A が持っていない」状態で X(約定件数など)を足せば、
*活動量 ≈ ボラ* という自明な関係だけで `dR2 > 0` が出てしまう。
`rv_12` / `rv_48` / `hl_range_z20d` / `volume_ratio_20` を A に入れることで、
X はそれらを超える増分を示さなければならない(有利な baseline を選ばない — protocol §6.1)。
既存 features の `realized_vol_20d` は20日窓なので、短期ボラの代理としては不十分である。

### Z 変換(唯一許可する派生変換・凍結)

```text
Z20d(x)_t = (x_t - mean(x over [t-20d, t))) / std(x over [t-20d, t))
```

- 窓は **左閉右開**(現在バーを含まない)。`realized_vol_20d` と同じ規約。
- 窓内の有効本数が窓の 90%(= 5,184 本)未満なら null。
- `std == 0` なら null(0除算を作らない)。
- `close_of_bar` 列の Z20d は close_of_bar、`start_of_bar` 列の Z20d は start_of_bar。

**これ以外の変換(対数以外の非線形化、交互作用項、ラグ展開、PCA、外れ値クリップ)を
実行時に追加しない。**

### 変換規約(A と X の**両方**に適用。凍結)

| 規約 | 値 |
|---|---|
| Z20d の窓完全性 | 20日窓に有効 5,184 本(90%)未満なら null |
| `rv_12` / `rv_48` | 窓が完全(12本 / 48本)でなければ null |
| ラグ・差分 | 行シフトではなく **ts 完全一致 join**(欠損バーを跨がない) |
| 0除算 | 分母が 0 または null なら null(無限大を作らない) |
| clip(生成時) | `norm_move_1` の ±10 のみ |
| clip(標準化後) | **全列を ±10 で対称クリップ**(A・X・placebo に同一適用)。z-score の極端な裾1本が二乗誤差の判定を支配するのを防ぐ |
| 有限性 | モデル行列に `isfinite` でない値があれば **quality failure**(実行を止める。黙って落とさない) |

**これらの床は X 側の Z20d(`z20d_signed_imb` など12列)にも同じく効く。**
床を A 側だけに書くと、X の有効行が過大評価される(実現可能性監査の指摘)。

---

## 4. 候補 information set X(凍結)

X は4つ。**まとめて1つの巨大モデルへ投入しない**(protocol §5)。

| id | 列(変換後) | 元列 | mechanism |
|---|---|---|---|
| **T0-A** flow | `signed_imb`, `Z20d(signed_imb)`, `Z20d(log(trade_count))`, `Z20d(log(avg_trade_size))`, `signed_imb * norm_move_1` | taker_buy_ratio / trade_count / avg_trade_size | aggressive-flow continuation vs absorption、参加者数と平均約定サイズの構造 |
| **T0-B1** OI | `dlog_oi_12`, `Z20d(dlog_oi_12)`, `Z20d(log(open_interest))`, `dlog_oi_12 * Z20d(return_1h)` | open_interest | 建玉の積み上がりと解消(crowded unwind) |
| **T0-B2** positioning | `Z20d(log(top_trader_position_ls_ratio))`, `Z20d(log(global_account_ls_ratio))`, `Z20d(log(taker_ls_vol_ratio))` | long/short ratio 3種 | 群衆ポジションの偏りと taker 方向 |
| **T0-C** basis | `premium_close`, `Z20d(premium_close)`, `dprem_12` | premium_close | perp/index premium = レバレッジ需要・squeeze 前兆 |

```text
signed_imb(t)   = 2 * taker_buy_ratio(t) - 1          # -1(全 sell)〜 +1(全 buy)
norm_move_1(t)  = return_5m(t) / rv_12(t)             # ボラ正規化した当該バーの値動き
dlog_oi_12(t)   = log(open_interest_t) - log(open_interest_{t-12})   # 1時間差分・ts完全一致join
dprem_12(t)     = premium_close_t - premium_close_{t-12}
avg_trade_size  = volume / trades                     # 価格を含まない(base 建て)
```

### 交互作用項を事前登録する理由(重要)

**absorption と continuation は線形項では区別できない。** 「大きな買い方向の
aggressive flow があったのに価格がほとんど動かない」= 吸収、「動いた」= 継続、であり、
これは flow × 値動き の**交互作用**である。線形モデルに交互作用を後から足すのは
実行時の自由度になるので、`signed_imb * norm_move_1`(T0-A)と
`dlog_oi_12 * Z20d(return_1h)`(T0-B1、価格 × 建玉変化 = build-up / short cover の区別)を
**事前に固定して X 側へ入れる**。

交互作用は A の情報(`norm_move_1`, `z20d_return_1h`)を含む。これが
「B だけが baseline の非線形関数を表現できる」抜け穴にならないよう、
**両因子を A 側にも入れて strict nesting を担保する**(§3)。
そのうえで主帰無 Bp(§12.1)が **A で説明できる成分を保存したまま残差だけを壊す**ので、
残る抜け穴(交互作用と A の共線性、ridge の正則化幾何の変化)も帰無分布へ吸収される。
二重の防御である。

- 差分は行シフトではなく **ts 完全一致 join**(欠損バーを跨いだ差分を作らない)。
- **除外列と理由**(実行時に「やっぱり入れる」ことを禁止):

| 列 | 理由 |
|---|---|
| `taker_buy_quote_ratio` | `taker_buy_ratio` と実質同一 |
| `avg_trade_notional` | `log(avg_trade_notional) = log(VWAP) + log(avg_trade_size)`。20日 z-score が**価格水準を密輸**し、OHLCV 情報が flow の手柄になる。価格を含まない `avg_trade_size = volume / trades` を採る |
| `open_interest_value` | `open_interest × price` とほぼ共線 |
| `top_trader_account_ls_ratio` | `top_trader_position_ls_ratio` と同族(position 版を採用) |
| `premium_open` | `premium_close` と同一系列の start_of_bar 版(片方のみ使う) |
- 各 X の列数は **3〜5**(T0-A 5 / T0-B1 4 / T0-B2 3 / T0-C 3)。
  集合間で自由度が揃っていないが、**各集合は自分の placebo 帰無に対してのみ判定される**ので
  比較の公平性は損なわれない(集合間の `dR2` 比較は §7 で禁止済み)。

---

## 5. Target(凍結。方向符号だけにしない)

すべて **執行整合**:シグナルはバー `t` の close 後、entry は `open[t+1]`。
`ts` 完全一致 join で計算し、欠損バーを跨いだ値は作らない。

| id | 定義 | 意味 | 使い道 |
|---|---|---|---|
| **Y1** direction | `open[t+1+h] / open[t+1] - 1` | 符号つきリターン | trade / abstain・方向 |
| **Y2** volatility | `log(1 + rv_fwd_h)`、`rv_fwd_h = sqrt( sum_{i=1..h} ( log open[t+1+i] - log open[t+i] )^2 )` | 保有区間の実現ボラ | サイジング・ストップ幅・期待移動幅 |
| **Y3** adverse path | `min_{i=1..h} ( low[t+i] / open[t+1] ) - 1` | long entry 後の最大逆行(MAE) | ストップ幅・abstain |

- Y1/Y2/Y3 とも連続量として扱う(符号の二値化はしない。二値化は情報を捨てる上に
  閾値という自由度を増やすため)。
- Y2 は右に歪むので `log(1 + rv)` 空間で回帰・評価する(A・B・placebo すべて同じ空間)。
- **Y2 は open 基準で定義する**。`open[t+1] -> open[t+1+h]` は Y1 の保有区間そのものなので、
  close 基準にすると窓が1バーずれる(監査指摘)。Y1/Y2/Y3 の3つすべてを
  `open[t+1]` を起点とする同一区間に揃えた。
- Y3 は long 目線の MAE。short 目線(`max high / open[t+1] - 1`)は **v1 では計算しない**
  (family を増やさない)。bar 内の経路順序は分からないので、Y3 は経路の順序を問わない
  下限値として扱う(first-touch 問題、§22)。
- 3 target のうち Y2 と Y3 は方向に依らない量であり、Phase 3 で死んだ「方向符号のみ」の
  枠組みを避けるための本命である。既存 findings で 33ヶ月生存したのは
  ボラ・流動性構造(H1–H4)であり、Tier 0 情報が最も乗りやすいのもこの軸だと考える。

### 検討したが採用しなかった target(記録)

**Y5 = `1[ |Y1| > 10bps ]`(その h の間に往復コストを超える動きが出るか)** は、
abstain 判断に最も直結する target であり、採用を検討した。**採用しなかった理由は
検出力ではなく p 解像度の算術**である:

```text
target 4 個にすると family = 36、Holm の最小閾値 = 0.05/36 = 1.389e-3
dev T0-B2 の全数 placebo の最小 p = 1/719          = 1.391e-3  >  1.389e-3
→ 最短 dev 窓の cell が、全数 placebo でも最小閾値へ届かなくなる
```

つまり **family を 4 target へ広げると、T0-B2 の cell は原理的に有意になれない**。
「検定できない cell を family に入れる」ことになるので v1 では 3 target に留める。
Y5 は cost relevance(§16)の中で**記述統計として**報告し、仮説検定はしない。
将来 T0-B2 の被覆が伸びて窓を長くできれば v2 で target に昇格させる。

---

## 6. Horizon(mechanism 別に凍結)

| information set | horizon h(バー) | 根拠 |
|---|---|---|
| T0-A flow | 1, 3, 12(5分 / 15分 / 1時間) | 約定フローの効果は短命という先行研究の主張を、短い側から順に見る |
| T0-B1 OI | 12, 48(1時間 / 4時間) | 建玉の積み上がり・解消はバー単位では動かない |
| T0-B2 positioning | 12, 48 | 同上 |
| T0-C basis | 12, 48 | 同上 |

→ (set, h) の組み合わせは **9**。実行後に horizon を追加・削除しない。

---

## 7. 有効サンプル定義(列ごとの欠測差の扱い)

**測定済みの被覆は列によって大きく違う**([tier0_ingest_v1 §9](tier0_ingest_v1.md))。
特に long/short ratio 系は **2022年に大きな欠測ブロック**がある(月次被覆:
top trader は 2022-02〜04 と 07〜11 がほぼ 0、taker L/S は 2022-01〜04 がほぼ 0)。

素朴な complete-case 抽出は **「2022年(下落・デレバレッジ局面)を黙って落とす」**
という regime 選択になる。これを避けるため、集合ごとに **窓を先に固定**する。

| information set | 検定窓(dev) | 確認窓(confirmation) | 根拠(ラベル非依存) |
|---|---|---|---|
| T0-A / T0-B1 / T0-C | `2021-01-01 .. 2024-12-31` | `2025-01-01 .. 2025-12-31` | 当該窓での被覆 0.995〜1.000 |
| T0-B2 | `2023-01-01 .. 2024-12-31` | `2025-01-01 .. 2025-12-31` | 2022年の欠測ブロックを**期間として明示的に除外**(complete-case で暗黙に落とさない) |

2020年は metrics 未公開(2020-09 開始)かつ baseline の 20日 warmup が必要なため、
**全集合で 2021-01-01 を開始点**とする(集合間で開始点を変えない)。

### 有効行の定義(凍結)

1つの test = (information set, target, horizon) について:

```text
valid(t) = A の全列が非 null
         ∧ その X の全列が非 null
         ∧ target Y(t, h) が非 null
         ∧ t が当該窓に含まれる
```

- **A と B は完全に同一の行集合で学習・評価する**(片方だけ行が増えることを禁止)。
- この行集合は **cell のあらゆる段階を支配する**: outer 学習・内側 CV の学習と検証・
  outer テスト・confirmation のすべてで同一であり、A / B / placebo すべてに同じものを使う。
- 月次被覆が 95% 未満の暦月は、その test の**ブロックごと除外**し、除外した月を報告する
  (行単位の穴で fold の一部だけが薄くなることを避ける)。
- 除外・欠測の集計は必ず report に出す。「都合の良い日だけ残す」操作はしない。

### warm-up buffer(実効開始点)

`Z20d` と `realized_vol_20d` / `drift_20d` は 20日の過去窓を必要とする。
窓開始の 20日前(および `rv_48` の 4時間前)のバーは **causal 窓を埋めるためだけに読み**、
**学習行にも評価行にもしない**。したがって各集合の実効開始点は
「窓開始 + 20日」であり、report にはこの実効開始点を記載する。

### 集合間比較の禁止

集合ごとに窓と有効行が異なる(T0-B2 だけ 2年)。したがって
**`dR2` の大きさを集合間で比較して順位付けしない**。各集合は自分の placebo 帰無に対してのみ
判定される。集合間の比較は「どれが GO したか」という質的な形でのみ行う。

### 窓の端と封印境界(leakage 防止・重要)

- target `Y(t, h)` は `t+1 .. t+1+h` のバーを読む。したがって **target 窓が当該窓から
  はみ出す行は、その窓の有効行に含めない**(dev の末尾行が confirmation を覗かない)。
- **封印境界の絶対規則**: いかなる target も `ts >= 2026-01-01` のバーを読んではならない。
  features には封印期間の行が存在しない(取り込み時に落としてある)ので、
  該当行の target は構造的に null になり自動的に除外される。これは意図された挙動であり、
  「データが足りない」として補完してはならない。

### 実測サンプル(**変換後の列**で計測。ラベル非依存)

生の列の被覆ではなく、**§3–§4 の変換を実装して**(Z20d の 90% 窓完全性、`rv` の完全窓、
1時間ラグの ts 完全一致 join を含む)数えた値。生の被覆から見積もると過大評価になる。
**判定に効くのは pooled OOS 行数**(expanding fold なので最初の6ヶ月は学習専用)。

| pooled OOS 窓 | 全バー | T0-A | T0-B1 | T0-B2 | T0-C |
|---|---:|---:|---:|---:|---:|
| dev21 = 2021Q3〜2024Q4(1,280 日) | 368,640 | 368,590 | 367,308 | (265,408) | 361,387 |
| dev23 = 2023Q3〜2024Q4(550 日) | 158,400 | — | — | **158,096** | — |
| conf = 2025Q1〜Q4(365 日) | 105,120 | 105,115 | 105,064 | 105,097 | 105,120 |

括弧つきの値は「T0-B2 を 2021 開始にした場合」の参考(925 日しか残らない)。
**T0-B2 の検定窓は 2023 開始**である。

実測 `n_eff`(= pooled OOS 行数 / h):

| cell | h=1 | h=3 | h=12 | h=48 |
|---|---:|---:|---:|---:|
| T0-A(dev) | 368,590 | 122,863 | 30,715 | — |
| T0-B1(dev) | — | — | 30,609 | 7,652 |
| T0-B2(dev) | — | — | 13,174 | **3,293** |
| T0-C(dev) | — | — | 30,115 | 7,528 |
| confirmation | 105,115 | 35,038 | 8,759 | 2,189 |

**最小 cell は T0-B2 h=48 の 3,293**(dev)/ **2,189**(confirmation)。
どちらも閾値(dev 2,000 / conf 500)を満たす。初期学習を 6ヶ月にしたことで
v1 の 2,196 から 50% 改善した。

---

## 8. 最低サンプル要件(凍結)

各 test で以下を**全て**満たさない場合、その test は「edge なし」ではなく
**検定不能(insufficient sample)**として記録する(p=1 として family には残す)。

```text
n_eff = (pooled OOS 評価行数) / h      # 学習行ではなく「評価に使った行」で数える

dev:          n_eff >= 2,000  かつ  有効 UTC 日数 >= 200
confirmation: n_eff >=   500  かつ  有効 UTC 日数 >= 100
```

`n_eff` は重複リターンの実効N補正(恒久ルール4)。h バー先までの target は
h バー分重複するため、素の行数を有意性の分母にしない。

**評価行は fold のテストブロックだけ**である点に注意する(expanding fold なので
最初の12ヶ月は学習専用)。変換後の列で計測した実測 n_eff は §7 の表を参照。

**最小 cell は T0-B2 h=48 の n_eff = 3,293(dev)**。閾値 2,000 は「小さい cell を落とさない代わりに、検出力の差を必ず報告する」
という設計である(§22-4)。閾値を結果を見てから動かさない。

---

## 9. Split と walk-forward(凍結)

### 9.1 段階

```text
dev 窓        : 探索と判定(walk-forward・複数 fold)
confirmation  : 昇格候補のみ・1回だけ(fold 機構は dev と同一)
final_oos     : 開かない(2026-01-01 以降は封印継続)
```

**confirmation 窓は dev の結果を全て記録・保存した後に一度だけ開く。**
confirmation を見てから dev の設定を変えない。

### 9.2 fold(expanding window)

- 初期学習期間 = **6ヶ月**、テストブロック = 3ヶ月、expanding(学習は窓の先頭から)。
- dev 2021-2024 → テストブロックは 2021Q3 〜 2024Q4 の **14 ブロック**。
- dev 2023-2024 → テストブロックは 2023Q3 〜 2024Q4 の **6 ブロック**。

初期学習を 12ヶ月から 6ヶ月へ縮めたのは、**12ヶ月だと T0-B2 の OOS が 2024 暦年だけになり、
§15-4「2 暦年以上で `dR2 > 0`」が原理的に満たせなくなる**ため(監査指摘)。
6ヶ月なら T0-B2 の OOS は 2023H2 + 2024 の2暦年にまたがる。
- confirmation も **同一の fold 機構**を使う(2025Q1〜Q4 の 4 ブロック、学習は
  dev 開始からの expanding)。dev と confirmation で手順を変えないことで、
  「窓が変わったのか手順が変わったのか分からない」交絡を作らない。

### 9.3 purge / embargo(凍結)

- 学習集合から、target 窓 `[t+1, t+1+h]` がテストブロック開始時刻以降に及ぶ行を **purge**。
- さらに **embargo = 288 バー(1日)** をテストブロック開始前から除外する。
- **各テストブロックの末尾 `h+1` バーも評価から外す**。外さないと、そのブロックの
  最後の行の target が次ブロックへ食い込み、fold 間が重なって
  leave-one-block-out(§15-3)が汚染される(監査指摘)。
- 標準化・z-score・ハイパーパラメータ選択は **学習集合のみ**で決める(テスト期間の
  統計量を一切使わない)。Z20d は元々左閉右開の因果変換なので、これに加えて
  モデル入力の標準化(平均・分散)を学習集合で固定する。

---

## 10. Estimator(capacity を A と B で一致させる。凍結)

| 項目 | 値 |
|---|---|
| モデル | Ridge 回帰(線形・閉形式)。target ごとに独立 |
| 入力標準化 | 学習集合の平均・標準偏差で z 化(テストには学習側の値を適用) |
| 正則化 | `alpha ∈ {0.1, 1, 10, 100, 1000}`(標準化後)。**A と B で同一グリッド** |
| alpha 選択 | 各 fold の**学習集合内**で、purge 付きの内側 walk-forward CV(3分割)により選択 |
| 標準化の異常 | 学習集合で `std == 0` の列があれば、その fold は **quality failure**(黙って列を落とさない) |
| 標準化後のクリップ | 全列 ±10 で対称クリップ(A・B・placebo に同一適用) |
| placebo の alpha | **replicate ごとに内側 CV をやり直す**。観測 B の alpha を流用すると帰無だけ罰則不足になり反保守的(監査指摘) |
| 乱数 | 使用しない(閉形式)。fold・purge・placebo の乱数種は §12 |
| 非線形モデル | **v1 では使わない**(capacity 差を information value と誤認しないため) |

線形に限定するのは意図的な保守側の選択である。「線形で見えない情報は存在しない」とは
主張せず、**v1 の結論は "線形・この変換の下で" という限定つき**であることを報告に明記する。

---

## 11. 効果量(凍結)

主指標:

```text
dR2 = R2_oos(B) - R2_oos(A)
```

- `R2_oos = 1 - SSE / SST`。**SST の基準予測は当該 fold の学習集合平均**
  (テスト期間の平均を使わない = 未来情報を使わない)。
- 全テストブロックの OOS 予測を連結してから1つの `R2_oos` を出す(fold 平均ではない)。

副指標(同時に必ず報告。単独では判定に使わない):

- `dIC` = Pearson corr(prediction, target) の B − A
- `dIC_spearman` = Spearman 版
- fold 別 `dR2`(安定性の判定に使う)
- 主要 X 列の係数の符号(mechanism validation に使う)

---

## 12. Placebo(capacity 対照。凍結)

「列が増えたこと自体」の効果を引くため、偽の X で同じパイプラインを丸ごと再実行する。
**帰無の作り方が本検定の核心**なので、2種類を明確に区別する。

### 12.0 なぜ素朴な巡回シフトでは足りないか(統計監査の指摘・fatal)

X を丸ごとシフトすると、**`corr(X, A)` まで壊れてしまう**。本物の X は A と相関を持つ
(例: `signed_imb` は同一バーの `return_5m` と機械的に相関し、交互作用項は A の列と
共線に近い)。ridge では、**A と相関した列を足すこと自体**が正則化の幾何を変え、
「A の方向の実効的な縮小量が変わる」だけで OOS の当てはまりが改善しうる。
これは **X の情報ではない**。しかし素朴シフト帰無ではその成分が再現されないため、
帰無分布が低い側に寄り、**偽陽性を生む**。

そこで、H0 が許すもの(A で説明できる成分)は保存し、H1 が主張するもの
(A で説明できない残差の時刻対応)だけを壊す帰無を主とする。

### 12.1 主帰無 Bp — A-projection placebo(**判定はこれだけを使う**)

```text
各 outer fold について、学習行のみで  Gamma_hat = argmin_G || X - A * G ||_F^2
残差            E = X - A * Gamma_hat        (学習で得た Gamma_hat を fold 全行へ適用)
placebo_d の X  X_p(d) = A * Gamma_hat + shift_d(E)
許容シフト集合  S = { 30, 31, ..., W_days - 30 }   (W_days = その段階の窓の暦日数)
```

- `A * Gamma_hat`(A で説明できる成分)は**そのまま残す** → `corr(X_p, A)` は本物と同じ。
- 壊すのは **残差 E の時刻対応だけ** → H1 が主張する「A を超える情報」だけが消える。
- `Gamma_hat` は**学習行のみ**で推定する(テスト期間の統計量を使わない)。
- 残差の自己相関・列間相関はシフトで保存される。

### 12.2 副次帰無 Bt — 素朴な巡回シフト(報告のみ)

X ブロックをそのままシフトしたもの。`corr(X, A)` を壊すので **Bp より甘い(反保守的)**。
両方を報告し、`Bp` と `Bt` で結論が食い違う場合はその事実を明記する。
**昇格判定に使うのは Bp のみ。**

### 12.3 共通規則

- **A は動かさない**。動かすのは X 側(Bp では残差 E、Bt では X 全体)のみ。
- 日単位シフトなので、日内周期構造(hour 効果)と曜日構造は壊さず、
  Y との時刻対応だけを壊す。これが H0 そのものである。
- **シフトはその段階の窓の内側で閉じる**(巡回シフト)。dev 段階の placebo が
  confirmation 窓の X を読むことは無い。
- **シフト後に null になる行の扱い(凍結)**: placebo ごとに有効行を
  `S_d = S ∩ {シフト後の残差(Bt では X)が全て非 null}` として再計算し、
  **その `S_d` の上で A と B を両方とも再評価**して `dR2_placebo` を作る。
  欠測を中央値などで埋めない(埋めると placebo だけ情報が薄まり、検定が甘くなる)。
  `S_d` の行数も記録する。
- **最小シフトは 30 日**。20日 z-score の記憶長より短いシフトでは、帰無側の残差が
  本物と部分的に整列してしまう(監査指摘)。30 日 > 20日窓 + 最長 horizon(4時間)。
- **シフト群は有限**である。`|S| = W_days - 59` を超える独立な placebo は作れないので、
  「K を好きなだけ増やす」ことはできない。したがって次の2段階とする。

| 段階 | 使うシフト | K | 最小 p |
|---|---|---:|---:|
| 第1段階(全 27 test) | `S` を決定的に等間隔サンプルした 200 個 | 200 | 4.98e-3 |
| 第2段階(昇格候補のみ) | **`S` を全数**(exhaustive randomization) | dev A/B1/C: 1,402 / dev B2: 672 / conf: 306 | 7.13e-4 / 1.49e-3 / 3.26e-3 |

- 第2段階へ進む条件は **第1段階の順位のみ**: `#{placebo >= obs} <= 5`
  (= 第1段階 p `<= 6/201 = 0.0299`)。効果量の大きさを見て決めない。
- 第2段階を実施した test の p は**第2段階の値**を採用する(解像度が高い方が正しい)。
  実施しなかった test は第1段階の p をそのまま使う。これは基準の緩和ではなく
  **解像度の調整**であり、単調な関係を壊さない。
- p 値(片側・add-one 規則、Microstructure v1 §7 と同じ)。**主 p は Bp から作る**:

```text
p = (1 + #{d in 使用したシフト集合 : dR2_placebo(d) >= dR2_obs}) / (1 + K)
```

- **MDE(最小検出可能効果)を全 cell で報告する**: `MDE = placebo 分布(Bp)の 95 パーセンタイル`。
  null だった cell についても必ず出す。「効果が無かった」ではなく
  **「MDE より大きい効果は無かった」**としか言えないことを、数値で示すため。

- シフトの選択は決定的(乱数を使わない)。種 dev=`20260817` / conf=`20260818` は
  bootstrap 用であり、placebo の再現性は「等間隔規則 + 全数」で担保する。

**解像度が Holm 閾値に届くことの事前確認**(この確認自体が事前登録の一部):

```text
Holm の最小閾値 = 0.05 / 27 = 1.852e-3
dev A/B1/C  最小 p = 1 / 1,403 = 7.13e-4   <= 1.852e-3   OK
dev B2      最小 p = 1 /   673 = 1.486e-3  <= 1.852e-3   OK(余裕は小さい)
```

第1段階だけでは最小閾値に届かないので、**第2段階は Holm 判定に到達するための必須手順**である。
confirmation の p は報告するが、昇格判定は §17-4(符号一致と大きさ)で行うので、
confirmation 側の p 解像度は判定を左右しない。

### 12.4 OHLCV-only sham 集合 S0(T0-A の対照・凍結)

「**新しいデータ**が効いたのか、**新しい変換の形**が効いたのか」を分離するため、
T0-A と同じ 5 列構成の OHLCV 由来 sham を事前登録する(いずれも因果・A には含まれない):

```text
s01 = clip( rv_12 / rv_48 - 1, -10, +10 )                       # ボラ比(水準ではなく比)
s02 = Z20d( 窓 [t-12, t) の (high-low)/close の平均 )
s03 = norm_move_1 の 1 バーラグ(ts 完全一致 join)
s04 = norm_move_1 の 2 バーラグ(ts 完全一致 join)
s05 = ( volume_ratio_20 の 20日窓内パーセンタイル順位 ) - 0.5
```

- S0 は **family の一員ではなく p 値を持たない**(placebo と同じく対照)。
- **T0-A の昇格条件**として、同一行集合・同一 pipeline で `dR2(T0-A) > dR2(S0)` を要求する。
- 他の集合では報告のみ。S0 が有意な `dR2` を出したら、それは pipeline が
  「もっともらしい列を足すだけで改善する」ことの証拠であり、**全結果の解釈をやり直す**。

---

## 13. 推論と信頼区間(凍結)

- **主 p 値は §12 の placebo 分布**から得る(パラメトリック検定を使わない)。
  placebo は capacity 差・系列相関・X×A 交互作用の混入を同時に処理できる唯一の帰無なので、
  昇格判定はこれだけを使う。
- **副次 p 値**として、UTC 日ごとの `dSSE` 寄与に Rademacher 符号を掛ける
  **day-cluster randomization**(20,000 反復、add-one 規則)も報告する。
  これは再学習を伴わないので解像度が高いが、capacity 差を統制しないため
  **判定には使わない**(placebo と食い違った場合はその事実を記録する)。
- `dR2` の 95% 信頼区間は **日クラスタ bootstrap**(UTC 日単位で日をリサンプル、
  20,000 反復、種 dev=`20260817` / conf=`20260818`)で出す。
  日をまるごと残すことで、日内の系列相関と重複 target を保存する。
- 実効N(§8)を必ず併記する。

---

## 14. Multiple testing family(凍結・列挙)

family は **(information set, horizon) 9 × target 3 = 27 test**。

| set | h | target | 数 |
|---|---|---|---|
| T0-A | 1, 3, 12 | Y1, Y2, Y3 | 9 |
| T0-B1 | 12, 48 | Y1, Y2, Y3 | 6 |
| T0-B2 | 12, 48 | Y1, Y2, Y3 | 6 |
| T0-C | 12, 48 | Y1, Y2, Y3 | 6 |
| **合計** | | | **27** |

- 主判定: **Holm 補正・family-wise alpha = 0.05**(family size 27 固定)。
- 検定不能(§8 不通過)・未実施の test も **p = 1 として family に残す**
  (family を縮めて有意にしない)。
- 副次判定として BH(FDR q = 0.10)も**同時に**報告する。BH は screening 的な
  読み方のためであり、**昇格判定は Holm を使う**。どちらを使うかを結果を見てから選ばない。
- placebo 実行は family に数えない(帰無分布の生成であって仮説検定ではない)。
- confirmation 段階は **昇格した test だけの新しい family** として Holm 補正した p を
  **報告する**。ただし GO の判定条件は §17-4(符号一致と大きさ)であって
  confirmation の p ではない。confirmation 窓は 365 日しかなく、
  巡回シフト群が `|S| = 352` に制限される(最小 p = 2.83e-3)ため、
  昇格 test が多いと Holm の最小閾値へ届かないことがある。
  **p 解像度の制約が判定を左右しないように、判定は再現性(符号と大きさ)で行う。**

---

## 15. Mechanism validation(凍結)

統計的に有意でも、機序と整合しない結果は昇格させない。各 test について:

1. **符号の安定性**: 主要 X 列の符号が dev の fold の **75% 以上**で一致すること。
   符号は **ridge 係数ではなく、A 残差化した偏相関**で測る:
   `corr( x_j - proj_A(x_j) , y - yhat_A )`(いずれも fold の学習行で作った射影・モデルを
   OOS 行へ適用)。共線性の下で多変量 ridge 係数の符号は同定されないため(監査指摘)。
2. **効果の安定性**: fold 別 `dR2 > 0` が dev の **75% 以上**の fold で成立すること
   (12 fold なら 9 以上、4 fold なら 3 以上)。
3. **単一ブロック依存でないこと**: fold を1つずつ除いた **leave-one-block-out の
   pooled `dR2` が全て正**であること(Microstructure v1 §8-5 と同じ規律。
   1四半期が結果を担いでいないことの確認)。
4. **単一 regime 依存でないこと**: dev を暦年で分割し、**2年以上**で `dR2 > 0` であること
   (T0-B2 は dev が2年なので「両年で正」を要求する)。
5. **副指標との整合**: pooled `dIC`(Pearson)の符号が `dR2` の符号と一致すること。
   `dR2 > 0` なのに `dIC <= 0` なら、当てはまりの改善が予測の向きと結びついていない。
6. **単一日が担いでいないこと**: `dSSE` への寄与が最大の UTC 日を1日だけ除いても
   `dR2 > 0` が保たれること(1日のイベントで結論が決まる事態を防ぐ)。

補足: 符号の**方向そのもの**は事前登録しない(flow は continuation とも absorption とも
解釈でき、事前に一方へ賭ける根拠が無いため)。代わりに dev で観測した符号を**記録・固定**し、
confirmation で同じ符号が出るかを見る(§17-4)。

さらに、GO した test については **列ごとの leave-one-out 寄与**(その列だけ落として
再学習したときの `dR2` の減少)を報告し、「どの観測量が効いたか」を明示する。
これは判定には使わず、機序の特定と次段階(Tier 1)の設計入力に使う。

---

## 16. Cost relevance(凍結)

**情報の存在**と**取引価値**を分けて評価する。有意性は §12–14 で、取引価値はここで。

Y1(方向)について、B モデルの OOS 予測を十分位に分け:

```text
edge_bps = ( mean(Y1 | 予測 top decile) - mean(Y1 | 予測 bottom decile) ) / 2 * 10,000
```

- 対象は **pooled OOS 予測**(dev と confirmation それぞれ)。in-sample では計算しない。
- **上位十分位(long 側)と下位十分位(short 側)を別々に報告する**。片側だけで
  spread が作られている場合、それは「方向情報」ではなく片側のドリフト適合でありうる
  (Phase 3 Arm A の long-only drift-fit と同じ失敗様式)。
  weekday / weekend 別の内訳も出す。
- 比較基準: OKX taker 往復 **10bps**、stress **15bps**(恒久ルール5・既存 findings と同一基準)。
- `edge_bps` と break-even cost を必ず報告する。**コスト未満の効果は「統計的事実」であって
  「エッジ」ではない**。
- **`edge_bps` は上限的な指標である**: 十分位の両端だけを取り、h バー保有の重複・
  部分建て・執行スリッページ・実際の turnover を考慮していない。
  これは「情報がコストの桁に届きうるか」を見るための換算であって、
  **戦略の損益ではない**(戦略化の検定は本フェーズの対象外)。
- Y2 / Y3 はサイジング・abstain・ストップ設計への入力であり、bps 換算の対象にしない
  (コスト比較で棄却しない)。これらは「取引するか否か・どれだけ張るか」に効く情報として
  別枠で評価する。
- 本フェーズでは execution optimizer / RL / maker queue simulator を実装しない。

---

## 17. Go / No-Go(凍結)

test 単位で判定する。

**GO(Tier 1 の機序検証へ昇格)** — 以下を全て満たす:

1. §8 の最低サンプル要件を満たす
2. Holm 補正後 `p <= 0.05`
3. §15 の mechanism validation 1–6 を全て満たす
4. confirmation 窓で **`dR2` の符号が dev と一致**し、かつ
   `dR2_conf >= 0.5 * dR2_dev`
5. **T0-A のみ**: 同一行集合・同一 pipeline で `dR2(T0-A) > dR2(S0)`(§12.4 の
   OHLCV-only sham を上回ること)。「新しい変換の形」だけで出た改善を昇格させない。
6. **T0-B1 / T0-B2 のみ**: X 列を**さらに1バー(5分)遅らせても** `dR2 > 0` が保たれること。
   metrics スナップショットの公開遅延は未実測であり、start_of_bar 割当ての5分バッファは
   **仮定であって計測ではない**(data_contract §8)。1バー遅延は「スナップショットが
   1バー遅れて届いていた場合」に相当する最小限の耐性試験である。
   さらに +12バー(1時間)遅延の感度も**報告**する(こちらは条件にしない。
   4時間 horizon に対して1時間の遅延を要求するのは過剰なため)。
7. Y1 の場合は追加で `edge_bps` と break-even を報告(GO の条件にはしない。
   コスト未満でも「情報は存在する」という結論自体は成立するため)

**CONDITIONAL HOLD(監視リスト)**:

- 1–3 は満たすが 4 が不成立
- または Y1 がコスト未満だが Y2/Y3 が GO

→ 実装を進めず記録のみ。次の v2 で扱う。

**NO-GO(v1 棄却)**:

- 2 を満たさない、または placebo 分布と区別できない
- → threshold・horizon・時間帯・片側限定・列の入れ替えで救済しない。
  再挑戦は別 version として定義を凍結し、**その後に開く窓**で行う。

**GO が1つも無い場合**、それは「Tier 0 の粒度・線形・この変換の下では、
**各 cell の MDE(§12)を超える** incremental information を検出できなかった」であり、
**「情報が存在しない」ではない**。null を報告するときは必ず MDE を併記する。
その場合の次の分岐は Tier 1(aggTrades の event-level 集約)であり、
Tier 0 の設定を緩めて掘り直すことではない。

---

## 18. 実行手順と成果物

```sh
# 1. ラベル生成(本文書の凍結後に初めて実行する)
uv run python -m mce.labels_tier0        # 未実装。Y1/Y2/Y3 を data/labels/ へ

# 2. screening 実行
uv run python -m mce.tier0_screening --stage dev
uv run python -m mce.tier0_screening --stage confirmation   # dev の記録保存後に1回だけ

# 3. 成果物
experiments/phase7/tier0_screening_dev_v1.json
experiments/phase7/tier0_screening_confirmation_v1.json
docs/findings/YYYY-MM-DD-phase7-tier0-screening-v1.md
```

report に必ず含めるもの(選別なし):

1. 27 test 全ての結果表(検定不能・未実施も含めて省略しない)
2. placebo 分布の要約と K(第1段階/第2段階)
3. fold 別 `dR2`・年別 `dR2`
4. 除外した暦月と欠測集計
5. 使用データの manifest sha256・source digest・コード commit・lockfile hash・
   `tier0_prereg.py` の sha256・凍結確定時刻
6. 実行時間と乱数種

### 次タスク(screening 実装)の受け入れ条件

1. `mce.tier0_prereg` の定数だけを参照する(閾値・列・窓を実装側に書き直さない)。
2. ラベル生成は独立 module とし、出力は `data/labels/` のみ。features へ `fwd_` 列を
   書かない(既存 loader guard と同じ規約)。
   **ラベル module は読み込み時に次を assert する**: 入力が
   `data/normalized/binance/*.parquet` または `data/features/binance_BTCUSDT_5m.parquet`
   のみであること、`max(ts) < 2026-01-01` であること、入力に `fwd_` 列が無いこと。
   dev 実行 module は `max(ts) < DEV_END` も assert し、confirmation 窓を物理的に読めなくする。
3. dev と confirmation は**別コマンド**で、confirmation は dev の artifact が
   存在しないと実行できない(順序の構造的強制)。
4. 27 test 全ての結果を1つの artifact に機械的に出力する。手で表から行を消せない形にする。
5. placebo・bootstrap の再現手順(K・シフト量・種)を artifact に含める。
6. モデル行列に `isfinite` でない値が入っていないことを毎 fold で assert する
   (0除算・inf が null 判定を素通りするのを止める)。
7. artifact に **`src/mce/tier0_prereg.py` の sha256** と **凍結確定時刻**
   (= 最初のラベル生成コマンドを実行した時刻)を記録する。
   `tests/test_tier0_prereg.py` は artifact が存在する場合、記録された sha256 が
   現在の `tier0_prereg.py` と一致することを検査する
   → **実行後に凍結仕様を書き換えたら CI が落ちる**(改竄の構造的検出)。

### 実行時間の見積り(実測ベース)

Ridge 閉形式の 1 fit(400,000 行 × 25 列)= 実測 **38 ms**(numpy + BLAS)。
支配的なのは Gram 行列 `X'X` の計算で、alpha グリッド 5 点は同じ Gram から
追加コストほぼ0で解ける。placebo ごとに内側 CV(3分割)をやり直すため、
1 replicate あたり **Gram 計算 ≈ 3 回**を見込む。

```text
第1段階: 27 test × 14 fold × 200 replicate × 3 Gram × 38ms ≈ 2.4 時間
第2段階: 昇格候補1件あたり 14 fold × 1,402 × 3 × 38ms ≈ 2.2 時間
         (T0-B2 は 6 fold × 672 なので ≈ 0.5 時間)
```

一晩で終わる規模であり、**特別な計算資源は不要**(numpy の閉形式 ridge のみ、
新規依存を追加しない)。実測値と乖離したら artifact に記録する。

---

## 19. 凍結する項目(実行後に変更しない)

- A の列と Z 変換の定義
- X 4集合の列と変換
- target Y1/Y2/Y3 の定義
- horizon(set 別)
- dev / confirmation の窓と fold 境界・purge/embargo
- estimator とハイパーパラメータグリッド・選択手順
- placebo の設計と K の昇格規則
- family(27)と補正手順
- 最低サンプル要件
- go/no-go 閾値

---

## 20. Null result の解釈(先に書いておく)

GO が 0 件だった場合に **言えること**:

> 2021–2025 の Binance BTCUSDT 5分足において、本文書が定義した線形モデル・変換・
> horizon・target の下では、Tier 0 の集約情報は OHLCV baseline に対する
> incremental information を検出できなかった。

**言えないこと**:

- 「Tier 0 情報に価値が無い」— 非線形・相互作用・状態依存の効果は検定していない
- 「microstructure に価値が無い」— Tier 0 は 5分に集約済みの粗い代理であり、
  event-level(Tier 1/2)の情報とは別物
- 「OHLCV が十分である」— baseline が強いことの証明にはならない
- 「他 venue でも同じ」— 本検定は Binance 単一 venue
- Final OOS について何か — 未開封

逆に GO が出た場合も、それは **「情報が存在する」までしか意味しない**。
戦略化・執行・コスト後の生存は別の検定である。

---

## 21. 人間の判断が必要な決定(実行前に確定させる)

以下は本文書で暫定的に決めたが、実行前に人間が明示的に承認・変更してよい項目である
(承認・変更は**実行前**に限る)。

| # | 論点 | 本文書の暫定 | 代替案 |
|---|---|---|---|
| 1 | dev の開始年 | 2021-01-01 | 2020-09(OI 開始)まで広げる / 2022 以降に絞る |
| 2 | T0-B2 の窓 | 2023-01-01 開始(2022 の欠測ブロック回避) | 欠測を許容して 2021 開始・被覆を報告 |
| 3 | 主判定 | Holm(FWER 0.05) | BH(FDR 0.10)を主にする |
| 4 | モデル | Ridge 線形のみ | 勾配ブースティングを capacity 対照つきで追加 |
| 5 | confirmation 窓 | 2025 通年 | OKX validation と同じ 2025-07..12 に合わせる |
| 6 | Y3 の向き | long 目線 MAE のみ | short 目線 MFE も追加(family が 36 になる) |
| 7 | cost 直結 target `Y5 = 1[abs(Y1) > 10bps]` | 記述統計のみ(仮説検定しない) | family へ入れる。ただし **T0-B2 を family から外す**か窓を伸ばさない限り、Holm 最小閾値へ placebo 解像度が届かない(§5) |
| 8 | 交互作用の本数 | 2本(T0-A / T0-B1) | 各集合に1本ずつ入れる(列数が増え、集合間の自由度差が広がる) |

---

## 22. 既知の弱点(先に記録する)

1. **線形限定**。相互作用・閾値効果・regime 依存を検出できない。
2. **単一 venue・単一銘柄**。cross-venue の一般化は主張できない。
3. **Tier 0 は集約済み代理**。aggressor side は 5分合計であり、
   個々の約定の系列(burst・大口)は見えない。
4. **T0-B2 の窓が他集合より短い**(2年)。集合間で検出力が揃っていない。
5. **placebo は日単位巡回シフト**。X が週次・月次の強い周期を持つ場合、
   シフトが完全な帰無を作らない可能性がある(週単位周期は保存されるため保守側)。
6. **metrics のスナップショット意味論**は公式仕様で完全には確認できていない
   (5分ごとの状態量であることは形式から明らかだが、集計区間の定義は要確認)。
   start_of_bar 割当ては5分バッファを持つ保守側の選択である。
7. **Y3(MAE)は low を使う**ため、bar 内の経路順序は分からない(first-touch 問題)。
   v1 では経路の順序を問わない下限値として扱う。
8. **confirmation 窓(2025年)はプロジェクト全体としては完全な処女期間ではない**。
   後半 `2025-07-01 .. 2026-01-01` は OKX の validation split と暦が一致し、
   Phase 3 の search loop で既に一度使われている(ただし対象は OKX の OHLCV DSL 戦略であり、
   Binance の Tier 0 情報については未使用)。この重なりは記録として明示する。
9. **dev 窓が上げ相場に偏る**。2021-2024 は 2022 の下落を含むが、T0-B2 の窓(2023-2024)は
   ほぼ上昇局面である。regime 依存の検出力は集合間で揃っていない(§15-4 で部分的に対処)。
10. **交互作用は2本だけ**事前登録した。3項以上の相互作用・閾値効果・状態遷移は検定していない。
11. **metrics の公開遅延は未実測**。+1バー遅延の耐性を T0-B1/B2 の昇格条件にしたが、
    これは「1バー遅れていた場合」の最小限の試験であり、遅延の**計測ではない**。
12. **A を 27 列まで厚くした副作用**として、真の増分が小さい場合の検出力は下がる。
    これは「偽陽性を出さないこと」を優先した意図的な選択である。
13. **T0-B2 の placebo 解像度は余裕が小さい**(全数 672 シフトで最小 p 1.486e-3 に対し
    Holm 閾値 1.852e-3)。窓を伸ばせない限りこれが上限である。

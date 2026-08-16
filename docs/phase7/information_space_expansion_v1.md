# Phase 7 — Information-Space Expansion Protocol v1

- 作成日: 2026-08-16(Phase 3 closeout と同時)
- 状態: **設計凍結前のプロトコル草案 v1**。個別 screening を実行する前に、
  対象情報集合ごとの節を「実行前追記凍結」する(bakeoff_protocol と同じ運用)。
- 前提: [Phase 3 bakeoff summary](../findings/2026-08-16-phase3-bakeoff-summary-v1.md)、
  [Judge freeze v1](../phase1/freeze_v1.md)、[data contract](../data_contract.md)
- 既存の [Microstructure v1(事前凍結・収集中)](../findings/2026-08-16-microstructure-v1-protocol.md)
  は**本ロードマップの部分集合であり、本文書は既存 v1 の定義を一切変更しない**
  (§9 と [microstructure v1 review](microstructure_v1_review.md) を参照)。

## 0. Research Question

> **5分足 OHLCV を baseline information set としたとき、どの追加情報群が、
> OHLCV だけでは説明できず、かつ取引コスト上意味のある incremental predictive
> information を持つか。**

各情報集合 X について独立に:

```text
H0: X は OHLCV baseline を超える有用な incremental information を含まない
H1: X は OHLCV baseline を超える再現可能な incremental information を含む
```

**目的は「別データを入れれば儲かる」の確認でも、「OHLCV の限界の証明」でもない。**
情報の存在を測り、次に何へ投資するかを決めるための screening である。

検証パイプライン:

```text
OHLCV baseline
      ↓
OHLCV + information set X
      ↓
incremental information test   (存在するか)
      ↓
mechanism validation           (主張した機序と符号・形が一致するか)
      ↓
cost relevance                 (執行コスト水準で意味があるか)
      ↓
out-of-sample validation       (別窓で再現するか)
      ↓
retain / reject
```

## 1. Baseline information set(凍結)

baseline は Phase 0–3 で使ったものと同一の observable のみ:

```text
5分足 OHLCV から導出される observable(open/high/low/close/volume/volume_quote,
return_*, volatility, range, volume_z, ma_slope, clock 位相)
```

- baseline 側に derivatives / microstructure を混ぜない。
- 追加情報集合 X は**必ず baseline を含んだ入れ子(nested)構造**で比較する
  (A = baseline、B = baseline + X)。X 単独モデルは補助情報にとどめる。

## 2. 情報集合の評価マトリクス(記入必須項目)

各 information set について、screening 実行**前**に次を記入する。

```text
name / source / venue
historical availability      (遡及でどこまで取れるか。実測日を書く)
prospective availability     (今から貯めれば取れるか)
timestamp semantics          (exchange ts / 受信 ts / 公開遅延 / 集計区間の閉じ方)
update frequency
raw data size                (実測 bytes/day)
existing local history       (mce.data_inventory の出力)
missing-data behavior
synchronization difficulty
leakage risk
reconstruction requirement
transaction-cost relevance
expected mechanism
candidate targets
minimum sample requirement
OHLCV baseline comparison    (どの baseline と入れ子にするか)
validation design
go / no-go criterion
implementation cost
research priority
```

優先順位は「理論上強そう」ではなく

```text
expected research value / (data cost + implementation cost)
```

で決める。

## 3. データ可用性の実測(2026-08-16、本実行環境から)

Phase 3 の想定(「derivatives は L2 より単純で履歴検証しやすい」)は、
**この repository の実測とは一致しない**。OKX の遡及は浅い。

### 3.1 OKX(執行モデルを凍結してある venue)

| 情報集合 | 遡及 | 現在のローカル在庫 | 出典 |
|---|---|---|---|
| 5m OHLCV | 上場以来 | 288,124本 / 2023-11-19〜2026-08-16 / 欠損0 | `data/manifests/ohlcv_*.json` |
| funding rate | **約3ヶ月** | 280本 / 2026-05-14〜2026-08-15 | [data_sources](../data_sources.md) |
| open interest(5m) | **約5日** | 1,441本 / 2026-08-10〜2026-08-15 | 同上。公開遅延未実測のため observable 未昇格([data_contract §8](../data_contract.md)) |
| trades / BBO / 400段 book | **遡及不可**(historical API 無し) | prospective 収集のみ([microstructure v1](../findings/2026-08-16-microstructure-v1-protocol.md)) | [microstructure_collection](../microstructure_collection.md) |
| liquidation | 未実装・未収集 | なし | — |

→ **OKX 単独では「今日から貯める」以外に microstructure / derivatives の履歴を作れない。**

### 3.2 Binance Vision(一括ダンプ。2026-08-16 に本環境から HTTP 実測)

`https://data.binance.vision/data/futures/um/...`(BTCUSDT perp)。
数値は当日の HEAD/GET 実測(zip サイズ)。

| データ | パス | 実測 | 内容 |
|---|---|---|---|
| klines 5m | `daily/klines/BTCUSDT/5m/` | 200 / **12.9 KB/日** | ヘッダ実測: `open_time,open,high,low,close,volume,close_time,quote_volume,count,taker_buy_volume,taker_buy_quote_volume,ignore` — **taker buy 数量と約定件数を含む** |
| metrics 5m | `daily/metrics/BTCUSDT/` | 200 / **11.0 KB/日**(2021-01-01 は有、2020-01-01 は 404) | `sum_open_interest, sum_open_interest_value, count_toptrader_long_short_ratio, sum_toptrader_long_short_ratio, count_long_short_ratio, sum_taker_long_short_vol_ratio` |
| premiumIndexKlines 5m | `daily/premiumIndexKlines/BTCUSDT/5m/` | 200 / **6.0 KB/日** | perp/index premium(basis)。列定義は公式 docs で要確認 |
| fundingRate | `monthly/fundingRate/BTCUSDT/` | 200 / 0.9 KB/月 | funding 履歴 |
| aggTrades | `daily/aggTrades/BTCUSDT/` | 200 / **5.0 MB/日**(2026-08-01)、8.0 MB/日(2023-11-19)、1.0 MB/日(2020-01-01) | 集約約定(aggressor side 含む) |
| trades | `daily/trades/BTCUSDT/` | 200 / 8.1 MB/日 | 個別約定 |
| bookDepth | `daily/bookDepth/BTCUSDT/` | 200 / **0.55 MB/日** | 板 depth の定期スナップショット(要仕様確認) |
| bookTicker | `daily/bookTicker/BTCUSDT/` | 200 / **199 MB/日** | L1 tick(2024-01-02 実測)。33ヶ月で ~200 GB |
| liquidationSnapshot | `daily/liquidationSnapshot/BTCUSDT/` | **404**(2023-01-02 / 2026-08-01) | 現状取得不可。要再確認 |

注意(そのまま制約として扱う):

- これは**別 venue(Binance USDT-M perp)**であり、Judge が凍結してある OKX ではない。
- 本環境(米国リージョンとみられる)からの実測。**日本 IP・ToS 上の可否は別途要確認**
  ([data_sources](../data_sources.md) の注意をそのまま適用)。
- 再配布はしない。ローカル個人研究の範囲に留める。

### 3.3 実測から導かれる帰結

**Priority A の観測量の一部(aggressive flow の粗い代理・OI・basis)は、
OHLCV と同じ 5分粒度・同じ桁のデータ量で、深い履歴として取得可能である。**
一方 L1 tick(bookTicker)は 200 GB 規模、L2 full book は遡及不可。
したがって「trades → L1 → L2」という素朴な順序ではなく、
**粒度は粗いが履歴が深く安価な情報から先に H0 を検定する**方が期待情報価値/コストが高い。

## 4. 情報集合の優先順位(実測を反映)

### Tier 0 — bar 集約済み・深い履歴・OHLCV と同コスト(最優先)

> **状態(2026-08-16 追記)**: Tier 0 の3系列は**取り込み・正規化・observable 化・
> 品質確認まで完了**した(ラベル未閲覧・仮説未登録)。契約と実測値は
> [tier0_ingest_v1](tier0_ingest_v1.md)、機械可読レポートは
> `experiments/phase7/tier0_quality_v1.json`。次は §6 に従った**事前登録**。

| set | observable | 想定 mechanism | 状態 |
|---|---|---|---|
| **T0-A 集約 aggressive flow** | taker_buy_volume / volume(= aggressor buy share)、taker_buy_quote、`count`(約定件数)、平均約定サイズ | aggressive-flow continuation、absorption(価格が動かない大量 taker 買い)、参加者数と方向の乖離 | 未実装・即取得可 |
| **T0-B derivatives state** | sum_open_interest、ΔOI、OI z-score、long/short ratio(top trader / all)、taker long-short vol ratio | price × OI state、建玉積み上がり、crowded position unwind、funding-conditioned continuation | 未実装・即取得可(2021〜) |
| **T0-C basis / funding** | perp−index premium、funding rate、funding 予測値との乖離 | carry、レバレッジ需要、squeeze 前兆 | 未実装・即取得可 |

Tier 0 は **reconstruction 不要・timestamp が bar と同一境界・合計 ~30 KB/日**。
「情報が存在するか」の H0 検定をこの上で先に済ませる。

### Tier 1 — event-level・履歴あり・集約が必要

| set | observable | mechanism | コスト |
|---|---|---|---|
| **T1-A aggTrades** | signed volume、notional imbalance、trade-count imbalance、large-trade imbalance、flow persistence、burst intensity、単位 aggressive flow あたりの価格応答、trade clustering | absorption / exhaustion、大口 impact、flow persistence | ~5–8 MB/日(1000日で ~6 GB zip)。ストリーム集約実装が必要 |
| **T1-B bookDepth スナップショット** | mid からの距離別 depth、depth 非対称性 | liquidity state、impact の条件付け | ~0.55 MB/日。仕様確認が必要 |

### Tier 2 — prospective 資産(既に収集機構がある)

| set | 状態 |
|---|---|
| **T2-A OKX trades / BBO(L1)/ 400段 L2** | collector・normalizer・quality gate 実装済み。**遡及不可・prospective のみ**。 既存 [Microstructure v1](../findings/2026-08-16-microstructure-v1-protocol.md) が M1(L1 OFI)・M2(10bps 板枯れ)・M3(吸収)を事前凍結済み。**結果を見る前に定義を変更しない** |

### Tier 3 — 後回し

- 遡及 L1 tick(bookTicker 200 GB)— Tier 0/1 で機序が見えてから検討する。
- cross-venue(OKX × Binance × Bybit の lead-lag、mid 乖離、flow divergence)—
  単一 venue の microstructure を理解する前に複雑化しない。
- liquidation 履歴 — Binance Vision の該当パスが 404、OKX は未実装。取得手段の再調査が先。
- options IV / skew / term structure、on-chain、macro、news、social —
  5分以下の研究では timestamp 精度・latency・licensing・情報の利用可能時刻の問題が大きい。

## 5. Targets(方向符号に限定しない)

各情報集合について、**複数 target を事前登録**し、multiple testing として扱う。

| target | 定義例(horizon h は事前登録) | 執行への接続 |
|---|---|---|
| return sign | `sign(fwd_open_return_h)` | trade / abstain、方向 |
| return magnitude | `|fwd_return_h|` | サイズ、期待コスト回収可否 |
| realized volatility | 次 h の実現ボラ | サイズ、ストップ幅 |
| tail move | `P(|r| > q95)` | リスク、abstain |
| range expansion | 次 h の高安レンジ / 直近中央値 | barrier 設計 |
| adverse move after entry | entry 後の MAE | 執行タイミング |
| (microstructure 到達後)spread / depth / impact | 執行コスト自体 | maker/taker 選択 |

- 既に **H1–H4(ボラ・流動性クラスタリング)が33ヶ月生存**している
  ([5m tendencies](../findings/2026-08-16-5m-tendencies-33mo-retest.md))。
  したがって volatility / magnitude 系 target は「OHLCV baseline が既に強い」領域であり、
  incremental を主張するには baseline を丁寧に組む必要がある(有利な baseline を選ばない)。
- 本フェーズでは **execution optimizer / RL / maker queue simulator を新規実装しない**。

## 6. Incremental information test(方法論)

成功条件を「profitable strategy が見つかった」に**しない**。

### 6.1 入れ子比較

```text
Model A: baseline features            → target
Model B: baseline features + X        → target
```

- **同一 split・同一 target・同一 estimator・同一前処理**で比較する。
- estimator は**単純なものを既定とする**(標準化 + ridge / logistic)。
  capacity 差を information value と誤認しないため、A と B は
  同一 family・同一正則化探索範囲・同一 CV 手順とする。
- 主要効果量: out-of-sample の ΔR²(連続 target)/ Δlog-loss・ΔAUC(離散 target)、
  および情報係数(IC)の差。**p 値だけでなく効果量を必ず報告する。**
- **capacity 対照(必須)**: X をブロック単位でシャッフルした placebo X̃ を同じ手順に通し、
  `B(X)` と `B(X̃)` を比較する。これで「列が増えたこと自体」の効果を引く。

### 6.2 時点整合(leakage 防止)

- data contract の `feature_time <= signal_time < execution_time` を継承する。
- bar 集約情報(Tier 0)は **bar t の区間 [ts, ts+5m) を閉じた時点で確定**とみなし、
  bar t の signal は close 後、fill は open[t+1](既存規則と同一)。
- event-level 情報(Tier 1/2)は **received/exchange timestamp を明示**し、
  集約窓は左閉右開 `[q−w, q)`。境界と同時刻のイベントは次の decision へ回す。
- 公開遅延が未実測の系列(OKX OI 等)は observable へ昇格させない。
  昇格には**遅延の実測**が前提(data contract §3 の `available_time` 導入)。
- **contemporaneous な機械的関係を予測力と取り違えない。**
  例: 同一 bar の taker buy share と同一 bar の return は定義上強く相関する。
  検定対象は必ず**将来 horizon**の target とし、同時刻関係は別途「記述統計」として報告する。

### 6.3 Final OOS firewall(venue をまたいでも維持する)

- 凍結 split(OKX): research `2023-11-19〜2025-07-01` / validation `〜2026-01-01` /
  final_oos `2026-01-01〜`(封印)。
- **information-space screening は `ts < 2026-01-01` のデータのみを使う。**
  別 venue(Binance)のデータであっても、封印期間と同じ暦期間を screening で見れば
  実質的に firewall を破ることになるため、**期間で封印を継承する**。
- screening 結果を Final OOS の解釈へ持ち込まない。

### 6.4 Multiple testing

- 事前登録した (information set × target × horizon) の全組み合わせを family として数える。
- family 内で Holm 補正(family-wise alpha = 0.05)。**未検定・N不足の組も p=1 として
  family size を維持する**(Microstructure v1 §7 と同じ規律)。
- 重複リターンの実効N補正(恒久ルール4)と、日次 cluster block bootstrap を用いる。
- 探索的に horizon / threshold / 時間帯を後から足さない。足す場合は v2 として再凍結。

### 6.5 最低サンプル要件

- bar 集約 target: 有効サンプル `n_eff = n_bars / h` が **1,000 以上**を必須とする。
- event 系: 既存 Microstructure v1 §8 の要件(n≥1,000、long/short 各≥300、
  event 日≥50、weekend≥12)を準用する。
- 満たさない場合は「edge なし」ではなく **検定不能**として記録する。

## 7. Mechanism validation

incremental information が検出された場合のみ次へ進む。

1. **符号と形が主張した機序と一致するか**(例: aggressive buy imbalance →
   continuation なら正、absorption なら条件付きで負)。
2. **条件付け変数で分解**(ボラ regime、流動性、時間帯、weekday/weekend)。
   単一 regime・単一時間帯だけに依存する結果は昇格させない。
3. **単調性**: 情報量の強さ(分位)と効果の単調関係があるか。
4. **安定性**: 非重複ブロック(固定10日 block 等)で符号が保たれるか。
5. 機序が説明できない統計的関係は「監視リスト」に留める(恒久ルール1)。

## 8. Cost relevance

- 効果量を **bps/取引** に変換し、OKX taker 往復 10bps(および stress 15bps)と比較する。
- break-even cost を必ず報告する。**コスト未満の効果は「統計的事実」であって「エッジ」ではない**
  (恒久ルール5)。
- コスト後に消える場合でも、target が volatility / liquidity / impact 系であれば
  「執行・サイズ設計への入力」としての価値を別枠で記録する(直ちに棄却しない)。

## 9. 既存 Microstructure v1 との関係(変更しない)

- M1(L1 OFI)/ M2(10bps 板枯れ)/ M3(aggressive-flow 吸収)は **事前凍結済みの
  prospective 検定**であり、本文書はその定義・閾値・split・pass 条件を一切変更しない。
- 本 Phase 7 は上位ロードマップであり、M1–M3 は Tier 2(OKX prospective)の
  最初の3仮説として**そのまま包含**される。
- Tier 0/1 の screening 結果を根拠に M1–M3 を書き換えない(結果閲覧前の変更禁止)。
- 詳細な対応表は [microstructure v1 review](microstructure_v1_review.md)。

## 10. Go / No-Go(情報集合ごと)

1つの information set X について、次を**事前に**書き下してから実行する。

**Go(次段階へ)**: 以下を全て満たす

1. データ品質ゲート通過(欠損・timestamp・単位・重複)
2. 最低サンプル要件(§6.5)充足
3. Holm 補正後 p ≤ 0.05 の (target, horizon) が1つ以上
4. placebo(shuffle-X)対照に対して効果量が明確に上回る
5. 効果の符号・形が事前登録した機序と整合(§7)
6. 非重複ブロックの多数(例 5/6)で符号一致

**Conditional hold(監視リスト)**: 3–6 の一部のみ満たす、または効果がコスト未満だが
volatility / liquidity target で安定 → 実装は進めず記録のみ。

**No-Go(棄却)**: 3 を満たさない、または placebo と区別できない
→ その情報集合は v1 では棄却。threshold・horizon・時間帯を緩めて救済しない。
再挑戦は別 version として定義を凍結し、**その後に始まる**窓で行う。

## 11. 成果物(screening ごと)

1. 事前登録文書(情報集合・observable 契約・target・horizon・go/no-go)
2. データ manifest(sha256・行数・期間・欠損)と取得元 URL・取得日
3. 実行コードと lockfile の commit hash・固定 seed
4. 全 (target × horizon) の結果表(未検定・N不足も含め省略しない)
5. placebo 対照の結果
6. findings 台帳エントリ(result / interpretation / limitations を分離)

## 12. 本フェーズで実装しないもの

- Market Microstructure DSL v2 本体(先に observable 契約と機序を確定させる)
- 新しい alpha strategy、trading 用 ML predictor、RL execution、maker queue simulator
- MCTS / memory agent / 改良 GA(→ [research backlog](../research_backlog.md))
- Final OOS の開封、Phase 3 の再実行

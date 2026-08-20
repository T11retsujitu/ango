# Data Contract

本プロジェクトの全データ資産が従う契約。バックテスト・実験はこの契約を前提とする。
変更する場合は本文書と対応テストを同一コミットで更新すること(Phase 1 終了後に freeze)。

## 1. レイヤー構成

```text
data/raw/         API レスポンス原形 (JSONL gzip)。追記のみ・不変。重複許容
data/normalized/  共通スキーマ Parquet。(source, symbol, ts) で重複排除。補間しない
data/features/    observable features。バー t の close 時点までに観測可能な値のみ
data/labels/      評価専用ラベル (fwd_*)。strategy feature としての利用は禁止
data/manifests/   データ資産の指紋 (sha256・行数・期間・欠損数)。git 管理
```

## 2. 時刻規約(最重要)

- 内部時刻はすべて **UTC**(ms 精度・tz 付き)。UNIX time は API 境界でのみ扱う。
- **バーの `ts` はバー開始時刻**(OKX candle の仕様)。行 t は区間 `[ts, ts+5m)` を表す。
- したがって行 t の close / high / low / volume は **`ts + 5m`(close 時刻)まで観測不能**。
- 約定規則:

```text
features = data available through close[t]      (= 時刻 ts_t + 5m までの情報)
signal   = generated after close[t]
fill     = open[t+1]                            (= 次に存在するバーの始値)

feature_time <= signal_time < execution_time
```

- 禁止: `signal uses close[t]` かつ `fill = close[t]`(同一バー close 執行)。

## 3. availability 宣言

observable feature の各列は `mce.features.AVAILABILITY` に availability 種別を宣言する。
未宣言の列は backtest loader が拒否する。

| 種別 | 意味 |
|---|---|
| `start_of_bar` | 行 t の値は時刻 `ts`(バー開始)時点で確定している(例: open, clock 系, as-of funding) |
| `close_of_bar` | 行 t の値は時刻 `ts + 5m`(バー close)で確定する(例: close, return_5m) |

いずれの種別も「signal at close[t] → fill at open[t+1]」の約定規則下では利用可能。
行単位の `available_time` 物理列は、公開遅延が行ごとに異なるデータ(aggTrades、
実測遅延つき OI)を observable に昇格させる時点で導入する。

## 4. observable / label の分離

- **label(未来参照列)は `fwd_` 接頭辞を必須とし、`data/labels/` にのみ存在する。**
- `data/features/` に `fwd_` 列が存在してはならない(テストで強制)。
- backtest loader (`mce.backtest.data`) は features のみを読み、`fwd_` 列を検出したら
  例外を送出する。
- 条件検索・評価でラベルが必要な場合は features ⋈ labels を **明示的に join** する。

## 5. rolling / 派生 feature の規約

- rolling 窓は `closed="left"`(現在バーを含まない)か、現在バーの close 確定値のみを使う。
- 部分窓は容認しない: 窓の完全性条件(本数・カバレッジ)を満たさない行は null。
- リターン系は行シフトではなく **ts 完全一致 join** で計算する
  (欠損バーを跨いだ誤った期間のリターンを作らない)。
- as-of join は `strategy="backward"` + tolerance 必須(未来値の参照禁止)。
- 新しい observable 列を追加したら `AVAILABILITY` へ宣言し、Future Mutation Test の
  対象に自動的に入る(features 全列一括検査)。

## 6. 欠損・重複

- 欠損バーは補間しない。検出は report / manifest が行う。
- normalized へのマージは **`(source, symbol, market_type, ts)`** で重複排除(冪等)。
- 未確定足(OKX `confirm != "1"`)は normalized に入れない。

**`market_type` をキーに含める理由(Y23)**: Binance の spot と USD-M perp は
**どちらも `source="binance"` / `symbol="BTCUSDT"`** である。`(source, symbol, ts)`
だけでは同一時刻の spot 行と perp 行が同じキーになり、**一方が他方を上書きしうる**。
実装 (`mce.normalize_binance.KEY_COLS`) は本節と一致していなければならない。

**イベント系列**(下記 §8 の funding 決済など)は 5分グリッド上に無い。
グリッドを前提とする「欠測バー」の概念を当てはめない。manifest も期待間隔を
持たせない(持たせると存在しない欠測を数えてしまう)。

## 7. clock 系列の定義

バー開始時刻 `ts` の壁時計(UTC)から決定的に導出する:

| 列 | 定義 |
|---|---|
| `minute_mod_15` | `minute(ts) % 15`(5分足では 0/5/10) |
| `minute_mod_60` | `minute(ts)` |
| `hour_utc` | `hour(ts)`(0–23) |
| `weekday_utc` | 0=月曜 … 6=日曜(ISO weekday − 1) |

## 8. funding / OI

- `funding_rate` は as-of join(backward, tolerance 9h)。`ts`(バー開始)以前に
  **決済確定**した直近値のみ(OKX `fundingTime` = 決済時刻)。
- Binance 代理 funding(source="binance")はキャリー統計専用。**OKX 執行 PnL には使わない。**
  この禁止は維持する。**別 venue の執行 PnL に他 venue の funding を持ち込まない。**

### 8.1 Binance USD-M funding 決済イベントの availability(Y27)

Phase 8 は Binance の funding を **Binance 自身の two-leg carry PnL** の中で使う。
上の禁止(別 venue へ持ち込まない)と矛盾しない: 同一 venue 内での使用である。

| 項目 | 定義 |
|---|---|
| 系列 | `data/normalized/binance/funding_rate_<symbol>.parquet`(**イベント系列**) |
| `ts` | **決済時刻**(Vision `calc_time` = 公式 REST `fundingTime`)。バー開始時刻ではない |
| availability | **`ts`(決済確定時刻)以後**。決済が確定した時点で初めて観測可能になる |
| 決定時点での参照 | **`funding_key = ts + DELTA_PUB` に対する as-of backward** で引く。tolerance を広げることは未来参照の防止にならない(公開遅延はキーのシフトで表す) |
| `funding_interval_hours` | **直前の決済との時刻差から導出**する。**8 をハードコードしない**(cap/floor 到達時に恒久的に1時間へ切り替わる規則がある) |
| 補間 | **しない。** 最初のイベントの間隔は null(直前の決済が観測範囲に無い) |
| `mark_price` | **この dump には無い**(実測)。決済時点の mark が要るときは別ソースから供給する。**null 列で代用しない** |

- observable feature へ昇格させる列は、従来どおり `mce.features.AVAILABILITY` へ
  宣言してから使う(§3)。**本節はまだ feature を定義していない**(入力層の契約のみ)。
- OI は公開遅延未実測・遡及約5日のため、**observable feature に昇格させない**
  (normalized 保持のみ。Phase 7 で再検討)。

## 9. split 規約

境界の単一定義は `mce.backtest.splits`(日付は Phase 1 終了後に freeze):

```text
research   : 2023-11-19T00:00Z <= ts < 2025-07-01T00:00Z
validation : 2025-07-01T00:00Z <= ts < 2026-01-01T00:00Z
final_oos  : 2026-01-01T00:00Z <= ts   (上限なし。将来蓄積分も自動的に封印域)
```

- research 開始より古いバー(将来バックフィルした場合)はどの split にも属さず、
  loader は返さない。
- `final_oos` は通常 loader API では読めない(Phase 6 の sealed evaluator のみ)。
  結果を research loop へ戻すことも禁止(ROADMAP §4.2)。

## 10. manifest

`data/manifests/*.json` に各 Parquet の sha256・行数・列名・ts 範囲・欠損バー数を記録する。
実験 artifact は使用データの manifest hash を必ず参照する。manifest は git 管理する。

# Phase 8.1 — Binance Vision `fundingRate` dump の実測 schema (v1)

- 日付: 2026-08-20 (UTC)
- 対象凍結版: **v1.8.5**(現行)。**本作業は凍結記録を1バイトも変更していない。**
- 作業区分: **入力データ配管のみ**(F4)。取引系列へ join せず、rho / シグナル /
  清算発生 / return / PnL を一切計算していない。Layer 1/2/3 を走らせていない。
  Final OOS を開いていない。封印済み prior register を読んでいない。
- **schema は推測していない。** 実装前に **2020-01 と 2025-12 の2ファイルだけ**を
  取得して実測し、その結果に合わせて normalizer を書いた。

---

## 1. 取得経路

| 項目 | 値 |
|---|---|
| dataset 名 | `funding_rate` |
| path template | `data/futures/um/monthly/fundingRate/{sym}/{sym}-fundingRate-{period}.zip` |
| cadence | monthly |
| market_type | `perp_linear`(USD-M perp) |
| raw ディレクトリ | `data/raw/binance/vision/funding_rate/BTCUSDT/`(**dataset 専用**) |
| ledger | 同ディレクトリの `download_ledger.jsonl`(**dataset 専用**) |

**path は klines と2点違う**(実測で確認した。klines から類推すると外れる):

- `5m/` に相当する **interval ディレクトリが無い**
- ファイル名が `{sym}-5m-{period}.zip` ではなく **`{sym}-fundingRate-{period}.zip`**

---

## 2. CSV schema(実測)

両月とも **header 行あり**、**3列**:

```text
calc_time,funding_interval_hours,last_funding_rate
1577836800000,8,-0.00012359
1577865600000,8,-0.00012383
```

| 列 | 型 | 意味(実測で確認した範囲) |
|---|---|---|
| `calc_time` | epoch **ms** | **funding 決済時刻**。protocol X4 のとおり公式 REST `fundingTime` と同じ量。**バー開始時刻ではない** |
| `funding_interval_hours` | 整数 | **dump 自身が宣言する**決済間隔。実測は全行 `8` |
| `last_funding_rate` | 小数 | **その決済で確定した**レート |

### 2.1 markPrice 列は**存在しない**

**dump にある列は上の3つだけである。** したがって protocol §8.1 の
`cash_flow(s) = q · MarkPrice(s) · f(s)` に要る **決済時点の mark price は
この経路からは供給できない**。

**null 列を作って埋めることはしない**(存在しないものを列にすると、
下流で「あるが欠測している」と誤読される)。mark が要るときは別ソース
(公式 REST の `markPrice`、または `mark_price_5m` の `ts = s − 5m` のバー。Y14)
から供給する。**本コミットではその経路を実装していない。**

### 2.2 `calc_time` の粒度(実測)

- 全行が **正時ちょうど + サブ秒**に載っている(`ts % 3_600_000 == ts % 1000`)。
  probe 2か月では 0〜16ms、**全 6,576 決済では 0〜47ms**(標本を広げて訂正した)。
- 決済時刻の UTC 時は **{0, 8, 16}** のみ。
- ms のジッタは実在する(例: `1764576000004` = 2025-12-01T08:00:00.004Z)。
  **丸めない。** ジッタを丸めると「決済時刻」を書き換えることになる。

---

## 3. 正規化後の列

`data/normalized/binance/funding_rate_BTCUSDT.parquet`
(Phase 7 由来の `data/normalized/funding_rate/binance_BTCUSDT.parquet` とは
**別ファイル**であり、そちらを上書きしない)

| 列 | 由来 | 意味 |
|---|---|---|
| `ts` | `calc_time` | **決済時刻**(UTC, tz-aware, ms 精度) |
| `funding_rate` | `last_funding_rate` | その決済で確定したレート |
| `funding_interval_hours` | **導出** | **直前の決済との時刻差**。最初のイベントは null |
| `funding_interval_ms` | **導出** | 同じ差の整数 ms(丸めのない原値) |
| `funding_interval_hours_declared` | `funding_interval_hours` | **dump の宣言値**。導出値と**別列**で持つ |
| `symbol` / `source` / `market_type` | 出所 | `BTCUSDT` / `binance` / `perp_linear` |

### 3.1 間隔を**導出**する理由と規則

protocol X5 は「`funding_interval_hours` を**行ごとに読む**。8 をハードコードしない」
と定める(cap/floor 到達時に恒久的に1時間へ切り替わる規則があるため)。
本実装は宣言値をそのまま信じるのではなく、**実際に経過した時間**を導出する:

```text
funding_interval_ms[t]    = ts[t] - ts[t-1]        (t = 0 は null)
funding_interval_hours[t] = funding_interval_ms[t] / 3_600_000
```

- **未来行から逆算しない。** 行 t の間隔は直前の行だけで決まる
  (末尾に決済を足しても既存行の値は動かない。テストで固定)。
- **最初のイベントは null。** 直前の決済が観測範囲の外にあるので不明である。
  **0 や 8 で埋めない。**
- **8時間に固定しない。** 1h / 4h / 8h いずれが現れてもそのまま保持する。
- 導出は **系列全体(全 zip を結合し重複排除して ts 昇順にしたもの)に1回だけ**
  適用する。月ファイル単位で適用すると各月の先頭が null になってしまう。

### 3.2 浮動小数の注意(実装上の落とし穴)

polars は `col / 定数` を **逆数の乗算**へ最適化するため、素直に書くと
ちょうど 8h(28,800,000ms)が **`7.999999999999999`** になる。整数部と剰余に
分けて計算すると Python の除算と 1bit まで一致し、`8.0` / `4.0` / `1.0` が
そのまま出る。**丸めているのではなく、丸めなくても厳密になる書き方を選んでいる。**

### 3.3 品質異常として数えるもの(落とさない・埋めない)

| 計数 | 意味 |
|---|---|
| `funding_interval_non_positive_rows` | 間隔が 0 以下(重複・逆順) |
| `funding_interval_disagrees_with_declared_rows` | 導出値と宣言値のずれが **1分**を超える |
| `funding_interval_max_deviation_ms` | そのずれの最大値(**許容幅で隠れた分も必ず出す**) |
| `funding_interval_first_event_null` | 間隔が不明な先頭イベントの数 |

1分の許容幅は §2.2 のサブ秒ジッタを吸収するためのものである。
1h / 4h / 8h の切り替え(時間オーダ)は必ずこの幅を超えるので検出される。

---

## 4. 封印(per-target cutoff)

```text
SEAL_CUTOFFS[dataset] → 正規化時に ts >= cutoff の行を物理的に落とす
```

- **Phase 7 の3系列の既定は `FINAL_OOS_START`(2026-01-01)のまま**で、
  1文字も変えていない(テストで固定)。
- **H5 が承認されるまで Phase 8 の系列も同じ値を使う**ので、現時点で全 dataset の
  値は一致している。「値が一致していること」と「1個のグローバルを共有していること」は
  違う。将来 layer 3 を有効化するときは**この表の Phase 8 側だけ**を動かす。

---

## 5. 実測結果(2020-01 〜 2025-12)

| 項目 | 値 |
|---|---|
| 取得 | **72 / 72 か月**、absent 0、**公開 CHECKSUM 検証 72 / 72** |
| dataset 固有 digest | `d1ddb1207742acd139921fa0f04e10771b81834b8ca4a0335bfb6bf36c677c34` |
| 決済イベント数 | **6,576** |
| 期間 | 2020-01-01T00:00:00Z 〜 2025-12-31T16:00:00.001Z |
| 封印落ち | **0**(dump が 2025-12 までなので該当行が無い) |
| 重複 | 完全重複 0 / 値の食い違い 0 / 未解決 0 |
| 導出間隔 | 6,575 本(先頭 1 本は null) |
| 間隔の分布 | **8h が 6,575 本**(1h / 4h は**この期間には現れなかった**) |
| 間隔の実測幅 | 7.9999875 h 〜 8.000013055556 h(サブ秒ジッタ) |
| 宣言値との食い違い | **0 件**。最大ずれ **47 ms** |
| 非正な間隔 | 0 件 |

**この期間の BTCUSDT では 1時間間隔への切り替えは起きていない。**
ただし**それは実装が 8h を仮定してよい理由にはならない**(X5 の規則は残っており、
別 symbol・別期間では起きうる)。実装は宣言値も導出値も行ごとに持つ。

---

## 6. 本作業で**していないこと**

- funding を取引系列・5分バーへ join していない
- `features_carry` / rho / シグナル / return / PnL / runner を実装していない
- IDX(`indexPriceKlines`)を追加していない
- 決済時点の mark price を供給していない(§2.1 のとおり dump に無い)
- Layer 1/2/3 を走らせていない。Final OOS を開いていない
- **凍結記録・凍結済みファイルを1バイトも変更していない**

# Phase 8.1 — Binance Vision `indexPriceKlines` dump の実測 schema (v1)

- 日付: 2026-08-21 (UTC)
- 対象凍結版: **v1.8.5**(現行)。**本作業は凍結記録を1バイトも変更していない。**
- 作業区分: **入力データ配管のみ**(F5 = protocol §4.1 の `IDX`)。
  取引系列へ join せず、rho / シグナル / 清算発生 / return / PnL を一切計算していない。
  Layer 1/2/3 を走らせていない。Final OOS を開いていない。
  封印済み prior register を読んでいない。
- **schema は推測していない。** 実装前に **2020-01 と 2025-12 の2ファイルだけ**を
  取得して実測し、その結果に合わせて normalizer を書いた。

---

## 1. 取得経路

| 項目 | 値 |
|---|---|
| dataset 名 | `index_price_5m` |
| path template | `data/futures/um/monthly/indexPriceKlines/{sym}/5m/{sym}-5m-{period}.zip` |
| cadence | monthly |
| market_type | `perp_linear`(USD-M perp) |
| raw ディレクトリ | `data/raw/binance/vision/index_price_5m/BTCUSDT/`(**dataset 専用**) |
| ledger | 同ディレクトリの `download_ledger.jsonl`(**dataset 専用**) |
| 正規化先 | `data/normalized/binance/index_price_BTCUSDT_5m.parquet` |

path は `klines` / `markPriceKlines` / `premiumIndexKlines` と**同じ形**である
(`5m/` の interval ディレクトリあり、ファイル名は `{sym}-5m-{period}.zip`)。
`fundingRate` だけが別形だった(あちらは interval 階層が無い)。

---

## 2. CSV schema(実測)

12 列の kline 形式。**header の有無はファイルごとに違う**:

| 項目 | 実測(全 72 か月) |
|---|---|
| header あり | **45 か月**(`open_time,open,high,low,close,volume,close_time,quote_volume,count,taker_buy_volume,taker_buy_quote_volume,ignore`) |
| header なし | **27 か月**(いきなりデータ行) |

**「ある世代から一斉に付いた」ではない。** 2022-01〜2022-06 は
`なし → あり → なし → あり → なし → あり` と**交互に現れる**
(2022-06 以降は全て header あり)。したがって
**月から header の有無を推測してはならない。** 実装は `read_zip_rows` が
**1ファイルずつ先頭セルで判定**しており、この非単調性の影響を受けない。

```text
1577836800000,7195.36581933,7195.97491024,7181.16863636,7181.31409091,0,1577837099999,0,300,0,0,0
```

| 列 | 実測 |
|---|---|
| `open_time` | epoch ms。**バー開始時刻**。全行が5分グリッド上 |
| `open` / `high` / `low` / `close` | **index 価格**。小数8桁が主 |
| `volume` / `quote_volume` / `taker_buy_*` / `ignore` | **全行 0**(index は板の約定ではない) |
| `close_time` | **全行で `open_time + 5m − 1ms`**(`close_time_policy = "exact"`) |
| `count` | 5分間の index サンプル数。通常 300(毎秒1本) |

### 2.1 index を mark / 約定価格と取り違えない

正規化後の列名を**水準で分けた**:

| 系列 | 列 |
|---|---|
| `index_price_5m` | `index_open` / `index_high` / `index_low` / `index_close` / `index_samples` |
| `mark_price_5m` | `mark_open` / `mark_high` / `mark_low` / `mark_close` / `mark_samples` |
| `klines_5m` / `spot_klines_5m` | `open` / `high` / `low` / `close` / `volume` / `trades` / … |
| `premium_index_5m` | `premium_open` / … / `premium_samples` |

**index は複数の現物取引所から合成される参照価格**であり、`mark`(清算トリガー)とも
perp の約定価格とも別物である。列名が衝突しないので、取り違えは型の水準で防がれる。
テスト `test_index_columns_cannot_be_confused_with_mark_or_traded_prices` が固定する。

### 2.2 不変条件は**破れたら止める**

- `volume` / `quote_volume` / `taker_buy_*` が 0 でない行 → **送出して止まる**
- `close_time != open_time + 5m − 1ms` → **送出して止まる**
- `open_time` が5分グリッド上にない → **送出して止まる**
- **価格(`open`/`high`/`low`/`close`)が正でない** → **送出して止まる**
- 列数が 12 でない → **送出して止まる**

**「実測では 0 件だった」ことに依存せず、規則として持つ。**
グリッドと正値性は実測では違反 0 件だが、共有の `close_time_policy="exact"` 経路は
`close_time` しか見ないので、**IDX 側で明示的に検査する**
(Phase 7 / F1 の挙動は1文字も変えていない。テストで固定)。
非正の価格を素通しすると、下流で log を取ったときの汚染源になる(J7 と同型)。

各価格列が **CSV のどの列から来るか**も列ごとにテストで固定してある
(列名の集合だけを見るテストでは `low` と `close` の取り違えを検出できない)。

---

## 3. 封印(per-target cutoff)

`SEAL_CUTOFFS["index_price_5m"] = FINAL_OOS_START (2026-01-01)`。

- **Phase 7 の3系列の既定は1文字も変えていない**(テストで固定)。
- 上書き可能だが、上書きしても他 dataset の既定は動かない(テストで固定)。

---

## 4. 実測結果(2020-01 〜 2025-12)

| 項目 | 値 |
|---|---|
| 取得 | **72 / 72 か月**、absent 0、**公開 CHECKSUM 検証 72 / 72** |
| dataset 固有 digest | `2b20cb59c9a9d03095a2bc3083138ec15098fcfb5d77c67c8427e7058e98fc0d` |
| 行数 | **628,115** |
| 期間 | 2020-01-01T00:00:00Z 〜 2025-12-31T23:55:00Z |
| 期待バー | 631,296 |
| **欠測バー** | **3,181**(10 区間、最大 **576 本 = 2日**) |
| 封印落ち | **0**(dump が 2025-12 までなので該当行が無い) |
| 重複 | 完全重複 0 / 値の食い違い 0 / 未解決 0 |
| `index_samples == 0`(前値横引き) | **44 本** |
| `index_open <= 0` | 0 件 |

### 4.1 欠測の内訳

| 種別 | 区間 | 本数 |
|---|---:|---:|
| **日単位の dump 欠落**(288 の倍数・始点 `23:55Z`) | 7 | **3,168** |
| 短時間の停止 | 3 | 13 |

日単位の欠落は取引所の停止ではなく **Vision 側の欠落**である
(F1 mark と同じ現れ方。[phase8_input_plumbing_v1](phase8_input_plumbing_v1.md) §3.1)。

主な区間:

```text
2022-07-23 23:55Z 以降  576 本(2日)
2022-07-26 23:55Z 以降  576 本(2日)
2022-07-29 23:55Z 以降  576 本(2日)
2023-04-06 23:55Z 以降  576 本(2日)
2022-04-26 / 2022-10-01 / 2023-02-23 23:55Z 以降  各 288 本(1日)
2020-01-19 13:05Z 以降 5 本 / 2020-12-17 07:30Z 以降 4 本 / 2023-11-10 03:40Z 以降 4 本
```

### 4.2 mark の欠測との関係(**同一ではない**)

| 系列 | 欠測 | 区間 |
|---|---:|---:|
| `mark_price_5m`(F1) | 2,318 | 8 |
| `index_price_5m`(F5) | **3,181** | 10 |

**同一区間として一致するのは 4 区間だけ**である(index のみ 6 区間 / mark のみ 4 区間)。
すなわち **Vision の欠落は系列ごとに違う**。片方の欠測台帳をもう片方へ流用してはならない。

**欠測は埋めていない。** ギャップは行が存在しないまま残り、
`gap_report` が全件を明細として記録する。

---

## 5. 版管理

| 種別 | path | 版管理 |
|---|---|---|
| raw zip | `data/raw/binance/vision/index_price_5m/BTCUSDT/` | 入れない |
| 正規化 parquet | `data/normalized/binance/index_price_BTCUSDT_5m.parquet` | 入れない |
| parquet manifest | `data/manifests/binance_index_price_index_price_BTCUSDT_5m.json` | 入れる |
| **入力 inventory** | `data/manifests/phase8_index_price_v1.json` | 入れる |
| コード / テスト | `src/mce/*.py` / `tests/test_phase8_index_price.py` | 入れる |

**既存 artifact を作り直していない。** `phase8_inputs_v1.json`(F1/F2)は
registry を分けたので**1バイトも変わらない**。既存3系列の
環境非依存 digest(mark / spot / funding)も不変であることを確認した。

再現:

```bash
uv run python -m mce.binance_vision --start 2020-01 --end 2025-12 --datasets index_price_5m
uv run python -m mce.normalize_binance --datasets index_price_5m
uv run python -m mce.manifest --datasets binance_index_price
uv run python -m mce.phase8_inputs --datasets index_price_5m \
    --name phase8_index_price_v1 --json data/manifests/phase8_index_price_v1.json
```

> **`mce.manifest` は fail-closed である。** 引数なしでは何も書かずに非ゼロ終了し、
> `--datasets NAME ...` か `--all` の明示を要求する。既存4件は parquet 指紋が
> ローカルとずれているため、既定で全件を書く設計のままだと
> **無引数実行しただけで commit 済みの指紋が環境依存の値で上書きされていた**。

### 5.1 バー系列の merge 意味論(**訂正 dump は自動では入らない**)

`store.merge_parquet` は既存行を優先する(`keep="first"`)。したがって:

- 保証されるのは**追記の冪等性**であって、`parquet == f(raw)` ではない。
- **訂正された dump を取り込むには、parquet を消してから再正規化する。**
- これは IDX 固有ではなく **Phase 7 から続く全バー系列の共有挙動**であり、
  変えると Phase 7 の正規化結果が動くので**変えない**。
- イベント系列(`funding_rate`)だけは導出列を持つため全再生成であり、ここが違う。

この性質はテスト `test_merge_keeps_existing_rows_when_raw_is_corrected` が固定する。

---

## 6. 本作業で**していないこと**

- index を取引系列・他の5分系列へ join していない
- `features_carry` / rho / シグナル / return / PnL / runner / report を実装していない
- funding mark resolver を変更していない(**独立してコミットできる状態のまま**)
- `phase8_prereg.py` / `two_leg.py` / `mark_path.py` / `rho.py` / `splits.py` を変更していない
- 既存 canonical parquet・既存 manifest・凍結記録を変更していない
- 欠測を補間していない・`index_samples == 0` のバーを落としていない
- Layer 1/2/3 を走らせていない。Final OOS を開いていない
- **v1.8.6 を適用も凍結もしていない**

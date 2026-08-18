# Phase 8.1 — 入力データ配管 F1 / F2 (v1)

- 日付: 2026-08-18 (UTC)
- 対象凍結版: **v1.8.4**(現行)。**v1.8.5 は凍結していない。**
- 作業区分: **入力データ配管のみ**。取引系列へ join せず、rho / シグナル /
  清算発生 / return / PnL を一切計算していない。
- `two_leg.py` を変更していない。**F3(`Bar` への `perp_high` 追加)は v1.8.5 の作業。**
- Layer 1/2/3 を走らせていない。Final OOS を開いていない。

---

## 1. 追加した2系列

| # | dataset | market | Vision path | 用途 |
|---|---|---|---|---|
| **F1** | `mark_price_5m` | USD-M perp | `data/futures/um/monthly/markPriceKlines/BTCUSDT/5m/` | **清算トリガーの入力** |
| **F2** | `spot_klines_5m` | **spot** | `data/spot/monthly/klines/BTCUSDT/5m/` | **spot 脚** |

取得: 2020-01 〜 2025-12 の 72 か月、**両系列とも 72/72 saved・absent 0・
公開 CHECKSUM 検証 72/72**。

### 1.1 mark と約定価格を混同しない

正規化後の列名を**水準で分けた**:

| 系列 | 列 |
|---|---|
| `mark_price_5m` | `mark_open` / `mark_high` / `mark_low` / `mark_close` / `mark_samples` |
| `spot_klines_5m` / `klines_5m` | `open` / `high` / `low` / `close` / `volume` / `trades` / … |

`mark_high` は**清算トリガーの入力**、`perp_high`(= `klines_5m` の `high`)は
**執行価格の代理**である。列名が衝突しないので、取り違えは型の水準で防がれる。
テスト `test_mark_price_columns_cannot_be_confused_with_traded_prices` が固定する。

---

## 2. 出所・digest・時刻規約

`data/manifests/phase8_inputs_v1.json`(版管理へ入れる)に次を記録した。

| 項目 | 内容 |
|---|---|
| 出所 | `source=binance`, `base_url`, `market_type`, `cadence`, `path_template` |
| **dataset 固有 digest** | 公開 zip の SHA-256 から作る**環境非依存**の指紋。`mark_price_5m` = `212c61ae8892…`、`spot_klines_5m` = `b6d13ee3b315…`(**別値**であることをテストで固定) |
| 時刻規約 | `open_time` = バー開始時刻。行 t は `[ts, ts+5m)`。5分グリッド。UTC |
| 封印 | `seal_cutoff = 2026-01-01T00:00:00Z`(Phase 7 の継承。dump は 2025-12 までなので落ちた行は 0) |
| 重複分類 | 完全重複 / 値の食い違い / 所有ファイルによる解決 / 未解決 |
| 欠測分類 | 欠測バー数と**ギャップ全件の明細**(要約で丸めない) |

### 2.1 Phase 7 の digest 不変性

`source_digest` は **dataset ごとの ledger** から作るので、系列を足しても
既存 dataset の digest は動かない。`raw_dir` と `ledger_path` も dataset ごとに
分かれている。Phase 7 の3系列については

- `path_template` / `cadence` / `market_type` を1文字も変えていない
- 正規化先のファイル名を変えていない

ことをテストで固定した(`test_phase7_dataset_specs_are_byte_identical`)。
Phase 7 の manifest は再生成していない(該当 parquet が本環境に無いため)。

---

## 3. 取り込んだ結果

| dataset | rows | 期待バー | 欠測バー | ギャップ本数 | 最大ギャップ |
|---|---|---|---|---|---|
| `mark_price_5m` | 628,978 | 631,296 | **2,318** | 8 | **1,152 本(4日)** |
| `spot_klines_5m` | 630,834 | 631,296 | **462** | 15 | 70 本 |

重複は両系列とも 0(完全重複・食い違い・未解決すべて 0)。封印落ちも 0。

### 3.1 F1 の欠測は**日単位の dump 欠落**を含む(重要)

```text
2020-01-19 13:05Z 以降     5 本
2020-12-17 07:30Z 以降     4 本
2021-06-30 23:55Z 以降   288 本(= 1日)
2021-07-23 23:55Z 以降  1152 本(= 4日)   <- 最大
2022-07-30 23:55Z 以降   288 本(= 1日)
2022-10-01 23:55Z 以降   288 本(= 1日)
2023-02-23 23:55Z 以降   288 本(= 1日)
2023-11-10 03:35Z 以降     5 本
```

288 の倍数で始点が `23:55Z` のものは**その日の dump が丸ごと欠けている**もので、
取引所の停止ではなく **Vision 側の欠落**である。残り3件(4〜5本)は短時間の停止。

**清算トリガーは mark で判定する規約なので、これらの日は清算判定ができない。**
どう扱うか(その期間を trade 候補から外す等)は **v1.8.5 の設計判断**であり、
**本作業では決めていない。埋めてもいない。**

### 3.2 F2 の欠測は取引停止で、`close_time` 異常と同じ事象

spot の 15 本のギャップはいずれも短く(12〜70 本)、うち7本は
**ギャップ直前のバーが `close_time` 異常のバーそのもの**である
(2020-02-19 11:35 / 2020-03-04 09:20 / 2020-12-21 14:05 / 2021-02-11 03:40 /
2021-04-25 04:00 / 2021-08-13 01:55 / 2023-03-24 12:35)。

すなわち **停止直前の切れたバー → 停止中は行が無い → 再開**、という
1つの事象の2つの現れ方である。

---

## 4. 実測で判明した dump の意味論の違い(**黙って吸収していない**)

### 4.1 `close_time` は dump ごとに意味が違う

`normalize_klines` は元々 `close_time == open_time + 5m − 1ms` を**厳格に要求**して
いた。spot dump ではこれが 630,834 行中 **8 行**で成り立たない。

内訳(全件):

| 期間 | open (UTC) | Δ(close − open) | 出来高 |
|---|---|---|---|
| 2020-02 | 2020-02-19 11:35 | +32,286 ms | 2.71 |
| 2020-03 | 2020-03-04 09:20 | +106,694 ms | 4.06 |
| 2020-12 | 2020-12-21 14:05 | **−1,059,479 ms** | **0** |
| 2021-02 | 2021-02-11 03:40 | +54,773 ms | **0** |
| 2021-04 | 2021-04-25 04:00 | +58,146 ms | 5.89 |
| 2021-08 | 2021-08-13 01:55 | +299,000 ms | 55.51 |
| 2021-12 | 2021-12-24 04:55 | +294,362 ms | 41.23 |
| 2023-03 | 2023-03-24 12:35 | +281,646 ms | **0** |

**8行すべて `open_time` は5分グリッド上にある。** `close_time` は
**そのバーの最終約定時刻**であり、出来高 0 のバーでは最終約定がバー開始より
前になることすらある(2020-12-21 の負の Δ)。

対処: `DatasetSpec.close_time_policy` を**dataset ごとに**持たせた。

| policy | 適用 | 不変条件 |
|---|---|---|
| `"exact"` | `klines_5m` / `premium_index_5m` / **`mark_price_5m`** | `close_time == open_time + 5m − 1ms`。**Phase 7 の挙動を変えていない**。F1 は実測 628,978 行すべてで成立 |
| `"last_trade_time"` | **`spot_klines_5m`** | `open_time` が5分グリッド上にあること。`close_time` の逸脱は**落とさず分類して数える** |

記録される計数: `close_time_not_bar_end_rows = 8`、`close_time_before_open_rows = 1`。
**行を落としていない。閾値も置いていない。**

### 4.2 mark の停止バー

`mark_samples`(dump の `count` 列)は5分間の mark サンプル数で、通常 300
(毎秒1本)。**43 行が 0** であり、mark が更新されないまま前値が横引きされている。

| 日 | 停止バー数 |
|---|---|
| 2020-07-27 | 23 |
| 2020-12-17 | 2 |
| 2021-03-02 | 11 |
| 2022-07-12 | 7 |

値は捏造ではないので**落としていない**が、清算トリガーの入力としては品質が違うため
`mark_stale_bars` として計数して記録する。**この扱いも v1.8.5 の設計判断**であり、
本作業では決めていない。

また `volume` / `quote_volume` / `taker_buy_*` が 0 でない markPriceKlines 行は
**取り込まない**(mark は板の約定ではない)。実測では全行 0 だった。

---

## 5. 版管理

| 種別 | path | 版管理 |
|---|---|---|
| raw zip | `data/raw/binance/vision/{mark_price_5m,spot_klines_5m}/BTCUSDT/` | 入れない |
| 正規化 parquet | `data/normalized/binance/{mark_price,spot_klines}_BTCUSDT_5m.parquet` | 入れない |
| parquet manifest | `data/manifests/binance_{mark_price,spot_klines}_*.json` | 入れる |
| **入力 inventory** | `data/manifests/phase8_inputs_v1.json` | 入れる |
| コード / テスト | `src/mce/phase8_inputs.py` ほか | 入れる |

再現:

```bash
uv run python -m mce.binance_vision --start 2020-01 --end 2025-12 \
    --datasets mark_price_5m spot_klines_5m
uv run python -m mce.normalize_binance --datasets mark_price_5m spot_klines_5m
uv run python -m mce.manifest
uv run python -m mce.phase8_inputs --json data/manifests/phase8_inputs_v1.json
```

---

## 6. 本作業で**していないこと**

- 2系列を取引系列へ join していない
- rho / シグナル / **清算発生** / return / PnL を計算していない
- `two_leg.py` を変更していない(**F3 は v1.8.5**)
- **v1.8.5 を凍結していない**
- H14b を実装していない。H14a の fallback を有効化していない
- Layer 1/2/3 を走らせていない。Final OOS を開いていない。封印済み prior register を読んでいない

**H13 は実装完了。認証済みの観測が外部から供給されるまで着手しない。**
**H14a / H14b は未解決のまま。**

---

## 7. v1.8.5 へ持ち越す設計判断(**私は決めていない**)

| # | 論点 |
|---|---|
| **P1** | F1 の日単位欠落(最大4日、計 2,318 本)の期間で清算判定をどうするか |
| **P2** | `mark_stale_bars`(43 本)をトリガー入力として使うか外すか |
| **P3** | F2 の停止ギャップ(462 本)を spot 脚の欠測としてどう扱うか(M6a / M6b の片脚欠落規則との関係) |
| **P4** | F3: `Bar` への `perp_high` 追加と再凍結 |


# Phase 7 Tier 0 — 取り込み・observable 契約 v1

- 作成日: 2026-08-16
- 状態: **取り込み完了・品質レポート生成済み・ラベル未閲覧・仮説未登録**
- 上位設計: [information_space_expansion_v1](information_space_expansion_v1.md)
- 実装: `src/mce/binance_vision.py` / `normalize_binance.py` / `features_tier0.py` / `tier0_quality.py`
- 一次記録: `experiments/phase7/tier0_quality_v1.json`(機械可読レポート)

本タスクの範囲は **データを揃えて品質を確認するところまで**である。
target・将来リターン・効果量は一切計算しない。仮説の事前登録は次のタスク。

## 1. 対象と venue

| 項目 | 値 |
|---|---|
| venue | Binance USDT-M perpetual(`futures/um`) |
| symbol | `BTCUSDT` |
| 期間 | `2020-01-01` 〜 `2025-12-31`(= `ts < 2026-01-01`) |
| 情報集合 | T0-A 集約 aggressive flow / T0-B derivatives state / T0-C basis |
| replication class | `cross_exchange_validation`(Judge を凍結してある OKX とは別 venue) |

**なぜ別 venue か**: OKX は funding 約3ヶ月・OI 約5日しか遡れず、板・約定の歴史 API が無い
([data_sources](../data_sources.md))。Tier 0 の観測量を**深い履歴**で検定できるのは
現状 Binance Vision の一括ダンプだけである。したがって Tier 0 screening は
「Binance 内で完結した情報存在検定」であり、OKX 執行を前提とした主張へ直接は格上げしない。

## 2. データ源(すべて 2026-08-16 に実測)

| dataset | 公開粒度 | パス | サイズ |
|---|---|---|---|
| `klines_5m` | 月次 | `monthly/klines/BTCUSDT/5m/` | 約 0.4 MB/月 |
| `metrics_5m` | **日次のみ** | `daily/metrics/BTCUSDT/` | 約 11–12 KB/日 |
| `premium_index_5m` | 月次 | `monthly/premiumIndexKlines/BTCUSDT/5m/` | 約 0.18 MB/月 |

- 各 zip の公開 `.CHECKSUM`(SHA-256)を**必ず検証**して保存する。不一致は保存しない。
- 404 は失敗ではなく「その period は未公開」として ledger に記録する
  (metrics は 2020-08 以前が未公開)。
- raw は immutable。再実行しても再取得しない(冪等)。
- **再配布しない**。ローカル個人研究の範囲に留める(ToS は要確認のまま)。

出所指紋は `mce.binance_vision.source_digest()` が `<period> <sha256>` を period 昇順に
並べた本文の SHA-256 として計算する。**parquet の sha256 と違い環境非依存**で、
同じ期間を取得した誰の環境でも一致する。

## 3. 時刻規約(data contract §2 の適用)

| dataset | 元の時刻列 | 解釈 |
|---|---|---|
| klines / premiumIndexKlines | `open_time` | **バー開始時刻**。行 t は `[ts, ts+5m)` を表す(OKX candle と同じ) |
| klines | `close_time` | 常に `open_time + 5m − 1ms`。正規化時に**全行検査**して不一致なら例外 |
| metrics | `create_time` | **5分ごとのスナップショット時刻**(UTC の naive 文字列)。区間集約ではない |

- 内部保持は UTC(ms 精度・tz 付き)。
- 2025年以降の dump が µs 精度で配布される場合に備え、`_epoch_ms()` が桁で判定して ms へ揃える。
- **欠損は補間しない**。欠測は品質レポートが検出・列挙する。
- join は **ts 完全一致**のみ。as-of による前方持ち越しはしない
  (metrics のスナップショットが無いバーは null のまま)。

## 4. observable 契約(features)

`data/features/binance_BTCUSDT_5m.parquet` を `mce.features_tier0` が生成する。

```text
baseline (A) : mce.features.build_features と同一コード(OKX と同じ定義)
Tier 0  (X)  : 下表
```

| 列 | availability | 由来・定義 |
|---|---|---|
| `taker_buy_ratio` | close_of_bar | `taker_buy_volume / volume`(分母0なら null) |
| `taker_buy_quote_ratio` | close_of_bar | `taker_buy_quote / volume_quote` |
| `trade_count` | close_of_bar | kline の `count` |
| `avg_trade_size` | close_of_bar | `volume / count` |
| `avg_trade_notional` | close_of_bar | `volume_quote / count` |
| `open_interest` | start_of_bar | metrics `sum_open_interest`(BTC 建て) |
| `open_interest_value` | start_of_bar | metrics `sum_open_interest_value`(USDT 建て) |
| `top_trader_account_ls_ratio` | start_of_bar | `count_toptrader_long_short_ratio` |
| `top_trader_position_ls_ratio` | start_of_bar | `sum_toptrader_long_short_ratio` |
| `global_account_ls_ratio` | start_of_bar | `count_long_short_ratio` |
| `taker_ls_vol_ratio` | start_of_bar | `sum_taker_long_short_vol_ratio` |
| `premium_open` | start_of_bar | premiumIndexKlines の open |
| `premium_close` | close_of_bar | premiumIndexKlines の close |

判定根拠:

- **close_of_bar** = バー区間 `[ts, ts+5m)` の集約。signal はバー close 後に作るので利用可能。
- **start_of_bar** = `ts` 時点のスナップショット。signal 時刻(bar close)から見て
  **5分以上前**の情報しか使わない保守側の割り当て。公開遅延が未実測でも
  この 5 分バッファがあるため leakage しない
  (OKX OI を observable へ昇格させない理由([data_contract §8](../data_contract.md))とは
  状況が異なる: あちらは同時刻 join だった)。
- OKX 固有の `funding_rate` / `oi` / `oi_usd` は供給元が無いので **列ごと落とす**
  (null で埋めて「あるように見せる」ことをしない)。

**ラベルは一切生成・参照しない。** 出力に `fwd_` 列が無いことを毎回検査し、
`mce.tier0_quality` が `mce.labels` を import しないことをテストで固定している。

## 5. 封印の継承(Final OOS firewall)

正規化時に `ts >= 2026-01-01`(= `splits.FINAL_OOS_START`)の行を落とす。
別 venue のデータでも**封印期間と同じ暦期間を screening で見ない**ため
([expansion protocol §6.3](information_space_expansion_v1.md))。
落とした行数は会計として記録し、品質ゲートで `sealed_rows_present == 0` を要求する。

## 6. 品質ゲート(結果を見てから緩めない)

`mce.tier0_quality` が機械判定する。1つでも FAIL なら CLI は非ゼロ終了する。

| gate | 意味 |
|---|---|
| `<ds>:on_5m_grid` | 全 ts が5分グリッド上 |
| `<ds>:strictly_increasing` | 重複排除後に単調増加 |
| `<ds>:utc` | tz が UTC |
| `<ds>:no_sealed_rows` | 封印期間の行が無い |
| `<ds>:no_conflicting_duplicates` | 同一 ts で値が食い違う行が無い |
| `<ds>:no_null_columns` | normalized に null が無い |
| `klines_5m:taker_buy_within_volume` | taker buy ≤ 総量(単位の整合) |
| `klines_5m:ohlc_consistent` | high ≥ max(open,close)、low ≤ min(open,close) |
| `features:no_forward_looking_columns` | features に `fwd_` 列が無い |
| `features:metrics_join_is_exact` | grid 外 snapshot・非正値が features へ紛れていない(本数照合) |
| `okx_consistency:close_median_diff_under_10bps` | 同時刻 close の相対差 中央値 < 10bps |
| `okx_consistency:return_correlation_over_0.9` | 5分リターン相関 > 0.9 |

`on_5m_grid` は**バーを表すデータセット(klines / premiumIndex)にのみ**適用する。
バーは `[ts, ts+5m)` を表すので grid 外は契約違反だが、snapshot 系(metrics)は
上流が数秒ずれることが実在するため(§7-4)、件数を報告した上で
「features へ混入していないこと」を別ゲートで確認する。

欠測(gap)は**ゲートにしない**。5分足の欠測は起きうる事実であり、
「欠測ゼロ」を通過条件にすると窓を選ぶ誘惑が生まれる。件数・最長ギャップ・
上位ギャップを**全て報告**し、判断は screening の事前登録時に行う。

## 7. 既知の注意点(すべて実データで確認した事実)

1. **premiumIndexKlines の volume / taker 系は常に 0**。約定フローではないので
   正規化時に捨て、`premium_samples`(件数)だけ残す。
2. **metrics dump には重複行が大量にある**(2020年は1日576行 = 288×2 の完全重複)。
   `(source, symbol, ts)` で重複排除する(実測 75,257 行)。
3. **日付境界の食い違い重複が2件実在する**(2024-04-08 00:00 と 2024-05-01 00:00 が
   前日ファイルにも入っており、OI が僅かに違う)。**`ts` の日付と一致する
   ファイルを所有者として採る**決定的規則で解決し、解決できない食い違いは
   `unresolved_conflicts` としてゲートで落とす。
4. **metrics のスナップショットが5分グリッドから 1〜4 秒ずれる期間がある**
   (2024-04-04 〜 2024-04-30 の 143 行)。値は書き換えず、**ts 完全一致 join の
   結果として features からは自動的に除外される**。除外が正しく効いていることを
   `features:metrics_join_is_exact` ゲートが本数で照合する。
5. **OI = 0 の穴が 473 行(OI notional = 0 は 485 行)ある**(2021〜2025 に散在)。
   normalized は source どおり保持し、**observable(features)では非正値を null に
   する固定規則**を置く(log / z-score を壊さないため)。この規則は
   ラベル閲覧前に決めたものであり、結果を見て変更しない。
6. **long/short ratio 系には空欄が多い**(`""` 引用符つきの空セル)。
   top trader 系 92k 行、taker L/S 37k 行、global 5.8k 行。値を捏造せず null にする。
   → **T0-B の被覆は列ごとに違う**(§9)。
7. **metrics は 2020-09 以前が未公開**(244 period が 404)。T0-A と T0-B/T0-C で
   利用可能期間が異なるので、入れ子比較は**共通の有効サンプル**で行う(次タスクで事前登録)。
8. Binance と OKX は別 venue。価格は一致するが**出来高水準は約 2.2 倍違う**(§9)。
   flow 系の絶対量を venue 間で比較しない。
9. parquet の sha256 は環境(polars 版・圧縮)で変わりうる。**再現性の根拠は
   `source_digest` と行数・期間**であって parquet の sha256 ではない。
10. CSV は引用符つきセルを含むので `split(",")` ではなく CSV parser で読む。
    古い dump は header 行が無いので、先頭セル名(`open_time` / `create_time`)で判定する。

## 8. 再現手順

```sh
# 1. 取得(公開 CHECKSUM を検証。冪等・再実行安全)
uv run python -m mce.binance_vision --start 2020-01 --end 2025-12

# 2. 正規化(ts >= 2026-01-01 は落とす)
uv run python -m mce.normalize_binance

# 3. observable features(ラベルは作らない)
uv run python -m mce.features_tier0

# 4. 指紋
uv run python -m mce.manifest

# 5. 品質レポート(ゲート不通過なら非ゼロ終了)
uv run python -m mce.tier0_quality --json experiments/phase7/tier0_quality_v1.json
```

## 9. 実測結果(2026-08-16)

数値の正は `experiments/phase7/tier0_quality_v1.json`。以下は要約。**全ゲート通過**。

| dataset | rows | 期間(UTC) | 欠測 | 最長ギャップ | 重複除去 | 食い違い | grid外 |
|---|---:|---|---:|---:|---:|---:|---:|
| klines_5m | 631,296 | 2020-01-01 00:00 〜 2025-12-31 23:55 | **0** | 0 | 0 | 0 | 0 |
| metrics_5m | 560,388 | 2020-09-01 00:00 〜 2025-12-31 23:55 | 636 (0.11%) | 630分 | 75,257 | 2(解決済) | 143 |
| premium_index_5m | 629,246 | 2020-01-01 00:00 〜 2025-12-31 23:55 | 2,050 (0.32%) | 5,765分 | 0 | 0 | 0 |

出所指紋(環境非依存):

| dataset | files | checksum 検証 | 未公開 period | source digest |
|---|---:|---:|---:|---|
| klines_5m | 72 | 72 | 0 | `768e4996…` |
| metrics_5m | 1,948 | 1,948 | 244 | `364700f3…` |
| premium_index_5m | 72 | 72 | 0 | `3d8257b3…` |

### observable features

`data/features/binance_BTCUSDT_5m.parquet` — 631,296 行(2020-01-01 〜 2025-12-31)。

| 列 | 被覆 | 欠測の主因 |
|---|---:|---|
| `trade_count` | 1.000 | — |
| `taker_buy_ratio` / `taker_buy_quote_ratio` / `avg_trade_*` | 0.9999 | 出来高0のバー 66本 |
| `premium_open` / `premium_close` | 0.997 | premium dump の欠測 2,050本 |
| `open_interest` / `open_interest_value` | 0.887 | metrics が 2020-09 開始 + OI=0 の穴 |
| `global_account_ls_ratio` | 0.878 | 上記 + 空欄 5,797 |
| `taker_ls_vol_ratio` | 0.828 | 上記 + 空欄 37,271 |
| `top_trader_*_ls_ratio` | 0.741 | 上記 + 空欄 92,226 / 92,192 |

**T0-A(flow)はほぼ全期間で使えるが、T0-B(derivatives)の被覆は 74〜89% で、
列によって欠測構造が違う。** 入れ子比較は同一の有効サンプル上で行う必要があり、
どの列をどの期間で使うかは事前登録で固定する。

### 分布(記述統計のみ)

- klines: `taker_buy_ratio` 中央値 **0.497**(買い/売り taker はほぼ拮抗)、
  約定件数 中央値 7,278/5分、出来高0のバー 66本
- metrics: OI 中央値 82,292 BTC、OI notional 中央値 31.2億 USDT、
  含意価格(= OI value / OI)中央値 43,579 USDT(価格水準として妥当)
- premium: 中央値 **−0.000324**、p01 −0.000909、p99 +0.001771(|premium| > 10% は 0件)

### 既存 OKX OHLCV との整合(同時刻・ラベル非使用)

ローカルに再取得した OKX 5分足(`ts < 2026-01-01` に限定)と突き合わせた。

| 指標 | 値 |
|---|---|
| 重なり | 225,332 本(OKX 側のみ存在する本数 **0**) |
| close 相対差 \|Δ\| | 中央値 **3.46 bps** / p99 7.68 bps / 最大 87.1 bps |
| 5分リターン相関 | **0.9983** |
| 出来高比(binance/okx)中央値 | 2.23 |

同一資産の別 venue として妥当な一致。**価格系列は同じものを見ている**と判断してよい。
一方で出来高水準は 2.2 倍違うので、**flow 系の絶対量を venue 間で比較してはならない**
(比率・z-score で扱う)。

## 10. 次のタスク(本タスクの範囲外)

Tier 0 の **incremental information test の事前登録**:
target・horizon・入れ子モデル・placebo 対照・最低サンプル・multiple testing family・
go/no-go を、**効果量を一切見ない状態で**凍結する
([expansion protocol §6](information_space_expansion_v1.md))。

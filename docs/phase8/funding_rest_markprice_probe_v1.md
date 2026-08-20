# Phase 8.1 — 公式 REST の funding markPrice 実測 (v1)

- 日付: 2026-08-20 (UTC)
- 対象凍結版: **v1.8.5**(現行)。**本作業は凍結記録を1バイトも変更していない。**
- 作業区分: **入力データ配管のみ**。取引系列へ join せず、rho / シグナル /
  清算発生 / return / PnL を一切計算していない。Layer 1/2/3 を走らせていない。
  Final OOS を開いていない。封印済み prior register を読んでいない。
- 外部操作は **公開 `GET /fapi/v1/fundingRate` のみ**。認証していない。注文していない。

---

## 0. 結論(先に書く)

**決済時点の markPrice は 2023-10-31T08:00:00Z より前には存在しない。**

REST は 6,576 決済すべてを返し、Vision と **6,576/6,576 が一対一で一致**した
(timestamp のずれは全件 0 ms、funding rate の不一致 0 件)。しかし
`markPrice` は **4,198 件が空文字列**であり、値があるのは **2,378 件**だけである。

| layer | 決済数 | markPrice あり | なし | 被覆 |
|---|---:|---:|---:|---:|
| layer 1(2020-01-01 〜 2025-06-01) | 5,934 | 1,736 | **4,198** | **29.3%** |
| layer 2(2025-06-01 〜 2026-01-01) | 642 | 642 | 0 | **100%** |

> **§8.1 の `cash_flow(s) = q · MarkPrice(s) · f(s)` は、layer 1 の前半について
> 一次情報から計算できない。** これは取得の失敗ではなく、**取引所が公開している
> 履歴の範囲そのもの**である。
>
> **`markPriceKlines` の5分足 close で代用してはならない**(本作業の禁止事項であり、
> §8.1 の「やむを得ず代理にする場合」とは別の話である。代理を使うなら
> `ts = s − 5m` のバーであることと、代理であることの明示が要る。Y14)。
> **欠測は埋めていない。** null のまま残し、件数を artifact に記録した。

---

## 1. 経路と安全性

| 項目 | 値 |
|---|---|
| host | `https://fapi.binance.com` |
| allowlist | **`/fapi/v1/fundingRate` の1本のみ** |
| 認証 | **しない**(公開エンドポイント。API key も署名も使わない) |
| 書き込み | **経路が存在しない**(POST / PUT / DELETE をモジュールが持たない) |
| rate limit | 500 req / 5min / IP を `/fapi/v1/fundingInfo` と共有。逐次 + 最小間隔 0.35s |
| 生レスポンス | **保存しない。** SHA-256・取得 UTC・要求範囲だけを残す |

到達性: 本環境から `fapi.binance.com` は 200 を返す
(2026-08-18 の記録では地域制限で遮断されていた。egress が変わっている)。

---

## 2. 公式仕様(2026-08-20 に取得)と実測の突き合わせ

| 項目 | 公式仕様 | 実測 |
|---|---|---|
| 認証 | 不要 | 不要で 200 |
| `limit` | 既定 100 / 最大 1000 | 1000 で取得できた |
| `startTime` | inclusive | **inclusive**(`startTime == fundingTime` でその件が返る) |
| `endTime` | inclusive | **inclusive**(`endTime == fundingTime` でその件が返る) |
| 超過時 | `startTime + limit` で切り詰め | 昇順で先頭から 1000 件 |
| 未指定時 | 直近 200 件 | 使っていない(常に範囲を明示) |

**境界の実測**(2020-01):

```text
startTime=1577836800000, endTime=1577836800000        -> 1 件(両端 inclusive)
startTime=1577836800000, endTime=1577865599999        -> 1 件
startTime=1577836800001, endTime=1577894400000        -> 2 件(+1ms で先頭を外せる)
```

> したがって**頁送りは「最終 `fundingTime` + 1ms」から**でなければならない。
> +1ms しないと境界の1件を必ず二重取得する。テストで固定した。

**ページング順序**: ページ内は**厳密昇順**、重複なし。全期間 6,576 件を
**7 ページ**(1000×6 + 576)で取得し、重複・逆転・無進行はいずれも 0 件。

---

## 3. レスポンスの全フィールド(実測)

2020-01 と 2025-12 の 186 件すべてで **5 フィールド**、増減なし:

```json
{"symbol":"BTCUSDT","fundingTime":1577836800000,
 "fundingRate":"-0.00012359","markPrice":"","rateType":"Regular"}
```

| フィールド | 型 | 実測 |
|---|---|---|
| `symbol` | string | 全件 `BTCUSDT` |
| `fundingTime` | int (ms) | **決済時刻**。Vision `calc_time` と**全件 0 ms 差** |
| `fundingRate` | string | Vision `last_funding_rate` と**全件一致** |
| `markPrice` | string | **2020-01 は全 93 件が空文字**、2025-12 は全 93 件が値 |
| `rateType` | string | 全 6,576 件が `Regular`(`Special` は0件) |

`rateType` は公式仕様では `Regular` / `Special` の2値で、`Special` は
株式配当由来の追加 funding を表す。**捨てずに列として保持している。**

---

## 4. `fundingTime` の時刻意味論

- **決済時刻**である(protocol X4 の主張と整合)。Vision `calc_time` と
  **6,576 件すべてで 0 ms 差**だった。
- バー開始時刻ではない。`[ts, ts+5m)` の区間解釈をしてはならない。
- Vision 側で観測されているサブ秒ジッタ(正時 + 0〜47ms。全 6,576 決済の実測)は REST 側にも同じ値で
  現れる(例: `1767196800001` = 2025-12-31T16:00:00.001Z が両系列に一致して存在)。

---

## 5. 照合(canonical は **Vision**)

**REST の値で Vision の funding rate を置換していない。** Vision を canonical とし、
REST は markPrice を供給する第2の観測として**照合するだけ**である。

### 5.1 許容差を実測してから固定した

**timestamp の完全一致を前提にしていない。** まず probe で差分を実測し、
その後に許容差を凍結した:

```text
FUNDING_TIME_TOLERANCE_MS = 1000   (= 1 秒)
```

| 根拠 | 値 |
|---|---|
| probe 2か月(186 件)の実測差 | **全件 0 ms** |
| 全期間(6,576 件)の実測差 | **全件 0 ms**、最大絶対値 0 ms |
| 最短の決済間隔(実測) | 28,799,985 ms(約 8h) |
| 許容差 / 最短間隔 | **約 1 / 28,800** |

0 に固定すると「完全一致を仮定するな」に反するため、Vision 側に実在する
サブ秒ジッタ(全 6,576 決済で 0〜47ms)を吸収する幅として 1 秒を採った。仮に将来 1 時間間隔へ
切り替わっても(X5)、許容差は間隔の 1/3,600 であり隣の決済を取り違えない。

### 5.2 曖昧なら拒否する

| `match_status` | 意味 |
|---|---|
| `matched` | 一対一で、**rate も一致**した |
| `rate_mismatch` | 一対一だが rate が違う。**`matched` にしない** |
| `ambiguous_multiple_rest` | 許容差内に REST 候補が**複数**。近い方を選ばず拒否 |
| `ambiguous_shared_rest` | 1つの REST 行に**複数の Vision 決済**が寄った(多対一) |
| `unmatched_vision` | 許容差内に候補が無い |

一致しなかった行は**落とさない**。理由つきで照合表に残す。

### 5.3 全期間の照合結果

| 項目 | 件数 |
|---|---:|
| Vision(canonical)決済 | **6,576** |
| REST 決済 | **6,576** |
| **一対一一致(`matched`)** | **6,576** |
| unmatched Vision | 0 |
| unmatched REST | 0 |
| rate 不一致 | 0 |
| 曖昧(複数候補 / 多対一) | 0 |
| timestamp offset 分布 | **`{0ms: 6576}`** |
| 期間 | 2020-01-01T00:00:00Z 〜 2025-12-31T16:00:00.001Z |
| 重複 `fundingTime` | 0 |
| 順序異常 | 0(厳密昇順) |
| 封印落ち | 0(要求範囲が cutoff 未満のため該当なし) |

### 5.4 markPrice の会計

| 分類 | 件数 |
|---|---:|
| `present`(正の値) | **2,378** |
| `empty`(空文字列) | **4,198** |
| `unparseable` | 0 |
| `non_positive`(0 以下) | 0 |

**欠測と値ありは単一の切り替わり**である(混在していない):

```text
最後に markPrice が無い決済 : 2023-10-31T00:00:00.001Z
最初に markPrice がある決済 : 2023-10-31T08:00:00Z
```

これより後の 2,378 件はすべて値があり、これより前の 4,198 件はすべて空である。

---

## 6. 保存物

| 種別 | path | 版管理 |
|---|---|---|
| REST 系列 | `data/normalized/binance/funding_rate_rest_BTCUSDT.parquet` | 入れない |
| 照合表 | `data/normalized/binance/funding_reconciliation_BTCUSDT.parquet` | 入れない |
| inventory | `data/manifests/phase8_funding_rest_v1.json` | **入れる** |
| コード / テスト | `src/mce/binance_rest.py` / `src/mce/phase8_funding_rest.py` ほか | 入れる |

**Vision 系列(canonical)と `phase8_funding_v1.json` は1バイトも変更していない。**

再現:

```bash
uv run python -m mce.binance_rest --start 2020-01 --end 2025-12
uv run python -m mce.phase8_funding_rest --json data/manifests/phase8_funding_rest_v1.json
```

再実行しても**データは1行も動かない**(`retrieved_at_utc` だけが新しい取得時刻に
更新される。冪等なのはデータであって出所の時刻印ではない)。

---

## 7. 本作業で**していないこと**

- REST の rate で Vision の canonical funding rate を置換していない
- markPrice の欠測を補完していない・`markPriceKlines` の5分足 close で代用していない
- IDX / carry 特徴量 / rho / シグナル / return / PnL / runner を実装していない
- 認証 API・注文 API を使っていない(モジュールに経路が存在しない)
- 2026-01-01 以降を**要求していない・保存していない**
- Layer 1/2/3 を走らせていない。Final OOS を開いていない
- **凍結記録・凍結済みファイルを1バイトも変更していない**

---

## 8. 下流への含意(**判断はしていない。事実だけ記録する**)

1. **layer 2 は決済時 mark が 100% 揃っている。** §8.1 の funding 受払を
   一次情報だけで計算できる。
2. **layer 1 は 2023-10-31 より前の 4,198 決済で mark が無い。** §8.1 を
   一次情報だけでは満たせない。代理を使うか、その期間の扱いを決めるかは
   **設計判断であり、本作業では決めていない。**
3. `rateType` は実測範囲で全件 `Regular` である。`Special` の扱いを決める必要は
   現時点では生じていないが、列は保持してある。

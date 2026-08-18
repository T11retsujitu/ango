# Phase 8.1 — P1 / P2 mark 入力可用性プローブ 所見 (v1)

- 日付: 2026-08-18 (UTC)
- 対象凍結版: **v1.8.4**(現行)。**v1.8.5 は凍結していない。**
- 作業区分: **入力可用性の確認のみ**。清算発生・rho・シグナル・return・PnL を
  一切計算していない。**REST 値を canonical dataset へ併合していない。**

---

## 0. 結論

**プローブは実行できたが、対象 12 区間すべてが `probe_blocked_by_egress` になった。**

本セッションの egress から Binance の REST API 面**全体**が地域制限で塞がれている。
したがって **P1 / P2 のどちらについても、source 側の可用性は判定できていない。**

**12 区間のいずれも `mark_path_unobservable` と分類していない。**
経路が塞がれていることは source についての所見ではないからである。
ここで `mark_path_unobservable` と書けば、「取れなかった」を「存在しない」に
すり替えることになる。

---

## 1. 実測した遮断

| host / path | 応答 |
|---|---|
| `fapi.binance.com/fapi/v1/markPriceKlines` | `{"code":0,"msg":"Service unavailable from a restricted location according to 'b. Eligibility' …"}` |
| `fapi.binance.com/fapi/v1/ping` | **同じ制限メッセージ**(データを含まない疎通確認すら通らない) |
| `fapi1〜4.binance.com` | HTTP 302 → `https://www.binance.com/en` |
| `api.binance.com`(spot REST) | 同じ制限メッセージ |
| `data.binance.vision`(dump CDN) | **HTTP 200。到達できる** |

agent proxy の状態は正常(`recentRelayFailures: []`)であり、TLS 失敗でも
proxy 障害でもない。**Binance 側が egress IP の地域を理由に拒否している。**

静的 dump の CDN は通り、取引 API 面だけが塞がれている。すなわち
**F1/F2 の取り込み(dump 経由)が成立したことと、REST が通らないことは矛盾しない。**

### 1.1 H13 への波及(**新しい情報**)

同じ遮断は `GET /fapi/v1/commissionRate` にも当然かかる。すなわち
**H13 は資格情報が無いことに加えて、この環境からは経路も無い。**
資格情報を供給しても本セッションからは解決できない。
H13 の実行は**Binance が受け入れる地域の環境**で行う必要がある。

---

## 2. プローブの仕様(**そのまま再実行できる**)

`src/mce/mark_gap_probe.py`。公開 `GET /fapi/v1/markPriceKlines` への読み取りのみ。

対象区間:

- **P1**: Vision の markPriceKlines が欠落している **8 区間**(計 2,318 本)
- **P2**: `mark_samples == 0` の連続塊 **4 区間**(計 43 本)

各区間について、**前後 12 本の重複対照窓**を付けて1回の GET で要求する
(最大区間 1,152 本 + 24 本 = 1,176 ≤ REST 上限 1,500 なので1回で足りる)。

記録する項目(要求どおり全件):

| 項目 | フィールド |
|---|---|
| 要求区間 | `requested_start_ms/utc`, `requested_end_ms/utc` |
| 返ってきた open_time | `returned_open_times` |
| 復元できた欠測バー | `gap_bars_recovered` / `target_open_times` |
| 突き合わせた重複行 | `overlap_rows_compared` / `overlap_open_times` |
| Vision との一致 | `overlap_rows_exact`, `overlap_max_abs_diff` |
| 生応答の digest | `response_sha256` |
| 取得 UTC 時刻 | `retrieved_at_utc` |

分類規則:

| 分類 | 条件 |
|---|---|
| `candidate_deterministic_repair` | 欠測バーを**全て**復元し、**かつ**重複窓が1行以上あり**全て完全一致** |
| `mark_path_unobservable` | **REST が応答した上で**復元できない、または重複窓が不一致 |
| `probe_blocked_by_egress` | REST に到達できない/制限応答/HTML 応答。**source の所見ではない** |

重複窓が1行も突き合わせられない場合は、復元できていても `repair` としない
(対照がなければ一致を主張できないため)。テストで固定してある。

---

## 3. 結果

```text
gap    2020-01-19 12:10 .. 14:30      5 本   probe_blocked_by_egress
gap    2020-12-17 06:35 .. 08:50      4 本   probe_blocked_by_egress
gap    2021-06-30 23:00 .. 07-02 00:55  288 本   probe_blocked_by_egress
gap    2021-07-23 23:00 .. 07-28 00:55 1152 本   probe_blocked_by_egress
gap    2022-07-30 23:00 .. 08-01 00:55  288 本   probe_blocked_by_egress
gap    2022-10-01 23:00 .. 10-03 00:55  288 本   probe_blocked_by_egress
gap    2023-02-23 23:00 .. 02-25 00:55  288 本   probe_blocked_by_egress
gap    2023-11-10 02:40 .. 05:00        5 本   probe_blocked_by_egress
stale  2020-07-27 18:15 .. 22:05       23 本   probe_blocked_by_egress
stale  2020-12-17 06:25 .. 08:30        2 本   probe_blocked_by_egress
stale  2021-03-02 00:05 .. 02:55       11 本   probe_blocked_by_egress
stale  2022-07-12 12:55 .. 15:25        7 本   probe_blocked_by_egress

{"probe_blocked_by_egress": 12}
```

成果物: `experiments/phase8/mark_gap_probe_v1.json`。

---

## 4. していないこと(**明示**)

- REST 値を canonical dataset へ**併合していない**(`merged_into_canonical_dataset: false`)
- **補間していない**
- **index / premium から mark を合成していない**
- **前値の横引きを「バー内の不利側 mark 経路」の証拠として扱っていない**
  (横引きは「その5分間に mark の更新が無かった」ことしか意味しない。
  更新が無かったことは、その間に不利側へ振れなかったことを意味しない)
- 清算発生・rho・シグナル・return・PnL を計算していない
- Layer 1/2/3 を走らせていない。Final OOS を開いていない

---

## 5. 現況

| # | 状態 |
|---|---|
| **P1** | **判定できていない。** 8 区間 2,318 本の source 側可用性は未確認 |
| **P2** | **判定できていない。** 4 区間 43 本の source 側可用性は未確認 |
| **H13** | 資格情報が無く、**加えて経路も無い**。許可された地域の環境で実行が必要 |

P1 / P2 を判定するには、**Binance が受け入れる地域の egress** から

```bash
uv run python -m mce.mark_gap_probe --json experiments/phase8/mark_gap_probe_v1.json
```

を実行すればよい。分類規則も記録項目も凍結してあるので、実行環境が変わっても
同じ判断が再現される。

**この判定が付くまで、P1 / P2 の期間について「復元可能」とも「観測不能」とも
言えない。** v1.8.5 の `liquidation_state_unknown` 条項は、まさにこの
未確定を安全側に倒すための規定である。

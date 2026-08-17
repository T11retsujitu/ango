# two_leg.py — 凍結プロトコル v1.8 への適合メモ

- 実装日: 2026-08-17
- 対象: `src/mce/backtest/two_leg.py` / `tests/test_two_leg.py`
- 凍結仕様: [carry_replication_protocol_v1](carry_replication_protocol_v1.md) **v1.8(FROZEN)**
- 前提レビュー: [a3_source_review_v1](a3_source_review_v1.md)(実装より先に完了済み)
- **凍結ファイルは1バイトも変更していない**(freeze 記録の6ハッシュを再照合済み)
- **実験は実行していない。** すべて合成データによる会計の単体・適合テストである。

---

## 1. 実装した凍結条項

| 凍結条項 | 実装 | テスト |
|---|---|---|
| §5.1 両脚とも `open[t+1]` 約定 | `_next_fill_bar` は `bar.ts > signal_ts` の最初の**両脚が揃う**バーを返す | T4 ×2 |
| §6.1 時刻基準の exit(行オフセット禁止) | exit も signal 時刻から解決。行番号を使わない | T4 |
| §6.2 損益恒等式 | `pnl_gross`(basis 形)と `pnl_gross_by_legs`(脚形)を**別々に計算**し一致を検査 | T3 ×3 |
| §6.3 Arm R | **本モジュールは閾値を評価しない**。entry/exit の時刻を受け取るだけ(ρ の判定は runner の責務) | — |
| §7.1 4約定コスト | `cost_spot_in / cost_perp_in / cost_spot_out / cost_perp_out` を**個別に保持** | T34 ×2 |
| §8.1 funding 境界 | `entry_fill_ts < s <= exit_fill_ts`。按分しない | T6 ×2 |
| §8.1 markPrice | 同じ funding 行の `mark_price` を使う(A3 S1/S2) | T3 |
| X5 可変 interval | `FundingEvent.interval_hours` を行ごとに保持。cashflow 式に 8 を焼き込まない | T19 |
| §9 M1 lot 丸め | 共通 step(perp の 0.001)へ**切り下げ**、残差を記録 | T31 |
| §9 M6a | entry で片脚が無ければ**建てない** | T5 |
| §9 M6b | exit で片脚が無ければ **roll-forward**。entry を遡って無効化しない。打ち切りは既存 `ExecutionConfig.cancel_after_ms` を再利用(Y28) | T5 |
| §9 M7 | spot/futures ウォレットを**分離**。funding を証拠金に含めるか選択可能 | 2件 |
| §9 M8 | `tracking_error` を毎バー記録。**閾値で間引かない** | 1件 |
| §11.1 サイジング | `q = position_capital·L/((L+1)·S_in)`。`reserve=0` で §11.1 の式に一致 | T31 / T16 |
| §11.1 MIN_NOTIONAL | 丸め後名目が 50 USDT 未満なら理由つきで棄却 | T31 ×2 |
| §11.3 清算 | **mark の不利側 intrabar 極値**で判定。`perp_close` を使わない | T30 / T13 ×2 |
| §11.4 追証 | trigger を割ったら予備資金から target まで補充。枯渇したら清算 | T32 ×3 |
| T16 | `q·P·(1+1/L)` の L 不変性 | 2件(下記 §3) |

**A3 から採ったのは `a3_source_review_v1.md` の adopted 7件のみ。**
diagnostic threshold(`REL_REBAL_THRESHOLD` / `BUFFER_GRID_BPS` / `CALIBRATION`)は
**1つも持ち込んでいない**(既存テスト `test_a3_diagnostic_thresholds_were_not_copied` が機械強制)。

---

## 2. 実装が露出させた**凍結仕様の穴**(3件)

**いずれも既定値を置かず、呼び出し側に明示を強制した**(`UNFROZEN_PARAMETERS`)。
黙って埋めると「凍結仕様に従った」と誤認されるため。

### 2.1 【重要】予備資金と拘束資本が両立しない(§11.1 × §11.4)

```text
§11.1: q = C·L/((L+1)·S_in)、deployed_capital = C
       → spot 名目 C·L/(L+1) + 初期証拠金 C/(L+1) = C  …… C を使い切る
§11.4: 「予備資金は deployed_capital に含める」
       → しかし C には余りが無いので、予備資金は 0 にしかなり得ない
       → 追証は原理的に発火せず、§11.4 も Y35(always_on は追証なしでは存在し得ない)も
          満たせない
```

**これは実装で解決できない。** 本モジュールは `reserve_usdt` を**必須引数**にし、
`position_capital = C − reserve` として建玉を縮めることで両立させたが、
**`reserve` の値は凍結プロトコルに存在しない**。

**推奨する最小の修正(v1.8.1)**: `MARGIN_RESERVE_USDT` を凍結し、
§11.1 の式を `q = (C − R)·L/((L+1)·S_in)`、`deployed_capital = C` と明記する。
`R = 0` は §11.4 を空文にするので選べない。

### 2.2 累積 funding を維持証拠金に含めるか(§9 M7)

§9 M7 は「**累積 funding を維持証拠金計算に含めるかを明示的に凍結する**」と述べているが、
`phase8_prereg.py` に値が無い。→ `funding_counts_toward_margin` を必須引数にした。
(実務上 Binance は funding を先物ウォレット残高に反映するので `True` が自然だが、
**自然さは凍結ではない**。)

### 2.3 清算後の規則(§11.3)

§11.3 は「清算後は**凍結した規則で**再ヘッジするか巻き戻す(どちらかを事前に選ぶ)」と
述べているが、値が無い。→ `post_liquidation` を必須引数にし、
**`"rehedge"` は `NotImplementedError` で拒否する**(再建玉規則が凍結されていないため、
実装すれば発明になる)。`"unwind"` のみ実装した。

---

## 3. 精度の申告:T16 は連続量では厳密、離散量では lot 量子化まで

T16「`NARDC(L)·(1+1/L)` が清算ゼロ時に `L` 不変」は、
**丸め前の `q_raw` について厳密に成立する**(実測: 全 `L` で 10005.00、差 < 1e-9)。

一方、§11.1 が要求する **lot step 丸め**を適用すると、量子化の分だけずれる。

| L | `q_raw` | 丸め後 `q` | `q·P·(1+1/L)` 丸め前 | 丸め後 |
|---|---|---|---|---|
| 1 | 0.050000 | 0.050 | 10005.00 | 10005.00 |
| 2 | 0.066667 | 0.066 | 10005.00 | 9904.95 |
| 3 | 0.075000 | 0.075 | 10005.00 | 10005.00 |
| 5 | 0.083333 | 0.083 | 10005.00 | 9964.98 |

**これは矛盾ではなく、離散化の必然である。** テストは両方を別々に検査する
(厳密不変性 / lot 1つ分以内)。**T16 を丸め後で厳密に要求すると通らない**ので、
将来 T16 を機械判定に使うときは許容幅を lot 由来と明記すること。

---

## 4. 保守側に固定した処理順序(仕様に明記が無かったため)

バー内の順序を **清算判定 → 追証 → funding 受払**に固定した。

funding を先に入れると、その分だけ証拠金が厚くなり清算が起きにくくなる。
short の funding は平均的に受取(在庫 K9: 85.4% が正)なので、
**清算判定を先に置くのが保守側**である。docstring に明記した。

---

## 5. 本モジュールがやらないこと

1. **シグナルを生成しない。** ρ も閾値も評価しない(runner の責務)。
2. **データを取得しない。** バーと funding イベントを受け取るだけ。
3. **実験を実行しない。** 経験的な結果を1つも生成していない。
4. **Final OOS・封印された事前予想レジスタを読まない。**
5. **凍結パラメータを上書きしない。** 既定は `phase8_prereg` から取る。

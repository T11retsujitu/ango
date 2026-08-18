# two_leg.py — 凍結プロトコル v1.8.1 への適合メモ

- 実装日: 2026-08-17
- 対象: `src/mce/backtest/two_leg.py` / `tests/test_two_leg.py`
- 凍結仕様: [carry_replication_protocol_v1](carry_replication_protocol_v1.md) **v1.8.1(FROZEN)**
- 凍結記録: `experiments/phase8/carry_freeze_v1_8_1.json`(v1.8 は `carry_freeze.json` に不変保存)
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

## 2. v1.8 で露出した穴 → **v1.8.1 ですべて凍結された**

v1.8 実装が露出させた3件は、決定ログにより v1.8.1 §24.1–24.3 で確定した。
**本モジュールの必須引数から外れ、凍結既定値になった。**

| # | v1.8 での状態 | v1.8.1 の凍結値 |
|---|---|---|
| G1 予備資金 | §11.1 が C を使い切り §11.4 と非両立 | **`MARGIN_RESERVE_USDT = 2000.0`**。`R = (C−R)/(L+1)` すなわち初期証拠金1トランシェ分として導出(`R = C/(L+2)`)。`POSITION_CAPITAL = 8000`。**L を変えても R は 2000 のまま** |
| G2 funding と証拠金 | 未凍結 | **`True`**。正負いずれの funding も先物ウォレットを動かす |
| G3 清算後 | 未凍結(`rehedge` は拒否していた) | **`"unwind"`**。再ヘッジは実装しない |

**内訳の検算**(primary `C=10000, L=3`): spot 名目 6000 + 初期証拠金 2000 + 予備資金 2000 = **10000**。

**T16 の基準変更**: 丸め前の厳密不変量は `C` ではなく **`POSITION_CAPITAL_USDT`(8000)**
に対して成立する。実測で全 `L` について 8000.000000(差 < 1e-9)。

## 2A. 【訂正】イベント順序(v1.8 の記述は誤りだった)

**v1.8 の docstring と本メモ §4 は「清算判定 → 追証 → funding」と書いていた。これは誤り。**
v1.8.1 §24.4 が正しい順序を凍結した。

```text
EVENT_ORDER = "funding_then_margin_then_topup_then_liquidation"

1. 適格な funding を先物ウォレットへ適用する
2. 不利側 mark 経路で証拠金を評価する
3. TOPUP_TRIGGER(0.010)は維持証拠金(0.004)より上なので、清算より先に処理する
4. 追証の後、なお維持証拠金以下なら清算する
```

実装・docstring・テスト(T40)をこの順序に揃えた。
**T40 は「funding を先に入れると同じバーで清算を免れる」ことを合成データで示す**ので、
順序が逆戻りしたら落ちる。

## 3. G5 — 清算会計の修正(v1.8 実装の誤りの是正)

| # | v1.8 の欠陥 | v1.8.1 の修正 | テスト |
|---|---|---|---|
| a | 清算後も予定 perp exit 価格を使っていた | `perp_out = 清算約定` | T36 |
| b | 清算後も通常の `cost_perp_out` を計上 | **0 にする**(spot 側は通常どおり) | T36 |
| c | `liquidation_loss` が価格 PnL を二重計上しうる | 価格損失は `q(P_in − P_liq)` に**一度だけ**。`liquidation_fee_usdt` は **clearance fee のみ** | T37 |
| d | 清算/巻き戻しの時刻・価格が記録されない | `liquidation_ts` / `liquidation_fill` / `spot_unwind_ts` / `spot_unwind_fill` / `naked_spot_bars` を追加 | T38 |

**恒等式の一般化**: `D_out := P_exit_actual − S_exit_actual`。
脚が別時刻で終了しても `PnL = q(D_in − D_out) + Funding` は保たれる(**T35** が清算経路で検算)。

**spot 巻き戻し**: 清算バーより**後**で最初に因果的に執行可能な spot open。
欠損バーは §9 M6b の roll-forward で飛ばし、飛ばした本数を `naked_spot_bars` に記録する。
perp が消えている間、`tracking_error` は 1.0 になる(**T39**)。

## 4. H14 — 清算コストは**未解決**(実験をブロックする)

`liquidation_slippage_bps = 0.0` は v1.8 が凍結した値ではない。**黙って維持していない。**

**確認できたこと**(公式 FAQ、2026-08-17 取得):
Liquidation Clearance Fee は**存在し**、「適用される rate × 建玉の名目価値」で算定される。
清算トリガは `Collateral < Maintenance Margin`(**本実装の判定と一致**)。
約定は Smart Liquidation の **IOC 成行**で、未約定分は Insurance Fund が引き取る
→ **short にとってトリガー価格より不利になりうる**。

**取得できなかったこと**: **rate の数値**。
risk bracket payload に `fee|liq|clear|penalt` に一致するキーは**0件**、
trading-rules の表は JS 描画で非 JS 取得では "No Data"、`leverageBracket` は 401。

```text
LIQUIDATION_CLEARANCE_FEE_RATE = None
LIQUIDATION_FEE_STATUS         = "pending_authoritative_read"
```

**H13 と同じ扱い**: `TwoLegConfig` は両値を**必須引数**として要求し、
清算経路の評価時に未解決なら `ValueError` で落とす(**T41**)。
清算しない経路は不必要に止めない(T41 の対照)。

## 5. 本モジュールがやらないこと

1. **シグナルを生成しない。** ρ も閾値も評価しない(runner の責務)。
2. **データを取得しない。** バーと funding イベントを受け取るだけ。
3. **実験を実行しない。** 経験的な結果を1つも生成していない。
4. **Final OOS・封印された事前予想レジスタを読まない。**
5. **凍結パラメータを上書きしない。** 既定は `phase8_prereg` から取る。

# Phase 8.1 — H14 の分割と H14b 修正条項の**草案**

- 日付: 2026-08-18 (UTC)
- 対象凍結版: **v1.8.4**(現行)
- 状態: **草案。凍結していない。実装もしていない。**
- 本文書は engine を変更しない。`two_leg.py` は凍結対象であり、**読んだだけ**である。

---

## 1. H14 の分割

v1.8.1 §24.6 の H14 は、性質の違う2つを1つの blocker に押し込んでいた。分割する。

| # | 内容 | 性質 | 解決手段 |
|---|---|---|---|
| **H14a** | 清算 clearance fee 率 | **取引所の固定パラメータ** | 権威ある一次情報の読み取り |
| **H14b** | 強制清算の**執行/滑り**モデル | **市場状態に依存する。固定パラメータではない** | 観測可能な履歴価格に基づく決定論的規則 |

**H14b について、単一の権威ある `liquidation_slippage_bps` の探索は打ち切る。**
滑りは板の深さ・清算の連鎖・そのバーのボラティリティで決まるものであって、
取引所が公表する定数ではない。定数を探し続けることは、存在しないものを
探すことになる。

現行の engine は両者を1つの blocker として扱っている:

```python
UNFROZEN_PARAMETERS = ("liquidation_clearance_fee_rate", "liquidation_slippage_bps")
```

分割は**凍結モジュールの記述変更を伴う**ため、H13 解決時の修正条項
(v1.8.5 を想定)へ束ねる。**本文書では凍結モジュールを編集していない。**

---

## 2. 現行の清算会計(**読み取り結果。変更していない**)

`two_leg.py` の清算経路:

```python
trigger = _liquidation_price(entry_price, margin_usdt, q, mmr)
fill    = trigger * (1.0 + slip_bps * 1e-4)          # <- H14b が置き換える対象
result.liquidation_fill   = fill
result.liquidation_fee_usdt = fee_rate * q * fill    # <- H14a
result.perp_out = fill                               # 予定 exit 価格は使わない(§24.5 a)
fut_w.margin_usdt = max(0.0, fut_w.margin_usdt + fut_w.unrealized_pnl(fill))
```

PnL は次で確定する:

```python
pnl_gross = q * (basis_in - basis_out) + funding_total      # basis_out = perp_out - spot_out
pnl_net   = pnl_gross - cost_total - liquidation_fee_usdt   # cost_total は清算時 cost_perp_out を含まない
```

### 2.1 会計上の帰結(**H14b を決める前に確認すべき点**)

**(a) fill は PnL へ直接入る。** `perp_out = fill` であり `pnl_gross` は `perp_out` を
そのまま使う。滑りを大きくすれば、その分だけ損失が増える。二重計上は無い
(clearance fee は別項)。

**(b) 損失は証拠金残高で打ち切られない。**
`fut_w.margin_usdt = max(0.0, ...)` の下限は **BarState の表示にしか効かない**。
`pnl_gross` は wallet を経由しないため、**建てた証拠金を超える損失をそのまま計上する**。

実際の Binance では、trader の損失は破産価格で頭打ちになり、それ以上は
保険基金 / ADL が負担する。したがって**上限を置かない滑り規則は、
経済的にあり得ない額の損失を計上しうる**。

**(c) 破産価格はトリガーのすぐ上にある。** short perp について:

```text
トリガー   m* = (margin + q·entry) / (q(1 + mmr))
破産価格   m_b = (margin + q·entry) / q  =  m* · (1 + mmr)
```

すなわち **m_b はトリガーの (1 + mmr) 倍**。tier 1 の `mmr = 0.004` では
**トリガーの +40 bps** にすぎない。

primary 設定では entry notional = `q·S = 6000 USDT`(`q = (C−R)L/((L+1)S)`)であり、
tier 1(0〜300,000 USDT)の範囲に収まる。したがって `mmr = 0.004` が適用され、
**トリガーと破産価格の距離は 40 bps** である。

**これが H14b の設計を支配する。** 5分バーの安値高値の幅は、清算が起きるような
局面では 40 bps を超えるのが普通である。つまり「バーの不利側極値」を素直に採ると、
ほぼ常に破産価格を超える。

---

## 3. 利用可能な履歴価格フィールド(**実測。manifest から**)

`data/` は版管理外で本セッションには実体が無いため、**manifest の列情報**で確認した。
Binance Vision の取得元は `data/futures/um`、すなわち **USD-M 無期限(perp)**である。

| dataset | 期間 | 列 |
|---|---|---|
| `klines_5m` | 2020-01-01 〜 2025-12-31(631,296 行、欠損 0) | ts, **open, high, low, close**, volume, volume_quote, trades, taker_buy_volume, taker_buy_quote |
| `premium_index_5m` | 2020-01-01 〜 2025-12-31(629,246 行) | ts, premium_open, **premium_high, premium_low**, premium_close, premium_samples |
| `metrics_5m` | 2020-09-01 〜 2025-12-31(560,388 行) | open_interest, long/short ratio 各種 |

### 3.1 判明した欠落(**H14b より前に効く。別件として報告する**)

| # | 欠落 | 影響 |
|---|---|---|
| **F1** | **`markPriceKlines` を取得していない。** `binance_vision.DATASETS` に無い | 凍結 engine の `Bar.mark_high` / `Bar.mark_close` は「markPriceKlines 由来」と明記されているが、**その供給源が構成されていない**。清算トリガー判定は mark で行う規約(§11.3 / 監査 Y38)なので、**現状では清算経路を実データで動かせない** |
| **F2** | **spot klines を取得していない。** 取得元は `futures/um` のみ | 二脚 engine の `spot_open` / `spot_close` に供給源が無い。**spot 脚全体が未取得** |
| **F3** | `Bar` に **`perp_high` が無い**(`spot_open, spot_close, perp_open, perp_close, mark_high, mark_close`) | 「不利側の**約定**価格極値」を engine へ渡す経路が存在しない。追加は**凍結済み `two_leg.py` の変更**であり再凍結を要する |

F1 と F2 は H14b とは独立の**データ取得の穴**である。**本文書では埋めない。**

---

## 4. H14b の草案 — 決定論的で保守的な fill 規則

### 4.1 方向

perp 脚は **short**(A2 `long_spot_only` の carry)。したがって不利側は**上**であり、
不利側の約定価格極値は当該バーの **`perp_high`** である。

### 4.2 候補規則

`m*` = トリガー、`m_b = m*(1 + mmr)` = 破産価格、`H` = 清算バーの `perp_high`。

| 案 | 規則 | 性質 |
|---|---|---|
| **R1** | `fill = max(m*, H)` | 上限なし。**(b) により、建てた証拠金を超える損失を計上しうる**。取引所の実態と食い違う |
| **R2** | `fill = min(max(m*, H), m_b)` | 観測価格を使い、**破産価格で頭打ち**。取引所の経済的実態と整合 |
| **R3** | `fill = m_b` 固定 | 最も保守的。**新しい価格フィールドを要さない**(F3 を回避)が、観測価格を使わない |

**推奨は R2**(ただし**決定は人間が行う**)。理由:

- 観測可能な履歴価格に基づく、という要求を満たす
- トリガーより良い約定を仮定しない(下限 `m*`)
- 経済的にあり得ない損失を計上しない(上限 `m_b`)
- 決定論的。乱数も推定も入らない

### 4.3 **R2 を採ると何が起きるか(重要)**

`m_b − m* = 40 bps` である。清算が発生するようなバーで 5 分足の高値がトリガーから
40 bps 以内に収まることは**稀**であろう。したがって R2 は

> **実際にはほぼ常に `fill = m_b` に張り付く**

と予想される。すなわち R2 と R3 は、実データ上ほとんど区別がつかない可能性が高い。

これは「観測価格に基づく規則」という要求を満たしつつ、**実質的には破産価格の
定数規則になる**ことを意味する。設計としては正当だが、
**「観測価格を使っている」という説明が実態より強く響く**risk がある。

**この点は凍結前に人間が了解しておくべきである。** 私はここで決めない。

なお「ほぼ常に張り付く」は**予想であって実測ではない**。実測するには清算バーを
特定する必要があり、それは experiment に踏み込む。**本文書では実測しない。**

### 4.4 変数と定数の案(**未凍結**)

```text
LIQUIDATION_FILL_RULE     = "adverse_traded_extreme_capped_at_bankruptcy"   # R2 の場合
LIQUIDATION_FILL_FLOOR    = "liquidation_trigger_price"
LIQUIDATION_FILL_CAP      = "bankruptcy_price = trigger * (1 + maint_margin_rate)"
LIQUIDATION_ADVERSE_FIELD = "perp_high"          # short perp なので高値
LIQUIDATION_SLIPPAGE_BPS  = 廃止(市場状態依存であり取引所の固定値ではない)
```

`liquidation_slippage_bps` は `UNFROZEN_PARAMETERS` から**削除**し、
H14b の規則名で置き換える。`liquidation_clearance_fee_rate`(H14a)は残す。

### 4.5 H14b が要求する前提作業

1. **F3**: `Bar` に `perp_high` を追加する(凍結 `two_leg.py` の変更 → 再凍結)
2. **F1**: `markPriceKlines` を取得する(トリガー判定に必要)
3. **F2**: spot klines を取得する(spot 脚に必要)
4. 恒等式テストの更新: 清算経路の `pnl_gross == pnl_gross_by_legs`(T35 系)が
   新しい `fill` でも成り立つこと。fill は `perp_out` にのみ入るので保たれるはずだが、
   **実装時に確認する**
5. 上限 `m_b` に張り付いたケースを **artifact に記録する**
   (`fill_rule_binding = "floor" / "observed" / "cap"`)。
   どれだけの頻度で観測価格が実際に効いたかを、後から検証できるようにするため

---

## 5. 本文書で**していないこと**

- engine を変更していない(`two_leg.py` は凍結対象。読んだだけ)
- H14b を実装していない・凍結していない
- H14a の値を決めていない
- 清算バーを特定していない。実データで滑りを測っていない
- rho / シグナル / return / PnL / Layer 1-3 評価 / Final OOS / 封印済み prior register
  のいずれにも触れていない

**H13 / H14a / H14b はいずれも実験ブロッカーのままである。**

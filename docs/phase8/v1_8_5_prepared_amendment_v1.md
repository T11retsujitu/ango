# Phase 8.1 — v1.8.5 修正条項の**適用前草案**(P3 / P4 / H14a / H14b)

- 日付: 2026-08-18 (UTC)
- 現行の凍結: **v1.8.4**
- 状態: **未適用・未凍結。** 凍結モジュール(`two_leg.py` / `phase8_prereg.py`)を
  **1バイトも編集していない**。v1.8.4 の凍結ハッシュは全て一致したままである。
- 前提: [P1/P2 プローブ所見](p1_p2_mark_availability_probe_v1.md) を報告済み。

**なぜ適用していないか**: 指示の冒頭が「v1.8.5 をまだ凍結するな」であり、
凍結モジュールを編集した瞬間に v1.8.4 の凍結不変量が壊れる
(`test_frozen_spec_was_not_edited_after_the_freeze` が失敗する)。
編集だけして封をしない状態は、**リポジトリが自分の凍結規律に違反した状態**になる。
そこで**適用と封印を1手にまとめられるよう**、確定した差分をここに全文置く。
承認があれば、この文書のとおりに適用してハッシュを再計算し、
v1.8.4 を保存したまま v1.8.5 の凍結記録を作る。

---

## 1. P3 — spot のギャップは既存の M6a / M6b のまま

**変更しない。**

- spot のギャップ(462 本 / 15 区間)は、既存の**片脚欠落**規則
  (§9 M6a / M6b)がそのまま扱う。
- **`close_time` 異常のバーに対する特別な filter を追加しない。**
  異常 8 行はすでに `close_time_not_bar_end_rows` / `close_time_before_open_rows`
  として計数されており、行は落としていない。ここに選別を足すと、
  **停止直前のバーだけを標本から外す**ことになり、結果依存の選択に近づく。

コード変更なし。凍結記録に決定として記す:

```json
"P3": {
  "decision": "preserve_m6a_m6b_semantics_for_spot_gaps",
  "special_filter_for_anomalous_close_time": false
}
```

---

## 2. P4 / H14b — `perp_high` の追加と執行モデルの凍結

### 2.1 `phase8_prereg.py` に追加する定数

```python
# --- v1.8.5 H14b: 強制清算の執行モデル(§31)---------------------------------
# 滑りは市場状態に依存する。**固定の bps を置かない。**
LIQUIDATION_EXECUTION_MODEL: Final = "adverse_trade_extreme_capped_at_bankruptcy"
# short perp:
#     candidate        = max(trigger_price, perp_high)
#     liquidation_fill = min(candidate, bankruptcy_price)
LIQUIDATION_FILL_FLOOR: Final = "liquidation_trigger_price"
LIQUIDATION_FILL_CAP: Final = "bankruptcy_price"        # = trigger * (1 + maint_margin_rate)
LIQUIDATION_ADVERSE_FIELD: Final = "perp_high"          # short なので高値
LIQUIDATION_TRIGGER_FIELD: Final = "mark_high"          # **判定にのみ使う**
LIQUIDATION_FIXED_SLIPPAGE_ALLOWED: Final = False
FILL_RULE_BINDINGS: Final = ("floor", "observed", "cap")

# --- v1.8.5 H14a: 清算手数料が未取得のままだった場合の事前登録 fallback ------
LIQUIDATION_FEE_FALLBACK: Final = "non_binding_if_no_liquidation_else_abort"
LIQUIDATION_FEE_ZERO_SUBSTITUTION_ALLOWED: Final = False
LIQUIDATION_MODEL_BLOCKED_DISPOSITION: Final = "liquidation_model_blocked"

# --- v1.8.5: 不利側 mark 経路が再構成できない区間の扱い ----------------------
LIQUIDATION_STATE_UNKNOWN_DISPOSITION: Final = "liquidation_state_unknown"
```

### 2.2 `two_leg.py` — `Bar` に `perp_high` を追加

現行:

```python
    ts: datetime
    spot_open: float | None
    spot_close: float | None
    perp_open: float | None
    perp_close: float | None
    mark_high: float | None = None
    mark_close: float | None = None
```

適用後:

```python
    ts: datetime
    spot_open: float | None
    spot_close: float | None
    perp_open: float | None
    perp_close: float | None
    perp_high: float | None = None   # **約定価格の高値。執行価格の代理にのみ使う**
    mark_high: float | None = None   # **mark の高値。清算判定にのみ使う**
    mark_close: float | None = None
```

docstring に追記する:

> `mark_high` は markPriceKlines 由来で、**清算判定にのみ**使う。
> `perp_high` は klines(約定)由来で、**執行価格の代理にのみ**使う。
> **この2つを入れ替えてはならない。** 入れ替えると、板に無い価格で約定した
> ことにするか、清算されない局面で清算したことにするかのどちらかになる。

### 2.3 `two_leg.py` — `UNFROZEN_PARAMETERS` の入れ替え

```python
UNFROZEN_PARAMETERS: tuple[str, ...] = (
    "liquidation_clearance_fee_rate",  # H14a: 権威ある率が未取得
)
```

`liquidation_slippage_bps` を**削除**する。`TwoLegConfig` からも同名フィールドを
削除し、`require_liquidation_cost()` は clearance fee だけを返す。

### 2.4 `two_leg.py` — 清算 fill の算出

現行:

```python
            fee_rate, slip_bps = cfg.require_liquidation_cost()
            trigger = _liquidation_price(...)
            # 成行 IOC。short にとってトリガー価格より良い約定はしない(§24.6)
            fill = trigger * (1.0 + slip_bps * 1e-4)
```

適用後:

```python
            fee_rate = cfg.require_liquidation_cost()
            trigger = _liquidation_price(
                fut_w.entry_price, fut_w.margin_usdt, q, cfg.maint_margin_rate
            )
            bankruptcy = trigger * (1.0 + cfg.maint_margin_rate)
            # **執行は約定価格の不利側極値。** mark ではない(§31)。
            observed = bar.perp_high
            if observed is None:
                # 執行価格の代理が無い bar で清算を評価してはならない
                result.reject_reason = "no_perp_high_on_liquidation_bar"
                result.disposition = P.LIQUIDATION_STATE_UNKNOWN_DISPOSITION
                break
            candidate = max(trigger, float(observed))
            fill = min(candidate, bankruptcy)
            result.fill_rule_binding = (
                "cap" if fill == bankruptcy and candidate > bankruptcy
                else "floor" if candidate == trigger
                else "observed"
            )
```

`TradeResult` に追加:

```python
    fill_rule_binding: str | None = None   # "floor" / "observed" / "cap"
    disposition: str | None = None         # liquidation_state_unknown 等
```

**`liquidation_fee_usdt = fee_rate * q * fill` は変えない。**
価格損失は `q(P_in − P_liq)` に一度だけ入るという §24.5 の会計も変えない。

### 2.5 上限の根拠(凍結記録に残す文言)

破産価格による上限は、**利用者の建玉に帰属する損失が破産境界を越えて伸びるのを
防ぐ**ためのものである。破産境界を越えた市場損失は取引所の保険基金 / ADL 機構が
負担するものであって、**清算された口座へ再び計上されるべきものではない**。

`bankruptcy − trigger = mmr = 40 bps`(tier 1)であるため、
`fill_rule_binding` が `cap` に偏る可能性が高い。**だからこそ記録する。**

---

## 3. H14a — 事前登録 fallback

```text
清算 clearance fee 率が実験時点で未取得の場合:

    liquidation_count == 0  → H14a は拘束しない(non-binding)
    liquidation_count >  0  → **経済的な performance 指標を出す前に中断**し、
                               liquidation_model_blocked と分類する
```

**ゼロ手数料での代替は決して行わない。**

`liquidation_count` は会計上の件数であり、**経済的な帰結の指標ではない**。
この分岐に return も PnL も要らない。

---

## 4. 不利側 mark 経路が再構成できない区間(**P1/P2 未確定の受け皿**)

```text
P1/P2 の source プローブを経てもなお不利側 mark 経路を再構成できない区間があり、
そこに建玉期間が重なる trade が1件でもあるとき:

    - その trade を**落とさない**
    - 清算が起きなかったと**仮定しない**
    - 経済的な指標を出す前に **layer を中断**し、
      disposition = liquidation_state_unknown と分類する
```

現況では P1 の 8 区間 2,318 本と P2 の 4 区間 43 本が**未確定**である
(egress 遮断のため判定できていない)。したがって、この条項は
**現時点では実際に発火しうる**。許可された地域からプローブを走らせて
`candidate_deterministic_repair` が付けば、その区間は解消される。

`liquidation_state_unknown` は GO でも NO-GO でもない。
§29 の `source_sensitive`、H14a の `liquidation_model_blocked` と同様、
**「この設計では判定できない」**という帰結である。

---

## 5. H13 — Arm-R シグナル生成に対する**hard blocker**

測定された taker rate は、**コスト依存のエントリ境界**に入る:

```text
ρ_u(C) = κ · log(1 + C)         C は往復コスト(taker を含む)
Arm R entry:  ρ > ρ_u(C)
```

すなわち H13 が未解決のままでは、**エントリ境界そのものが未定**であり、
経験的な Arm-R シグナルを生成できない。`rho.require_resolved_cost()` は
既にプレースホルダで例外を出す。v1.8.5 でも**この blocker は解除しない**。

さらに本セッションの環境からは **REST 経路自体が地域制限で塞がれている**ため、
資格情報を供給しても解決できない。許可された地域の環境で実行する必要がある。

---

## 6. 適用時の手順(**承認後に1手で行う**)

1. §2 の差分を `phase8_prereg.py` / `two_leg.py` へ適用する
2. `Bar` の新フィールドに合わせて §21.2 のテストを更新・追加する
   - `perp_high` と `mark_high` を入れ替えたら落ちるテスト
   - `fill_rule_binding` が floor / observed / cap を正しく返すテスト
   - 上限が破産価格を越えないテスト
   - `liquidation_slippage_bps` を渡そうとすると失敗するテスト
   - `perp_high` が無い bar で清算評価すると `liquidation_state_unknown` になるテスト
   - 清算経路の `pnl_gross == pnl_gross_by_legs` が新しい fill でも成り立つテスト
3. ハッシュを再計算し、**v1.8 / v1.8.1 / v1.8.2 / v1.8.3 / v1.8.4 の5記録を
   1バイトも変えずに**保存したまま `carry_freeze_v1_8_5.json` を作る
4. `unresolved_at_freeze` は **H13 / H14a**(H14b は解決済み)
5. `post_freeze_policy.experiments_permitted` は **false のまま**

**F3 の適用は上の 1 と同じ手で行う。** 本文書だけでは `Bar` に `perp_high` は入らない。

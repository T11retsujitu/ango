# Phase 8.1 — v1.8.6 修正条項の**適用前草案**(funding 決済 mark の source 選択)

- 日付: 2026-08-21 (UTC)
- 現行の凍結: **v1.8.5**
- 状態: **未適用・未凍結。** 凍結モジュール(`phase8_prereg.py` / `two_leg.py` /
  `mark_path.py` / `rho.py` / `splits.py` / `aave_rates.py`)を**1バイトも編集していない**。
  v1.8.5 の凍結ハッシュは**全 26 項目が一致したまま**である。
  既存の凍結記録(v1.8 〜 v1.8.5)も**1バイトも変更していない**。
- 適用範囲: **funding 決済時 markPrice の source 選択と fidelity の記録のみ。**
- 根拠となる実測:
  [再構成 probe](funding_mark_reconstruction_probe_v1.md) /
  [materiality 分析](funding_mark_materiality_v1.md) /
  [REST markPrice 実測](funding_rest_markprice_probe_v1.md) /
  [Vision fundingRate schema](funding_rate_dump_schema_v1.md)

**なぜ適用していないか**: 凍結モジュールを編集した瞬間に v1.8.5 の凍結不変量が壊れる。
編集だけして封をしない状態は**リポジトリが自分の凍結規律に違反した状態**になる。
そこで**適用と封印を1手にまとめられるよう**、確定した差分をここに全文置く。

---

## 0. 変更しないもの(**明示**)

| 項目 | 状態 |
|---|---|
| hypothesis / Primary Research Question | **不変** |
| arm 定義(Arm R / B0..B4 / E) | **不変** |
| layer 境界(layer 1 / 2 / X / 3) | **不変** |
| promotion 閾値の数値(`MIN_TRADES` / `RHO_MIN` / コスト階層 …) | **不変** |
| GO / NO-GO の**判定手続き** | **拡張する**(§4.1 / §8.1 が「判定できない」帰結を2つ足す。**閾値は動かさない**) |
| `FINAL_OOS_START` = 2026-01-01 | **不変**(1文字も触らない) |
| 封印・per-target cutoff | **不変** |
| コスト階層・`base_taker` / `stress` | **不変** |
| 証拠金規則・event order(§24.4)・清算会計(§24.5) | **不変** |
| **H13**(taker commission)| **不変。未解決のまま実験をブロックする** |
| **H14a**(清算手数料)/ H14b(執行モデル) | **不変** |
| Aave の `source_sensitive` 規則(§29) | **不変**(本条項は**別軸**の source-sensitivity を足す) |
| mark 経路の状態(§32 の5状態) | **不変**(本条項はそれを**使う側**である) |
| gate 順序(§33) | **`evaluate_gates()` の中身は不変。その手前に段を1つ足す**(§4.1)。§25 の優先順位規則により、順序の記述だけを supersede する |

**本条項は仮説を変更しない。実験を解禁しない。**

---

## 1. exact reconstruction を棄却する証拠

事前に固定した primary candidate(`floor_5m(fundingTime)` のバーの `mark_open`)を
REST の公式 markPrice と **原文の Decimal** で突き合わせた結果:

| 項目 | 実測 |
|---|---:|
| 比較できた決済 | **2,378** |
| **Decimal 完全一致** | **1,919(80.70%)** |
| **不一致** | **459(19.30%)** |
| 対応不能 / 曖昧 | 0 / 0 |

**判定は `proxy_only_not_exact_reconstruction` である。**

棄却が「規則の選び方が悪かっただけ」ではないことの根拠:

1. **どの 5m OHLC 規約も primary を上回らない**(感度分析。
   `phase8_funding_mark_materiality_v1.json` の `candidate_sensitivity` に**再現可能な形で**入っている):
   `open` **1,919** / 直前バー `close` 887 / `low` 164 / `high` 79 /
   同バー `close` 0 / 直前バー `open` 0 / 次バー `open` 0。
2. **`open` と直前バー `close` の OR をとっても 2,109 / 2,378(88.7%)止まり**で、
   **269 件はどの規約でも説明できない**。
   *(この 2,109 は **primary の成績ではない**。OR 規則は採用しない。§4。
   artifact 側も `union_is_not_the_primary_score: true` で明示している)*
3. **ms のジッタが原因ではない。** 不一致率は offset 0 の決済のほうが**高い**
   (370/1,711 = 21.6% 対 89/667 = 13.3%)。バーを取り違えているなら逆向きになる。
4. **459 件の差は「mark の1秒あたり変動」の分布に完全に収まる**(超過 0 件)。
   `markPriceKlines` は **1Hz 離散サンプルの要約**(300 samples / 5min)であり、
   バー N の `close` は第299秒、バー N+1 の `open` は第300秒である。
   実際 `open == 直前バーの close` は全バーの **29.9%** しか成り立たない。
5. **範囲外の 31 件は 31/31 が直前バーの `[low, high]` の内側**にある
   (= 「バーが始まる前の mark」)。
6. 一致/不一致を分けるのは**バー内変動幅ではなく値の刻み**である。
   `candidate_sensitivity.by_tick_class` の実測: 0.1 刻みに貼り付いた `mark_open` は
   **1,177 / 1,214(96.9%)**一致するのに対し、フル精度の値は
   **742 / 1,164(63.7%)**しか一致しない。
   一方でバーの値幅の中央値は一致群 123.49 / 不一致群 126.89 と**ほぼ同じ**である。

> **結論**: funding の markPrice は**連続計算された mark を決済時刻ちょうどで
> スナップショットした値**であり、5分バーの `open` は **1Hz サンプルの最初の1点**である。
> **粒度が違うので、規則を精緻化しても一致しない。**

---

## 2. proxy を採用する理由

**exact ではないと認めたうえで**、誤差の大きさが funding 収支の水準に対して
小さいことを実測した([materiality 分析](funding_mark_materiality_v1.md)):

| 量 | p50 | p95 | p99 | max | 2,378 決済の累積 \|誤差\| |
|---|---:|---:|---:|---:|---:|
| \|bps 誤差\| | 0 | 0.16811 | 0.66757 | 2.39797 | 70.96727 |
| \|cashflow 差\| (USDT/BTC/決済) | 0 | 6.961e-5 | 5.178e-4 | 3.310e-3 | **0.04687** |
| \|名目リターン差\| | 0 | 9.968e-10 | 7.332e-9 | 5.679e-8 | **7.039e-7** |

**2,378 決済ぶんの「1 BTC あたり単位誤差」の絶対値を足し合わせると 0.047 USDT、
名目換算で 7.04e-7 である**(対象期間 2023-10-31 〜 2025-12-31、792 日)。
**建玉を仮定した量ではない。** 各決済の単位誤差を算術的に足しただけで、
数量も保有期間もコストも入っていない。

- **これは「exact である」ことを意味しない。** 一致率は 80.70% である。
- **これは経済的判定でもない。** strategy PnL を計算していない。
- 言えるのは「**入力誤差の桁**」だけであり、それを踏まえて
  **proxy を明示的に proxy として採用する**という設計判断である。

---

## 3. 期間短縮案を却下する理由

「REST markPrice がある 2023-10-31 以降だけを使う」案は**採らない**。

| 理由 | 内容 |
|---|---|
| **layer 1 が壊れる** | layer 1 は `2020-01-01 <= ts < 2025-06-01` と**凍結済み**である。開始を 2023-10-31 へ動かすのは **layer 境界の変更**であり、§0 の「変更しない」に反する |
| **replication の対象が消える** | A2 のサンプルは 2020-01-08 〜 2024-03-11。2023-10-31 以降に切ると、**再現対象期間のほとんどを捨てる**ことになる |
| **標本下限を割る** | §16.3 の `MIN_TRADES` は暦の算術で事前評価する規約であり、期間を削ると layer 1 が構造的に `insufficient_sample` へ落ちる |
| **結果を見た後の期間選択に近い** | 期間を「データが良い方」へ寄せる操作は、**恒常ルールが禁じている標本選択**と同型である |
| **誤差の桁が小さい** | §2 のとおり、短縮によって得られる正確さは名目 7.04e-7 のオーダーであり、失う期間に見合わない |

**代わりに、fidelity をイベント単位で記録して区別する**(§5)。
**期間は削らず、質の違いを可視化する。**

---

## 4. event-level source selection rule(**人間が決定した方針**)

```text
決済 s ごとに、次の順序で **1つだけ** 選ぶ:

  1. REST markPrice が **有効**(非空・parse 可能・正)
        → source = "binance_funding_rest"            fidelity = "exact_rest"
  2. REST markPrice が **有効でない**(空 / parse 不能 / 非正)場合に限り、
     floor_5m(s) のバーの mark_open
     ただし mark 経路の状態が observed / verified_repair のときのみ
        → source = "binance_mark_price_kline_open"   fidelity = "official_kline_proxy"
  3. それ以外
        → source = None                 fidelity = "unavailable"
```

**step 2 の発動条件は「空」ではなく「有効でない」である。**
「非空だが parse 不能」「非空だが 0 以下」を step 3 へ落とすと、
**proxy が使えるのに layer を止める**ことになる。実測では
`unparseable` も `non_positive` も 0 件だが、**規則は 0 件に依存させない**。

REST が有効でなかった理由は**分類して記録する**
(`rest_absent` / `rest_unparseable` / `rest_non_positive`)。
**「空だった」と「壊れていた」を同じ箱に入れない。**

**規則の性質**:

- **順序は固定で、イベントごとに切り替えない。** REST があるのに proxy を使う経路も、
  proxy が「近い」から REST より優先する経路も**存在しない**。
- **`mark_open` を exact と呼ばない。** fidelity は必ず `official_kline_proxy` である。
- **欠測・stale・route 未確認を補完しない。** §32 の状態が
  `observed` / `verified_repair` でなければ **`unavailable`** であって、
  前値の横引きも補間もしない。
- **各イベントで `open` と直前 `close` の近い方を選ばない。**
- **OR 規則を使わない**(§1 の 2,109 件は primary の成績ではない)。

### 4.1 `unavailable` のときの停止規則

```text
建玉中の決済のうち 1 件でも fidelity == "unavailable" があれば:
    その trade を落とさない
    funding を 0 と仮定しない
    **経済指標より前に layer を中断し** disposition = funding_mark_unavailable
```

- **`liquidation_state_unknown` と同じ扱いの第3の帰結**であり、GO でも NO-GO でもない。
- **`funding == 0` と数えてはならない。** 数えれば「観測できなかった」が
  「funding が無かった」に化ける(§33 が `liquidation_count == 0` について
  禁じているのと同じ誤りである)。

**gate の順序**(§33 を supersede するのは**順序の記述だけ**):

```text
mark 経路の観測可能性 → **funding mark の可用性** → 清算検出 → 清算件数
    → H14a の手数料 gate → 経済指標
```

**実装は凍結関数を編集しない。** `mark_path.evaluate_gates()` は
§33 の順序の唯一の実装であり**凍結対象**なので、そこへ段を差し込まない。
代わりに **`carry_runner` が呼ぶ前に判定する**:

```python
# carry_runner の中(凍結モジュールは触らない)
if any(f == "unavailable" for f in fidelities_while_open):
    return FUNDING_MARK_UNAVAILABLE_DISPOSITION      # ← ここで返す
disposition = mark_path.evaluate_gates(...)          # ← 凍結関数はそのまま
```

- `evaluate_gates()` の**引数も戻り値も挙動も変えない**。
- 呼び出し側で前段に置くので、**順序は §4.1 のとおりに実現され、
  `mark_path.py` は1バイトも変わらない**(§10)。
- 「前段で返す」ことをテストで固定する(§11 T84)。

---

## 5. fidelity 列(イベント単位で保持する)

```text
FUNDING_MARK_FIDELITIES = ("exact_rest", "official_kline_proxy", "unavailable")
```

| fidelity | 意味 |
|---|---|
| `exact_rest` | 公式 REST の markPrice をそのまま使った。**再構成ではない** |
| `official_kline_proxy` | REST が有効でなかったので `mark_open` を使った。**exact ではない** |
| `unavailable` | 使える入力が無い。**値を作らない** |

**要約に畳まない。** 決済ごとに保持し、artifact に件数と構成比を出す(§7)。

### 5.1 fidelity は **primary 系列**を記述する

3値は **§4 の primary 選択**に対するラベルである。§8 の sensitivity は
同じイベントで別のフィールド(直前バーの `mark_close`)を使うので、
**同じ 3 値で説明できない**。したがって:

- fidelity 列は **primary 経路のもの**であり、sensitivity 経路の値には使わない。
- sensitivity の実行は**自分の source ラベル**
  (`kline_previous_close_sensitivity`)を別に持ち、**fidelity 3 値を汚さない**。
- **sensitivity のラベルを `official_kline_proxy` と書かない**
  (どのフィールドを使ったのかが消える)。

---

## 6. layer ごとの fidelity 区分

実測(再構成 probe §4 / materiality §3.4):

| layer | 決済 | `exact_rest` | `official_kline_proxy` の候補 | `unavailable` |
|---|---:|---:|---:|---:|
| layer 1 `literature_in_sample` | 5,934 | **1,736** | **4,174** | **24** |
| layer 2 `contaminated_confirmation` | 642 | **642** | 0 | 0 |

```text
LAYER_FUNDING_MARK_FIDELITY = {
    "layer1": "partial_proxy",          # exact_rest 1,736 / proxy 4,174 / unavailable 24
    "layer2": "exact_rest",             # 642 / 642 が公式 REST
    "layer3": "unknown_until_observed", # 2026-09-01 以降。**まだ1バーも存在しない**
}
```

**layer 3 を表から落とさない。** GO を判定するのは layer 3 である(§18.3)。
凍結時点で layer 3 は未来なので fidelity は**測れない**が、
**「測れない」と書くことと、表から消すことは違う**。
消せば、後から誰かが「layer 3 は exact だと決まっていた」と読みうる。
layer 3 のデータが存在したら**実測して分類し直す**(その時点で再凍結が要る)。

- **layer 1 は `partial_proxy`** である。29.3% だけが `exact_rest` で、
  70.3% が proxy、0.4%(24 件)が `unavailable`。
- **layer 2 は `exact_rest`** である(642 / 642)。
- **この区分を「layer 1 も exact」と丸めない。**
- **24 件の `unavailable` は P1(Vision の日単位 dump 欠落)の内側**であり、
  状態は `route_unverified`(**`source_unobservable` ではない**)。
  許可された egress からプローブを再実行して `verified_repair` に昇格すれば、
  この 24 件は `official_kline_proxy` へ移る。**それまでは補完しない。**

---

## 7. artifact へ追加する必須フィールド(§20 の拡張)

```text
funding_mark:
  fidelity_counts:        { exact_rest, official_kline_proxy, unavailable }
  fidelity_shares:        { 同上を構成比で }          ← **必ず件数と両方出す**
  fidelity_describes:     "primary_selection_only"    ← §5.1
  rest_invalid_reasons:   { rest_absent, rest_unparseable, rest_non_positive }
  by_layer:               { layer1: {...}, layer2: {...}, layer3: {...} }
  layer_fidelity_class:   { layer1: "partial_proxy", layer2: "exact_rest",
                            layer3: "unknown_until_observed" }   ← **落とさない**
  proxy_is_exact:         false                       ← **常に false**
  exact_match_rate_measured: "1919/2378"              ← **丸めない**
  selection_rule:         "rest_official_then_kline_open_proxy"
  or_rule_used:           false
  nearest_candidate_selection: false
  unavailable_events:     [ 全件の provenance ]       ← 要約で丸めない
  sensitivity:
    previous_bar_close:   { 主統計量・promotion 判定を再計算した結果 }
  disposition:            null | "funding_mark_source_sensitive"
                                | "funding_mark_unavailable"
```

---

## 8. primary と sensitivity

| 区分 | 定義 | 用途 |
|---|---|---|
| **primary** | §4 の選択規則(REST → `mark_open`) | **判定はこれだけで行う** |
| **sensitivity** | **REST が有効でない決済に限り**(空 / parse 不能 / 非正)、`mark_open` の代わりに**直前バーの `mark_close`** | **報告のみ。primary にしない** |

- sensitivity でも **REST が有効な決済は REST のまま**である(そこは動かさない)。
- **イベントごとに `open` / 直前 `close` の近い方を選ばない。**
- **OR 規則を使わない。**
- sensitivity は **§15.1 の family に入らない**(多重比較補正の対象外)。

### 8.1 disposition — `funding_mark_source_sensitive`

```text
if (primary での promotion / GO 判定) != (previous-close sensitivity での判定):
        → funding_mark_source_sensitive
```

- **GO でも NO-GO でもない第3の帰結**である。**GO へ昇格させない。**
- 根拠は §29 の Aave `source_sensitive` と同じ論法である:
  **結論が入力ソースの選択で反転するなら、それは機序についての結論ではなく
  ソース選択についての結論である。**
- §29 とは**別軸**なので、両方を独立に記録する
  (`rate_source_sensitive` と `funding_mark_source_sensitive` は同時に立ちうる)。

### 8.2 この判定が**どこで効き、どこで効かないか**(**隠さない**)

sensitivity は **REST が有効でない決済でしか primary と違わない**。実測の内訳から、
効く場所は次のように決まっている:

| layer | REST 無効の決済 | sensitivity は primary と違うか | 効果 |
|---|---:|---|---|
| layer 1 | **4,198 / 5,934** | **違う** | §18.1 の **replication gate に効く** |
| layer 2 | **0 / 642** | **違わない**(全件 `exact_rest`) | **promotion 判定には効かない** |
| layer 3 | 未知(未来) | 未知 | GO 判定に効くかどうかは**測るまで分からない** |

> **したがって、現在のデータでは `funding_mark_source_sensitive` は
> promotion(layer 2 → layer 3)の判定を変えられない。** layer 2 が
> 100% `exact_rest` だからである。**これは「安全だから無視してよい」ではなく、
> 「この gate は layer 1 の replication 側でしか働いていない」という事実である。**
>
> **layer 3 で REST の markPrice が欠けた場合には GO 判定に効く。**
> そのときのために規則を**先に**凍結しておく(事後に足すと、
> データを見てから gate を作ったことになる)。

---

## 9. `phase8_prereg.py` に追加する定数(**まだ書いていない**)

```python
# --- v1.8.6: funding 決済 mark の source 選択(§4)---------------------------
# **exact ではない。** proxy を proxy として明示的に採用する。
FUNDING_MARK_SOURCE_ORDER: Final = ("binance_funding_rest", "binance_mark_price_kline_open")
FUNDING_MARK_PROXY_FIELD: Final = "mark_open"
FUNDING_MARK_PROXY_BAR_RULE: Final = "floor_5m(funding_time)"
FUNDING_MARK_PROXY_USABLE_STATUSES: Final = MARK_PATH_ACCEPTABLE  # observed / verified_repair
FUNDING_MARK_PROXY_IS_EXACT: Final = False
FUNDING_MARK_OR_RULE_ALLOWED: Final = False
FUNDING_MARK_NEAREST_CANDIDATE_ALLOWED: Final = False
FUNDING_MARK_INTERPOLATION: Final = "none"

# 実測(v1.8.6 の根拠。**閾値の較正には使わない**)
FUNDING_MARK_PROXY_EXACT_MATCH_MEASURED: Final = (1919, 2378)

# --- fidelity(§5)-----------------------------------------------------------
FUNDING_MARK_FIDELITIES: Final = ("exact_rest", "official_kline_proxy", "unavailable")
# fidelity は **primary 経路**のラベルである(§5.1)。sensitivity には使わない。
FUNDING_MARK_FIDELITY_DESCRIBES: Final = "primary_selection_only"
LAYER_FUNDING_MARK_FIDELITY: Final = MappingProxyType({
    "layer1": "partial_proxy",
    "layer2": "exact_rest",
    "layer3": "unknown_until_observed",   # **表から落とさない**(§6)
})
# REST が有効でなかった理由。**「空」と「壊れていた」を同じ箱に入れない**(§4)
FUNDING_MARK_REST_INVALID_REASONS: Final = (
    "rest_absent", "rest_unparseable", "rest_non_positive",
)

# --- sensitivity(§8)---------------------------------------------------------
FUNDING_MARK_SENSITIVITY: Final = "previous_bar_mark_close"
FUNDING_MARK_SENSITIVITY_SOURCE: Final = "binance_mark_price_kline_previous_close"
FUNDING_MARK_SENSITIVITY_FIDELITY_LABEL: Final = "kline_previous_close_sensitivity"
# **「空」ではなく「有効でない」** が発動条件である(§4)
FUNDING_MARK_SENSITIVITY_APPLIES_WHEN: Final = "rest_mark_price_invalid"
FUNDING_MARK_SENSITIVITY_IN_FAMILY: Final = False

# --- disposition(§4.1 / §8.1)------------------------------------------------
FUNDING_MARK_UNAVAILABLE_DISPOSITION: Final = "funding_mark_unavailable"
FUNDING_MARK_SOURCE_SENSITIVE_DISPOSITION: Final = "funding_mark_source_sensitive"
FUNDING_MARK_SOURCE_SENSITIVITY_RULE: Final = (
    "if sign/decision under the primary funding-mark source differs from the "
    "previous-bar-close sensitivity then classify as funding_mark_source_sensitive, not GO"
)
FUNDING_MARK_ZERO_SUBSTITUTION_ALLOWED: Final = False
```

**`MIN_TRADES` / promotion 閾値 / コスト / margin / H13 / H14a は触らない。**

---

## 10. 必要なコード変更(**まだ編集していない**)

| ファイル | 変更 | 新規/変更 |
|---|---|---|
| `src/mce/phase8_prereg.py` | §9 の定数を追加 | 変更(**凍結対象。再凍結が要る**) |
| `src/mce/funding_mark_resolver.py` | **funding mark resolver**: 決済 → (value, source, fidelity)。§4 の順序、§8 の sensitivity、§6 の layer 区分(**規則から導出**)を実装し、`unavailable` を返す経路を持つ | **新規。実装済み**(純粋関数のみ。適合テスト `tests/test_funding_mark_resolver.py`) |
| `src/mce/features_carry.py` | `funding_mark_price` / `funding_mark_source` / `funding_mark_fidelity` 列と availability 宣言 | **新規**(未実装) |
| `src/mce/carry_quality.py` | fidelity 別件数・構成比、`unavailable` の停止判定を品質 gate に入れる | **新規**(未実装) |
| `src/mce/carry_runner.py` | **primary と previous-close sensitivity の二経路**を回し、判定が割れたら `funding_mark_source_sensitive` | **新規**(未実装) |
| `src/mce/carry_report.py` | fidelity 構成比と source-sensitive の表示。**proxy を exact と書かない** | **新規**(未実装) |
| `src/mce/backtest/mark_path.py` | **変更しない**(§32 の状態をそのまま使う側である) | 不変 |
| `src/mce/backtest/two_leg.py` | **変更しない**(funding 額の入力を受け取るだけ) | 不変 |
| `docs/phase8/carry_replication_protocol_v1.md` | §37 として本条項を追記(§25 の優先順位規則に従い §8.1 の mark 記述を supersede) | 変更 |
| `experiments/phase8/carry_freeze_v1_8_6.json` | **新規作成**。v1.8 〜 v1.8.5 は `preserved_predecessors` で固定し**1バイトも変更しない** | **新規** |

---

## 11. 必要な適合テスト(**まだ書いていない**)

| # | テスト |
|---|---|
| **T78** | REST markPrice があれば**必ず**それを使う(proxy が近くても REST を上書きしない) |
| **T79** | REST が空のときだけ `mark_open` を使う |
| **T80** | fidelity が `exact_rest` / `official_kline_proxy` / `unavailable` の3値で、**イベント単位**に残る |
| **T81** | **`proxy_is_exact` が常に `false`**。artifact のどこにも proxy を exact と書かない |
| **T82** | mark 経路が `observed` / `verified_repair` でなければ proxy を使わず `unavailable` |
| **T83** | `unavailable` を **funding 0 で代替しない**。`funding_mark_unavailable` を出す |
| **T84** | gate が **funding mark の可用性を経済指標より前**に評価する |
| **T85** | **OR 規則が存在しない**(`open` と直前 `close` の和集合を採る経路が無い) |
| **T86** | **イベントごとの最近傍選択が存在しない** |
| **T87** | sensitivity が **REST 空の決済にだけ**効き、REST がある決済は動かない |
| **T88** | sensitivity が **family に入らない**(Holm 補正の本数が変わらない) |
| **T89** | primary と sensitivity で判定が割れたら `funding_mark_source_sensitive`、**GO にしない** |
| **T90** | layer 1 が `partial_proxy`、layer 2 が `exact_rest` と分類される |
| **T91** | artifact に fidelity の**件数と構成比の両方**が出る |
| **T92** | 実測一致率が **`1919/2378` のまま**で、100% へ丸められていない |
| **T93** | `unavailable` の全件 provenance が artifact に残る(要約で丸めない) |
| **T94** | **v1.8.5 以前の凍結記録が1バイトも変わっていない** |
| **T95** | Aave の `rate_source_sensitive` と `funding_mark_source_sensitive` が**独立に**立つ |
| **T96** | REST が**非空だが parse 不能 / 非正**なら proxy へ落ちる(`unavailable` にしない)。§4 |
| **T97** | REST 無効の理由が `rest_absent` / `rest_unparseable` / `rest_non_positive` に**分類**される |
| **T98** | proxy のバー選択が **`floor_5m(funding_time)`** であること(境界時刻は新バー、その 1ms 前は前バー) |
| **T99** | `evaluate_gates()` の**引数・戻り値・挙動が v1.8.5 のまま**である(前段は呼び出し側にある) |
| **T100** | funding mark の可用性判定が **`evaluate_gates()` を呼ぶ前**に返る(呼ばれないことを固定) |
| **T101** | sensitivity 経路の source ラベルが **`kline_previous_close_sensitivity`** であり、fidelity 3値を汚さない。§5.1 |
| **T102** | `LAYER_FUNDING_MARK_FIDELITY` に **layer3 が存在**し、値が `unknown_until_observed` である。§6 |
| **T103** | layer 2 が全件 `exact_rest` のとき、sensitivity が **promotion 判定を変えない**(§8.2 の事実を固定する) |
| **T104** | `candidate_sensitivity` の **union を primary の成績として読める経路が無い**(`union_is_not_the_primary_score` が true) |

---

## 12. 本草案で**していないこと**

- 凍結記録・凍結コードを**1バイトも変更していない**(v1.8.5 の 26 ハッシュは全一致)
- `phase8_prereg.py` / `two_leg.py` / `mark_path.py` を編集していない
- v1.8.6 を**適用も凍結もしていない**
- rho / シグナル / strategy return / PnL / Layer 1/2/3 を計算していない
- **proxy を exact と表現していない**
- 一致率 1,919/2,378 を 100% へ丸めていない
- 459 件の不一致を「許容差内の一致」へ分類し直していない
- OR 規則の 2,109 件を primary の成績として使っていない
- 2026-01-01 以降を読んでいない
- **実験を解禁していない**(H13 / H14a は未解決のまま)

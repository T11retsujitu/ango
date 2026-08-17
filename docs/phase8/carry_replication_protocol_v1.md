# Phase 8.1 — BTC spot–perp funding carry:再現プロトコル **v1.2**(**draft・未凍結**)

- 作成日: 2026-08-17(v1) / 改訂: 2026-08-17(**v1.2**)
- 対象: [Phase 8.0 選定メモ](phase8_selection_memo_v1.md) が第1位に選んだ **P8-C1**
- **再現アンカー(唯一)**: **A2** *Fundamentals of Perpetual Futures* —
  He, Manela, Ross, von Wachter. arXiv `2212.06888`(v6 2024-08-21)。**`VERIFIED-FULL`**
- **経済的文脈(再現対象ではない)**: **A1** *Crypto Carry* — Schmeling, Schrimpf, Todorov.
  *Management Science* 2026-05-06, DOI `10.1287/mnsc.2024.05069`。**`VERIFIED-FULL`**
- 設計参照: **A3** arXiv `2605.05089`(collateral 制御)/ **B2** SSRN `6805838`(funding の観測可能性)
- 監査記録: [carry_protocol_audit_v1](carry_protocol_audit_v1.md)
- 状態: **draft v1.2。凍結していない。** §22 の freeze blocker が未解決。
- **本文書は forward return / 損益 / バックテストを一切計算していない。Final OOS 未開封。**

---

## 0-A. v1 → v1.2 改訂記録(**追記型。v1 の誤りを隠さない**)

v1 は (i) アンカー論文2本の**全文取得**と (ii) **5レンズの独立敵対監査**によって、
**凍結前に**重大な欠陥が見つかった。v1 の該当設計は**破棄ではなく訂正**として記録する。

### 論文の読み違いによる訂正

| id | v1 の誤り | 訂正 |
|---|---|---|
| **X1** | A1 を perpetual carry のアンカーとした | **A1 は dated(固定満期)futures の論文。** 本文に「perpetual crypto futures **instead of** the standard fixed-term futures analyzed in our paper」と明記。**A1 はアンカーから外し、経済的文脈に降格** |
| **X2** | A1 のサンプル終端を「2023年初以前」と推定 | **誤り。実際は 2019-03 〜 2024-07**(bis.org 配信 PDF の本文は "This version: October 1, 2025")。**layer 1/2 の境界が変わる** |
| **X3** | 固定 horizon の carry trade を再現対象とした | **A2 が検定しているのは random-maturity arbitrage**(スプレッドがコスト階層別の理論境界を超えたら建て、コスト無しの理論関係へ戻ったら解消)。**保有期間は内生**。A2 サンプルは 2020-01-08 〜 2024-03-11 |
| **X4** | H4(`calc_time`)を未解決の fatal blocker とした | **解決。** Vision `calc_time` ≡ 公式 REST `fundingTime` = **決済時刻**(2024-01 全行で照合) |
| **X5** | funding 間隔を 8時間固定とした | cap/floor 到達時に**恒久的に1時間へ切り替わる**規則がある。`funding_interval_hours` を**行ごとに読む** |
| **X6** | Binance REST は到達不可とした | `https://www.binance.com/fapi/v1/...` は **200**。**決済時点の `markPrice` が取れる** |

### 敵対監査による訂正(全件 §Y として採番。詳細は[監査記録](carry_protocol_audit_v1.md))

| id | 深刻度 | v1 の欠陥 | v1.2 の対応 |
|---|---|---|---|
| **Y1** | **fatal** | **entry 規則がどこにも定義されていなかった。** §10/§14 は horizon を列挙するが「いつ建てるか」を述べていない | **§6 で A2 の閾値規則を primary arm として明示**(X3 と同じ結論へ収束) |
| **Y2** | **fatal** | 固定 horizon × 連続グリッドでは、§6.2 の恒等式により **D 項が telescope して always_on + 余分な4約定に縮退**する。戦略としての内容が無い | 固定 horizon を **primary から外し**、記述的 robustness へ降格(§10) |
| **Y3** | **fatal** | Holm 補正を要求しながら **帰無仮説も検定統計量も p の作り方も定義していなかった**(Phase 7 の placebo に相当するものが無い) | **§15.0 で randomization 帰無を定義**(K=1,000 の事前登録 seed) |
| **Y4** | **fatal** | §18.3 の「layer2 の 0.5 倍」規則を Phase 7(n_eff 2,185–8,751)から輸入したが、layer 3 は数件〜数十件しかない | **§16.4 に事前登録の標本下限**、§18.3 を**絶対基準へ変更** |
| **Y5** | high | ε を as-of join の **tolerance 側**に置いていた。tolerance を広げても未来参照は防げない | **§5.2 を「key の publication-delay シフト」へ書き換え** |
| **Y6** | high | funding 境界 `entry <= s < exit` は、決済時刻と約定時刻が一致する位相で**建てた瞬間に直前8時間分を受け取る** | **`entry < s <= exit` へ変更**(§8.1) |
| **Y7** | high | M6(片脚欠測で trade 無効化)は **exit 側に適用すると look-ahead 選択**になる | **M6a(entry 側 skip)/ M6b(exit 側 roll-forward)へ分割**(§9) |
| **Y8** | high | 位相 4系列 × 6 horizon = **24 統計量なのに family を 6 と宣言**していた | 位相は**固定 epoch 基準**にし、**平均で1統計量へ畳む**(§16.1) |
| **Y9** | high | B3(exposure 一致 random)が **単一 seed の1本の実現値**だった | **randomization 分布へ変更**(§12) |
| **Y10** | high | 常時建玉に近い arm では **B3 が原理的に無情報**(自由度が無い) | **exposure < 0.7 の arm でのみ判定条件に使う**(§12) |
| **Y11** | high | NARDC の**集計方法が未定義**、数量 `q` も未定義 | **定額名目 `N0` を凍結し、trade 単位の `r_i` を主統計量に**(§17) |
| **Y12** | high | layer 2 を「効果量の発見に使わない」としながら、**promotion gate・MDE・比較基準の3箇所で layer 2 の効果量を使っていた** | **layer 2 の用途を正直に書き直し、汚染較正であることを明記**(§13) |
| **Y13** | high | 封印は `normalize_binance` が**書き出し前に物理的に落とす**実装。layer 3 のデータは**そもそも存在しなくなる** | **`PHASE8_PROSPECTIVE_START` を別定数化し、per-target cutoff に**(§13.3) |
| **Y14** | medium | markPrice 代理「直近バー値」は、`ts` がバー開始のため **as-of backward で未来の close を掴む** | **`ts = s − 5m` のバーの close と明示**(§8.1) |
| **Y15** | medium | exit を `t+h`(行オフセット)で書いていた。欠損バーがあると horizon が狂う | **時刻基準へ変更**(§6.1) |
| **Y16** | medium | 清算の判定価格・約定価格が未指定 | **不利側の intrabar 極値で判定**(取引所イベントなので look-ahead ではない)(§11.3) |
| **Y17** | medium | 昇格条件 (d)「break-even ≥ 5bps」が (a)「base_taker で net>0」と**代数的に同値**だった | **2倍マージンへ**(§14.1) |
| **Y18** | medium | layer 1 の replication gate が**定性的**(「大きい」「時間変動」に数値が無い) | **数値を凍結**(§18.1) |
| **Y19** | medium | 重複ありの5分グリッド系列に **p / CI を付けられる artifact スキーマ**だった | **推論欄を非重複ブロックへ分離**(§20) |
| **Y20** | low | `basis_rel_ma_*` の説明(左閉窓)と availability(`close_of_bar`)が矛盾 | **`start_of_bar` に統一 + 窓完全性条件**(§4.2) |

---

## 1. Primary Research Question

> **A2 が Binance の 2020-01-08 〜 2024-03-11 で報告した
> random-maturity arbitrage(BTC spot ロング / perpetual ショート、
> スプレッドが no-arbitrage 境界を超えたら建て、理論関係へ戻ったら解消)は、
> ango が独立に収集したデータの、A2 のサンプル外の期間でも再現するか。
> そして個人が負担する two-leg・4約定コストと資金拘束の後に経済性が残るか。**

```text
Q1 (replication) : 機序は ango のデータにも存在するか      → 符号と形で判定
Q2 (extension)   : 個人スケールのコスト後に経済性が残るか  → 経済指標で判定
```

**Q1 成立 かつ Q2 不成立は正常な結論である**(Phase 7 の
「statistically positive, economically negative」と同型)。そのとき Q2 で Q1 を否定しない。

---

## 2. Non-goals

1. **方向予測をしない。** delta-neutral を維持する。
2. 新しい戦略を発明しない。A2 の機序の再現が出発点。
3. execution optimizer / RL / maker queue simulator を実装しない。**maker fill を仮定しない。**
4. cross-venue を扱わない(Binance 単独)。
5. **dated futures を扱わない**(A1 の対象だが ango はデータを持たない)。
6. ETH その他を扱わない(H9 まで BTC 単独)。
7. **Final OOS(`ts >= 2026-01-01`)を開封しない。**
8. Phase 7 の artifact を再評価・再探索しない。
9. 5分足を捨てない。

---

## 3. Replication / Extension の境界

### 3.1 忠実再現(replication)— A2 のみ

| # | 内容 | 出所 |
|---|---|---|
| R1 | funding は8時間ごとに支払われ、**直前8時間の futures–spot スプレッドの平均に概ね等しい** | A2 |
| R2 | perpetual の理論価格からの**乖離が存在**する | A2 |
| R3 | 取引コストがあると no-arbitrage 価格は**点ではなく帯**になる | A2 |
| R4 | **random-maturity arbitrage**: 帯の外で建て、コスト無しの理論関係へ戻ったら解消 | A2 |
| R5 | 乖離は**時間とともに縮小する** | A2 |

### 3.2 明示的に範囲外

N1 BTC+ETH の cross-section / **N2 dated futures(= A1 の対象)** / N3 multi-venue /
N4 投資家層の識別 / N5 裁定資本制約の構造推定 / N6 A2 の options・stablecoin 借入金利の完全再現
(**stablecoin 借入金利は代理を使う。代理であることを明記する** → H11)。

### 3.3 ango 独自の拡張(extension。**replication と別節で報告**)

E1 個人スケールの two-leg コスト後経済性を primary endpoint に置く /
E2 decision-time observability の厳密化 / E3 **固定 horizon arm(記述的 robustness のみ)** /
E4 randomization 対照 / E5 always-on baseline。

### 3.4 A1 の役割(**再現対象ではない**)

A1 は **dated futures** の carry を 2019-03 〜 2024-07 で分析し、
取引所横断平均 carry ≈ **7% p.a.**、原因を limits to arbitrage に帰した。
本 Phase では **「なぜ carry が消えないのか」の経済的説明**としてのみ引用し、
**A1 の数値を ango の結果と比較しない**(商品が違う)。

---

## 4. 情報集合

### 4.1 データセット

| 記号 | データ | 出所 | 粒度 |
|---|---|---|---|
| `SPOT` | Binance spot BTCUSDT klines | `data/spot/monthly/klines/BTCUSDT/5m/` | 5m |
| `PERP` | Binance USDT-M perp klines | `data/futures/um/monthly/klines/BTCUSDT/5m/` | 5m(保有済) |
| `FUND` | funding rate | Vision `monthly/fundingRate/BTCUSDT/` + 公式 REST(`markPrice` 付き) | 8h イベント |
| `IDX` | index price klines | `data/futures/um/monthly/indexPriceKlines/BTCUSDT/5m/` | 5m |
| `PREM` | premium index klines | `data/futures/um/monthly/premiumIndexKlines/BTCUSDT/5m/` | 5m(保有済) |

### 4.2 observable

| 列 | 定義 | availability |
|---|---|---|
| `spot_close` / `perp_close` | 各 5m バーの close | `close_of_bar` |
| `basis_abs` | `perp_close − spot_close` | `close_of_bar` |
| `basis_rel`(= A2 の ρ) | `(perp_close − spot_close) / spot_close` | `close_of_bar` |
| `funding_last_settled` | **決定時点以前に決済が確定した直近 funding**(§5.2) | `start_of_bar` |
| `funding_interval_hours_last` | 同上の行の間隔(**8 とハードコードしない**。X5) | `start_of_bar` |
| `basis_rel_ma_w` | `basis_rel` の**左閉窓**移動平均(現在バーを含まない)。窓 `w` は §14.2 で凍結。**窓完全性を満たさない行は null**(data_contract §5) | **`start_of_bar`**(Y20) |
| `arb_bound_upper(c)` / `arb_bound_lower(c)` | コスト階層 `c` における A2 の no-arbitrage 帯(§6.3) | `close_of_bar` |

**禁止列**: 次回 funding(`predicted` / `estimated` を含む)、建玉後の premium から計算される量、
`fwd_` 接頭辞の一切。

---

## 5. decision time と availability

### 5.1 約定規則(data_contract §2 を継承)

```text
features = close[t] までに観測可能
signal   = close[t] の後
fill     = open[t+1]   ← spot leg と perp leg の両方
```

### 5.2 funding の観測可能性(**Y5 で修正**)

`calc_time` は決済時刻である(X4 で確定)。**tolerance ではなく key のシフトで扱う。**

```text
1) 公開遅延シフト(未来参照を防ぐ本体):
     funding_key = settlement_time + DELTA_PUB        (DELTA_PUB >= 0、凍結値)
2) as-of backward join:
     funding_last_settled(t) = 最後の行で funding_key <= ts_t
3) 陳腐化ガード(古すぎる値を使わない。未来参照とは無関係):
     ts_t - funding_key > MAX_STALE なら null
```

- **`DELTA_PUB` を大きくすることが安全側**、`MAX_STALE` を大きくすることは安全側ではない。
  v1 はこの2つを1つの `tolerance` に混同していた(Y5)。
- `ts_t`(バー開始)基準にすることで、signal 時刻(バー close)から**最低5分前**の情報のみ使う。
- `DELTA_PUB` / `MAX_STALE` の凍結値は **H4 解決済みなので確定可能**(§22)。

---

## 6. 取引構造(**Y1 / X3 で全面改訂**)

### 6.1 建玉と決済(時刻基準。**行オフセットで書かない** — Y15)

```text
entry(バー t で条件成立 → バー t+1 の open で約定):
    spot leg : BUY   q BTC  @ S_in = spot_open[t+1]
    perp leg : SELL  q BTC  @ P_in = perp_open[t+1]

exit(条件成立バー u → バー u+1 の open で約定):
    spot leg : SELL  q BTC  @ S_out
    perp leg : BUY   q BTC  @ P_out

exit_fill_time は entry_fill_time + (経過時間) として時刻で解決する。
該当 ts のバーが存在しなければ M6b(§9)の roll-forward を適用する。
```

数量は**定額名目**で固定する(Y11):

```text
q_i = N0 / S_in,i        N0 は凍結定数
```

### 6.2 損益の恒等式

```text
PnL_gross = q(S_out − S_in) − q(P_out − P_in) + Funding
          = q(D_in − D_out) + Funding          D = P − S
```

収益源は **(i) basis の縮小** と **(ii) funding 受取**のみ。§21 T3 で数値検証する。

> **Y2 の警告**: entry が**連続グリッド**(exit の瞬間に次を建てる)だと、
> `Σ q(D_in − D_out)` は `q(D_start − D_end)` へ **telescope** し、
> 戦略は「always_on + 余分な4約定コスト」に縮退する。
> **したがって entry には必ず「建てない期間」を作る条件が要る。** これが §6.3 の閾値規則である。

### 6.3 Arm 定義(**primary は Arm R のみ**)

| arm | entry | exit | 位置づけ |
|---|---|---|---|
| **Arm R(replication)** | `basis_rel > arb_bound_upper(c)` | `basis_rel <= theoretical_relation_no_cost` | **primary。A2 の random-maturity arbitrage** |
| Arm B(baseline) | 期間開始で1回 | 期間終了で1回 | always_on carry |
| Arm E(extension) | 固定 horizon の非重複グリッド | entry + h | **記述的 robustness のみ。promotion 対象外**(Y2) |

- `arb_bound_upper(c)` はコスト階層 `c` の関数として **A2 の導出に従って実装**する。
  **実装式は凍結前に `phase8_prereg.py` へ書き下し、A2 本文の該当式を引用する**(→ H11)。
- **コスト階層 `c` の集合は事前登録で凍結し、後から増やさない**(§15)。

---

## 7. コストモデル(両脚)

### 7.1 成分と適用

脚ごとに独立の `CostConfig` を持つ `TwoLegCostConfig` を導入する。

```text
cost_total = q·S_in ·c_spot + q·P_in ·c_perp + q·S_out·c_spot + q·P_out·c_perp
```

**約定は entry 2 + exit 2 の計4回。** これが単一銘柄戦略との決定的な差である。

### 7.2 シナリオ

| シナリオ | spot 片道 | perp 片道 | 位置づけ |
|---|---:|---:|---|
| `maker_low` | 1 bps | 1 bps | **参考のみ**(maker fill を仮定しない) |
| `base_taker` | 5 bps | 5 bps | **primary** |
| `stress` | 10 bps | 10 bps | **昇格ゲート**(§14.1 (d)、Y17) |

実 fee 表は H6 の確定後に反映する。**確定までは仮置きであることを artifact に明記する。**

### 7.3 4約定コストの含意(**結果ではなく算術**)

`base_taker` で往復 **20 bps**。8時間 funding 1回の受取を `f` とすると
コスト償却に必要な決済回数は `n ≈ 0.0020 / f`。
外部知識 K1/K7(既に汚染台帳に記録)によれば `f` は 8時間あたり概ね 1e-4 未満のオーダーで、
`n` は数十回=**1週間以上**になる。

> **帰結**: 短い保有は原理的に不利である。Arm R は保有期間を内生に決めるので
> この制約を設計に埋め込む必要はないが、**Arm E の短 horizon が落ちることは予想済み**であり、
> 予想を先に書いておく(事後に「最初から見なかった」と言わないため)。

---

## 8. funding 収支

### 8.1 受払規則(**Y6 / Y14 / X5 で修正**)

```text
short perp を保有中、各決済 s において:
    cash_flow(s) = + q · MarkPrice(s) · f(s)         f(s) > 0 なら short の受取

対象となる決済(Y6):
    entry_fill_time < s <= exit_fill_time
```

- **境界を `<` / `<=` にした理由**: 決済 `s` は **`s` で終わる区間**の保有を精算する。
  v1 の `entry <= s < exit` では、決済時刻ちょうどに建てた瞬間に
  **保有していない直前8時間分を受け取ってしまう**。
- **MarkPrice(s)**: **公式 REST の `markPrice`(決済時点)を primary とする**(X6 / H10)。
  代理を使う場合は **`ts = s − 5m` のバーの `perp_close`**(Y14)。
  「直近バー」と書くと `ts <= s` のバー、すなわち close が `s` より**後**のバーを掴む。
- **間隔**: `funding_interval_hours` を**行ごとに読む**(X5)。8 をハードコードしない。
- **按分しない**(決済時刻に建玉が無ければ 0)。

### 8.2 funding 捕捉率(secondary)

```text
funding_capture = 実現 funding 受取 / 同期間 always_on の funding
```

会計比率であり **GO 判定には使わない**。

---

## 9. hedge mismatch

| # | 源 | 扱い |
|---|---|---|
| M1 | 数量の丸め(lot step 差) | 共通 step へ丸め、残差を記録 |
| M2 | spot open と perp open の価格差 | §6.2 に内包 |
| M3 | funding 受取が USDT で積み上がる | **再投資しない**(v1 では単利) |
| M4 | spot 手数料の建て通貨 | USDT 建て計上に固定(H6) |
| M5 | 証拠金の変動 | §11.3 |
| **M6a** | **entry 時**にどちらかの脚の価格が無い | **建てない**(現在情報のみ。look-ahead でない) |
| **M6b** | **exit 時**にどちらかの脚の価格が無い | **entry を遡って無効化しない。次に両脚が揃うバーへ roll-forward する**(Y7) |

`tracking_error` を毎バー記録し分布を報告する。**閾値による除外はしない。**

---

## 10. Arm E の候補 horizon(**promotion 対象外**)

```text
H = { 8h, 24h, 72h, 168h, 336h, 720h }
```

- **Y2 により、固定 horizon の連続グリッドは always_on へ縮退する。**
  したがって Arm E は「縮退することの実証」と記述統計のためだけに走らせる。
- **Arm E は §15 の family に入らず、GO 判定にも使わない。**
- 非重複グリッドは **固定 epoch 基準**で刻む(Y8):
  `entry_time ∈ GRID_EPOCH + k·h`、`GRID_EPOCH = 1970-01-01T00:00Z` を凍結。
  layer 間で位相がずれないことを保証する。

---

## 11. turnover と資金拘束

### 11.1 拘束資本(**Y11 で修正**)

```text
deployed_capital = N0 · (1 + 1/L)      ← 定額名目なので全 trade・全 layer で同一
```

- `N0` と `L` を凍結する(`L` の暫定値 3。H6 で確定)。
- 感応度として `L ∈ {1,2,3,5}` を報告するが **primary は凍結した1つ**。
  `L` は分母に決定論的に入るため、**`NARDC(L)·(1+1/L)` が清算ゼロ時に一定**であることを
  テストで検証する(§21 T16)。

### 11.2 turnover / exposure

`turnover` と **`exposure`(建玉していた時間の割合)を arm ごとに必ず記録する**(Y10)。

### 11.3 清算(**Y16 で明確化**)

- 維持証拠金の判定は **不利側の intrabar 極値**(short perp なので `perp_high[t]`)で行う。
  **これは look-ahead ではない** — 清算は戦略の意思決定ではなく**取引所のイベント**であり、
  不利側極値で評価するのが保守側である。
- 強制決済は **トリガー価格 + スリッページ**より良い価格では約定しない。
- 維持証拠金 tier は H6 の確定後に凍結。清算件数を artifact に記録する。

---

## 12. baseline(**Y9 / Y10 で修正**)

| ID | baseline | 目的 |
|---|---|---|
| `B0` | always_flat | ゼロ基準 |
| `B1` | buy_and_hold_spot | 方向性ベンチマーク(無相関の検算) |
| `B2` | **always_on_carry** | タイミング規則が上回るべき基準 |
| `B3` | **randomization 対照** | **単一 seed ではなく分布**(§15.0) |
| `B4` | funding_sign_rule | 素朴規則 |

**B3 の再定義(Y9)**: 建玉回数と保有期間分布を Arm R に一致させたランダム entry を
**`K_RANDOM = 1,000` の事前登録 seed** で生成し、**分布**として扱う。

**B3 の適用範囲(Y10)**: `exposure < EXPOSURE_GUARD`(凍結値 0.7)の arm に対してのみ
**昇格・NO-GO の判定条件として使う**。exposure がそれ以上の arm では
**B3 は報告するが判定に使わない**(自由度が無く原理的に無情報のため)。
その場合は **B2 との比較が主たる対照**になる。

---

## 13. split と外部知識汚染

### 13.1 3層(**X2 / X3 で境界を修正**)

```text
layer 1  literature_in_sample      ts <  2024-08-01
         = max(A2 終端 2024-03-11, A1 終端 2024-07) を月境界へ切り上げ
layer 2  contaminated_confirmation 2024-08-01 <= ts < 2026-01-01   (約 17 ヶ月)
layer 3  prospective_final         ts >= PHASE8_PROSPECTIVE_START   ← H5
```

**v1 は layer 1/2 の境界を 2023-11-19 としていた。X2 により誤りである。**
独立窓は 26ヶ月ではなく **約 17ヶ月**しかない。

### 13.2 layer 2 の用途(**Y12 で正直に書き直し**)

v1 は「layer 2 を効果量の発見に使わない」と書きながら、実際には3箇所で layer 2 の
効果量を数値入力にしていた。**正しくは以下である。**

| layer 2 の用途 | 使う | 汚染の影響 |
|---|:-:|---|
| 機序の符号・形の再現確認 | ○ | 小(符号は K1/K2 で既知だが、形は既知でない) |
| **昇格ゲートの評価**(§14.1) | ○ | **あり。K1/K7/K8 で較正されている** |
| **layer 3 の MDE 推定**(§16.4) | ○ | **あり** |
| 新規の効果量の「発見」 | **×** | — |

> **明記**: §14.1 の昇格閾値・§16.4 の MDE は **汚染された窓で較正されている**。
> したがって **GO の最終判定は layer 3 のみで行い、layer 2 の数値は比較基準にしない**
> (§18.3 を絶対基準へ変更した理由。Y4/Y12)。

### 13.3 封印の実装(**Y13 — 実装上の危険**)

**この repository の封印は load 時ではなく `normalize_binance` の書き出し時に効く。**
`apply_screening_cutoff` が `ts >= FINAL_OOS_START` の行を**物理的に落として**から
parquet を書く。したがって:

- **既存の cutoff をグローバルに可変化してはならない**(Phase 7 の再現性が壊れる)。
- `mce.backtest.splits` に **`PHASE8_PROSPECTIVE_START` を別定数として追加**し、
  Phase 8 の正規化器に **target ごとの cutoff** を渡す。
- **H5 が承認されるまで、Phase 8 の正規化器は `FINAL_OOS_START` を使う。**
  すなわち **layer 3 のデータは取得も生成もされない。**
- layer 3 は既存 `final_oos` の内側にあるため、**その採用は firewall 改訂(freeze v2)であり
  人間の明示的承認を要する**(H5)。
- **本 draft は `mce.backtest.splits` を一切変更していない。**

### 13.4 外部知識汚染台帳(凍結時に転記)

| id | 内容 | 期間 | status |
|---|---|---|---|
| K1 | **dated futures** carry は取引所横断平均 ≈ 7% p.a.、spike 時 40% 超 | 2019-03〜2024-07 | `VERIFIED-FULL` |
| K2 | perpetual の理論価格からの乖離は通貨市場より大きく、**縮小する** | 2020-01-08〜2024-03-11 | `VERIFIED-FULL` |
| **K8** | **A2 の BTC random-maturity arbitrage は Sharpe 1.8(リテール高コスト)/ 最大 3.5** | 2020-01-08〜2024-03-11 | `VERIFIED-FULL` |
| K7 | (**未検証**)Hyperliquid 単一 venue carry 17.9%(2024)/ 3.6%(2025) | 2024–2026 | `UNVERIFIED` |

**K8 は本候補にとって最も汚染的な数値である。** 報告された risk-adjusted の大きさを
実行前に知ってしまった。**いかなる閾値の設定にも使ってはならない。**

---

## 14. 昇格規則

### 14.1 layer 2 → layer 3

```text
Arm R を、事前登録した各コスト階層 c について評価し、以下を全て満たすものだけ昇格:
  (a) 主統計量 mean(r_i) > 0                      (primary cost scenario)
  (b) B2(always_on)を上回る
  (c) exposure < EXPOSURE_GUARD の場合のみ: B3 の randomization 帰無を棄却(§15.0)
  (d) stress シナリオでも mean(r_i) > 0            ← Y17 で (a) と独立にした
  (e) n_trades >= MIN_TRADES['layer2']             ← Y4
  (f) 多重比較補正後に有意(§15)
昇格 0 件 → layer 3 を開かずに negative result で閉じる(§19 F1)
```

**(d) の変更理由(Y17)**: v1 の「break-even ≥ 5bps」は primary が `base_taker`(5bps)
であるため **(a) と代数的に同値**で、独立な条件として機能していなかった。
`stress`(10bps)での正値要求に置き換えることで **2倍のマージン**を要求する。

### 14.2 補助パラメータ

`basis_rel_ma_w` の窓は **{24h, 168h}** に固定。**探索しない。**

---

## 15. 多重比較補正と帰無仮説

### 15.0 帰無仮説と p の作り方(**Y3 — v1 に欠落していた中核**)

v1 は Holm 補正を要求しながら帰無も検定統計量も定義していなかった。
Phase 7 の A-projection placebo に相当するものを置く。

```text
帰無 H0: 「entry のタイミングは、それが生む exposure を超える情報を持たない」

実現: B3 randomization(§12)
  - Arm R の建玉回数と保有期間分布に一致するランダム entry を K_RANDOM = 1,000 seed で生成
  - 各 seed で主統計量 mean(r_i) を計算し、帰無分布を作る
  - p = (1 + #{ stat_random >= stat_obs }) / (1 + K_RANDOM)
  - seed 列は事前登録で凍結する(実行後に増やさない)
```

- **exposure >= EXPOSURE_GUARD の arm では、この帰無は無情報である**(Y10)。
  その場合は p を報告するが判定に使わず、**B2 との経済的比較のみで判定する**。
  この分岐は**事前に凍結する**(結果を見て選ばない)。

### 15.1 family

| family | 要素 | 補正 |
|---|---|---|
| **primary family** | Arm R × 事前登録コスト階層(**凍結した本数のみ**) | **Holm-Bonferroni**(FWER 0.05) |
| **family に入れないもの** | Arm E(§10)、cost scenario の感応度、`L` 4値、mark 代理2種、bootstrap ブロック長3種 | 補正しない。**GO 判定に使わない** |

- **位相 4系列は family に入れない**(Y8)。Arm E にのみ存在し、
  **固定 epoch 基準で刻み、4系列の平均を1つの記述統計に畳む**。
- 補正前の p も併記する。

---

## 16. effective N・不確実性・標本下限

### 16.1 位相と重複(Y8 / Y19)

- Arm R は **event-driven** なので位相の概念が無い(閾値が決める)。
- Arm E のみ非重複グリッド。**`GRID_EPOCH = 1970-01-01T00:00Z` 固定**、
  4位相は**平均して1つの数**にする。
- **重複ありの5分グリッド系列には p / CI / MDE を計算しない**(点推定のみ)。
  artifact のスキーマでも分離する(§20)。

### 16.2 主 CI(**Y-統計 で修正**)

**再抽出単位は「日」ではなく「trade」である。**

```text
primary CI : 非重複 trade の純リターン r_i に対する stationary bootstrap
secondary  : day-cluster block bootstrap(ブロック長 1日 / 7日 / 30日)を参考として併記
```

v1 は Phase 7 の day-cluster bootstrap を流用していたが、Phase 7 の観測単位は
**バー単位の回帰残差**であり、ここでの推定量は**数週間にわたる trade の和**である。
単位が違う。

**CI が 0 を含む場合は、有意であっても「効果量の下限は確定していない」と併記する。**

### 16.3 標本下限(**Y4**)

```text
MIN_TRADES = { 'layer1': ..., 'layer2': 30, 'layer3': 20 }   ← 凍結値
```

- **`n` は暦の算術で決まるので、どのデータも開かずに事前評価できる。**
- layer 3 の `n` が下限を割る arm は **`insufficient_sample` と記録し、GO も NO-GO も出さない。**
  「検出力が無かった」ことを「効果が無かった」と書かない。

### 16.4 MDE(**Y-統計 で式を明示**)

```text
MDE(layer3) = t(0.975, n3 − 1) · sd(r_i | layer2) / sqrt(n3)
```

- `sd` は **layer 2 の非重複 trade 単位**のものを使い、**その sd 自体の CI も報告する**。
- **ゲートにする**: `MDE(layer3) > mean(r_i | layer2)` の arm は
  **layer 3 を開く前に `insufficient_power` と記録する**。
- **この MDE は汚染された layer 2 で較正されている**(§13.2)。そのことを併記する。

---

## 17. Primary endpoint(**Y11 で再定義**)

### 17.1 主統計量

```text
定額名目:        q_i = N0 / S_in,i
拘束資本:        C   = N0 · (1 + 1/L)         ← 全 trade で同一
trade 純リターン: r_i = PnL_net,i / C
主統計量:        mean(r_i)  (n = 非重複 trade 数)

PnL_net,i = q_i(D_in − D_out) + Funding_i − cost_4legs,i − 清算損失_i
```

**経済的見出し**として、`NARDC = Σ PnL_net / (C × 経過年数)` を**併記**する。
ただし**検定は `mean(r_i)` で行う**(集計が一意に定まり、n が明示されるため)。

### 17.2 Secondary

`break_even_cost_bps`(片道)/ trade 単位 Sharpe / max drawdown / 最大清算距離 /
turnover / **exposure** / `funding_capture` / `tracking_error` 分布 / B1 との相関。

### 17.3 primary にしないもの

方向正解率・hit rate / gross return / 単年の最良値。

---

## 18. GO / NO-GO

### 18.1 layer 1(replication gate。**Y18 で数値化**)

**layer 1 のデータを見る前に `phase8_prereg.py` へ数値を凍結する。**
凡庸で標準的な定義を決め打ちし、駄目なら駄目と報告する(恒久ルール6 の精神)。

```text
R1 confirmed : corr( f(s), 直前8時間の basis_rel 平均 ) >= RHO_MIN
R2 confirmed : |basis_rel| が帯の外に出た区間の割合 >= OUT_OF_BAND_MIN
R5 confirmed : basis_rel の年次分散が単調非増加、または回帰の時間トレンド係数 < 0
replication 失敗 : f(s) > 0 の割合が 0.5 を有意に下回る(符号が体系的に逆)
```

**失敗なら Q2(extension)へ進まない。**

### 18.2 layer 2 → layer 3

§14.1 の (a)–(f) を全て満たす arm のみ昇格。

### 18.3 layer 3(最終判定。**Y4 / Y12 で絶対基準へ変更**)

v1 は Phase 7 の「layer2 の 0.5 倍」を流用していた。**これを廃止する。**

理由: (i) layer 3 の `n` は Phase 7 の 1/100 以下で比が極端に不安定、
(ii) layer 2 の値自体が汚染較正されている、
(iii) funding 水準の**secular な低下**があると、機序が再現していても比は下がる。

```text
GO                  : mean(r_i | layer3) > 0
                      かつ 符号が layer 2 と一致
                      かつ stress シナリオでも > 0
                      かつ n3 >= MIN_TRADES['layer3']
                      かつ exposure < EXPOSURE_GUARD なら B3 帰無を棄却
insufficient_sample : n3 < MIN_TRADES['layer3']            → GO も NO-GO も出さない
insufficient_power  : MDE(layer3) > mean(r_i | layer2)      → 同上
NO-GO               : 符号反転、または stress で負
```

- **判定に p を主基準として使わない**(小標本で構造的に出にくいため)。
- **GO は実運用の許可ではない。** 意味は「Phase 8.2(執行の精緻化・collateral 制御)へ昇格」。

### 18.4 救済の禁止

NO-GO を、閾値・窓・cost scenario・`L`・コスト階層の入れ替えで救済しない。

---

## 19. negative result として閉じる条件(事前凍結)

| # | 条件 | 帰結 |
|---|---|---|
| F1 | layer 2 で **全 arm が昇格条件を満たさない** | **economically negative** で閉じる。layer 3 を開かない |
| F2 | `exposure < EXPOSURE_GUARD` の arm が **B3 帰無を棄却できない** | 機械的効果。**棄却** |
| F3 | **stress で負**(§14.1 (d) 不通過) | コスト想定のずれで消える。**監視リスト止まり** |
| F4 | layer 1 で **符号が A2 と逆** | **replication 失敗**。extension を実行しない |
| F5 | 品質ゲート不通過で、**修正が結果を見た後になる** | 停止。**ゲートを緩めない** |
| F6 | layer 3 で **符号が再現しない** | **NO-GO** |
| F7 | 昇格 arm が1つだけ | 補正後の生存を確認。単独生存は **conditional hold** 止まり |
| F8 | **`n` が下限を割る / MDE が大きすぎる** | `insufficient_sample` / `insufficient_power`。**「効果が無い」と書かない** |
| F9 | Arm E が **always_on へ縮退する**ことが実証された | **予測どおり**として記録(Y2)。Arm R の判定には影響しない |

---

## 20. experiment artifact 仕様

`experiments/phase8/carry_{layer1,layer2,layer3}_v1.json`(追記専用・省略なし)。

```text
protocol, prereg_sha256, prereg_module_sha256, source_commit, uv_lock_sha256
manifests(spot / perp / funding / index / premium), source_digest
layer, layer_bounds(assert 済み), sealed_rows_present == 0
funding_interval_hours_distribution        ← X5 の検算
per_arm:
  arm ("R" | "B0".."B4" | "E:<h>"), cost_tier, exposure
  primary_nonoverlapping:                  ← Y19: 推論欄はここだけ
      n_trades, mean_r, NARDC, stationary_bootstrap_ci_95,
      p_randomization, p_holm, promoted, promotion_reason,
      mde, sample_floor_status ("ok"|"insufficient_sample"|"insufficient_power")
  robustness_overlapping:                  ← 点推定のみ。p / CI を持たない
      n_trades, mean_r, NARDC
  break_even_cost_bps, sharpe_trade, max_drawdown, turnover,
  funding_capture, tracking_error_{p50,p95}, funding_received_total,
  cost_total_by_leg, liquidation_count, min_margin_distance
randomization: { K_RANDOM, seeds_sha256, null_distribution_summary }
sensitivity  : cost scenario / L 4値 / mark 代理2種(family 外)
excluded     : M6a skip 件数、M6b roll 件数と理由
external_knowledge : §13.4 の台帳を埋め込む
```

**昇格しなかった arm も削除せず理由付きで残す。**

---

## 21. 実装予定ファイルとテスト

### 21.1 実装

| ファイル | 内容 | 新規/変更 |
|---|---|---|
| `src/mce/binance_vision.py` | spot / fundingRate / indexPriceKlines のパス追加 | 変更 |
| `src/mce/binance_rest.py` | 公式 REST の funding(`markPrice` 付き)取得 | **新規**(X6) |
| `src/mce/normalize_binance.py` | 上記の正規化。**per-target cutoff**(Y13) | 変更(**既存 Phase 7 経路の既定値は不変**) |
| `src/mce/features_carry.py` | `basis_*` / `funding_last_settled` / `arb_bound_*` + availability 宣言 | 新規 |
| `src/mce/carry_quality.py` | 品質ゲート(グリッド・重複・欠測・spot–perp 整合・封印) | 新規 |
| `src/mce/backtest/two_leg.py` | two-leg 執行器(4約定・funding・清算・mismatch) | **新規**(既存 engine を壊さない) |
| `src/mce/backtest/costs.py` | `TwoLegCostConfig` 追加 | 変更(既存 `CostConfig` は不変) |
| `src/mce/backtest/splits.py` | **`PHASE8_PROSPECTIVE_START` 追加**(H5 承認後) | **H5 まで変更しない** |
| `src/mce/phase8_prereg.py` | 凍結パラメータ(`N0`/`L`/`DELTA_PUB`/`MAX_STALE`/`MIN_TRADES`/`K_RANDOM`/`EXPOSURE_GUARD`/`GRID_EPOCH`/コスト階層/§18.1 の数値) | 新規 |
| `src/mce/carry_runner.py` | layer 単位の実行器 + artifact | 新規 |
| `src/mce/carry_report.py` | artifact から表を**機械生成** | 新規 |

### 21.2 テスト

| # | テスト | 対応 |
|---|---|---|
| T1 | features に `fwd_` 列が無い | contract §4 |
| T2 | **合成データで未来 funding を混ぜて as-of join が掴まないこと** | §5.2 / Y5 |
| T3 | 損益恒等式 `PnL = q(D_in−D_out)+Funding` | §6.2 |
| T4 | 両脚が `open[t+1]` で約定 | §5.1 |
| T5 | **M6a は skip、M6b は roll-forward(entry を遡って消さない)** | §9 / Y7 |
| T6 | funding 境界が `entry < s <= exit` | §8.1 / Y6 |
| T7 | `sealed_rows_present == 0` | §13.3 |
| T8 | layer 境界が事前登録と一致 | §13.1 |
| T9 | **Phase 7 経路の cutoff 既定値が変わっていない** | §13.3 / Y13 |
| T10 | Holm family サイズが凍結値と一致(Arm E を含まない) | §15.1 |
| T11 | 凍結違反検出(実行後に spec を編集していない) | §0 |
| T12 | findings の表が artifact 由来 | §20 |
| T13 | 清算が**不利側 intrabar 極値**で判定される | §11.3 / Y16 |
| T14 | **B3 が randomization 分布(K_RANDOM 本)である** | §12 / Y9 |
| T15 | 本文の数値と `phase8_prereg.py` の一致 | — |
| T16 | `NARDC(L)·(1+1/L)` が清算ゼロ時に `L` 不変 | §11.1 |
| T17 | **exposure >= EXPOSURE_GUARD の arm で B3 が判定に使われない** | §12 / Y10 |
| T18 | **markPrice 代理が `ts = s − 5m` のバーを使う** | §8.1 / Y14 |
| T19 | **`funding_interval_hours` を行ごとに読む**(8 をハードコードしない) | X5 |
| T20 | Arm E の非重複グリッドが `GRID_EPOCH` 基準で layer 間整合 | §10 / Y8 |
| T21 | 重複あり系列に p / CI が付かない | §16.1 / Y19 |

### 21.3 凍結前チェックリスト

**未実装が1つでも残っている状態で凍結しない**(Phase 7 §6.2 の再発防止)。

- [ ] §5.2 の publication-delay シフト(T2)
- [ ] §6.3 の Arm R 閾値規則と `arb_bound_*` の実装(A2 の式を引用)
- [ ] §8.1 の funding 境界・markPrice・可変間隔(T6/T18/T19)
- [ ] §9 の M6a / M6b(T5)
- [ ] §11.3 の清算(T13)
- [ ] §12 の B3 randomization と exposure guard(T14/T17)
- [ ] §15.0 の帰無分布と p
- [ ] §16.2 の trade 単位 bootstrap
- [ ] §16.3 の標本下限・§16.4 の MDE ゲート
- [ ] §18.1 の数値化した layer 1 gate
- [ ] §13.3 の per-target cutoff と Phase 7 経路の不変性(T9)
- [ ] §20 の artifact 全フィールド

---

## 22. freeze blocker(v1.2 時点)

| # | 事項 | 状態 |
|---|---|---|
| ~~H4~~ | Binance `calc_time` の semantics | **✅ 解決**(= 決済時刻。X4) |
| ~~H2~~ | A1 のサンプル期間 | **✅ 解決**(2019-03〜2024-07。dated futures。X1/X2) |
| **H5** | layer 3 を設けるか / firewall 改訂(freeze v2)の可否 | **未解決・fatal** |
| **H6** | spot leg の執行前提・fee 表・margin tier・`N0`・`L` | **未解決・高** |
| **H11** | A2 の `arb_bound(c)` の実装式と、stablecoin 借入金利の代理 | **未解決・高**(新規) |
| H10 | 公式 REST `markPrice` を primary にするか | 未解決・中(**推奨: する**) |
| H9 | BTC 単独か ETH を足すか | 未解決・中 |
| H7 | ToS 上の利用可否 | 継続して要確認 |
| H8 | *Alpha Illusion* P1–P6 を報告規準として採用するか | 未解決・低(**推奨: 採用**) |

**H5 は依然として fatal である。** H11 が新たに fatal 相当に近い
(A2 の境界式を実装できなければ Arm R が定義できない)。

---

## 23. このプロトコルが言っていないこと

1. **carry が儲かるとは言っていない。** 損益を1つも計算していない。
2. **A2 が正しいとも間違っているとも言っていない。** 独立に確認する対象である。
3. **layer 2 で正の carry が出ても発見ではない**(K1/K2/K8 で符号も大きさも既知)。
4. **本文書は事前登録ではない。** §22 が解決するまで凍結しない。
5. **§7.3 の算術は結果ではない。** 外部知識由来の桁を代入した設計判断である。
6. **v1 の設計は「動かしてみて駄目だった」のではない。** 一度も実行せずに、
   全文取得と敵対監査だけで訂正した。**実行前に直せたことがこの改訂の要点である。**

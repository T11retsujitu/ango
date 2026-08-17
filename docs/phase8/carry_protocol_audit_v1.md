# Phase 8.1 carry replication protocol — 凍結前 独立監査記録 v1

- 実施日: 2026-08-17
- 対象: [carry_replication_protocol_v1](carry_replication_protocol_v1.md) の **v1(初版)**
- 結果: **v1.2 へ改訂**。fatal 4件・high 12件・medium 8件・low 2件を反映
- **本監査は実験を1つも実行していない。データも開いていない。**

---

## 0. なぜ凍結前に監査するか

この repository には前例がある。

- Phase 7 の事前登録では、統計監査が **「placebo が corr(X,A) を壊す」という fatal**
  を凍結前に検出した(commit `d7bc0b7`)。
- leakage 監査の指摘もラベル生成前に反映された(`861b273`)。
- 逆に、confirmation では **fold 構成の食い違いが結果の符号を反転させた**
  ([confirmation findings §6.1](../findings/2026-08-17-phase7-tier0-confirmation-v1.md))。

> **設計の誤りは、実行前なら訂正で済み、実行後なら交絡になる。**

したがって Phase 8.1 でも、**凍結前に**独立レンズで敵対的に潰す。

---

## 1. 監査の構成

5つの独立レンズが、それぞれプロトコルと repository の実コードを読んで欠陥を探す。
各指摘は**別のレビュアが反証を試みる**(既定の立場は「その指摘は誤りか過大である」)。

| レンズ | 観点 | 状態 |
|---|---|---|
| `leakage` | 先読み・情報漏洩・時刻規約 | **完了**(→ v1.2) |
| `statistics` | 帰無・多重比較・実効 N・検出力 | **完了**(→ v1.2) |
| `contract_and_repo` | data contract・恒久ルール・既存コードとの整合 | **完了**(→ v1.3) |
| `economics` | 執行現実性・資金・清算・両脚コスト | **完了**(→ v1.4) |
| `replication_fidelity` | 論文の主張と設計が本当に対応しているか | **未完了** |

**注記(正直な範囲宣言)**: 本記録は **完了した4レンズ**を反映している。
`replication_fidelity` は未完了であり、**追加の欠陥が出る可能性がある**。
v1.4 は「監査を通過した」のではなく「4レンズ分の指摘を反映した」状態である。
また各指摘に対する**独立レビュアの反証判定も未完了**である(§5)。

### レンズ別の検出数

| レンズ | fatal | high | medium | low |
|---|---:|---:|---:|---:|
| `leakage` | 2 | 6 | 4 | 1 |
| `statistics` | 4 | 6 | 4 | 1 |
| `contract_and_repo` | 2 | 6 | 6 | 2 |
| `economics` | 3 | 5 | 5 | 1 |

**重複を除いた反映件数: fatal 9・high 20・medium 15・low 4。**
うち **3件は2つ以上のレンズが独立に同じ結論へ到達した**
(entry 規則の欠落 / B3 が単一実現値 / layer 1 gate が定性的)。

これとは別に、**論文本文の独立取得**によって §2 の X1–X6 が判明した。

---

## 2. 論文の読み違い(監査とは別経路で判明)

| id | 内容 | 検証方法 |
|---|---|---|
| X1 | **A1 は dated futures の論文であり perpetual ではない** | BIS WP 1087 PDF 本文抽出 |
| X2 | **A1 のサンプルは 2019-03〜2024-07**(配信 PDF の版は "October 1, 2025") | 同上 |
| X3 | **A2 のサンプルは 2020-01-08〜2024-03-11、戦略は random-maturity arbitrage** | arXiv 2212.06888v6 PDF 本文抽出 |
| X4 | **`calc_time` は funding 決済時刻**(公式 REST `fundingTime` と全行一致) | Vision CSV と REST の直接照合 |
| X5 | cap/floor 到達で funding が**恒久的に1時間へ切り替わる**規則がある | 検証エージェント報告(**ango 未確認**) |
| X6 | `www.binance.com/fapi/v1/...` は **到達可**、`markPrice` が取れる | 直接実測 |

**教訓**: **書誌ページと abstract だけでは、論文が何の商品を扱っているかすら分からない。**
→ Phase 8 では**再現アンカーに `VERIFIED-FULL`(本文取得)を必須**とする。

---

## 3. `leakage` レンズの指摘と対応

| id | 深刻度 | 指摘 | v1.2 の対応 |
|---|---|---|---|
| Y1 | **fatal** | **entry 規則がどこにも定義されていない。** §10/§14 は horizon を列挙するだけで「いつ建てるか」を述べていない。§4.2 の `basis_rel_ma_*` と §14.2 の窓凍結は、存在しない規則を前提にしている | §6.3 で **Arm R(A2 の閾値規則)を primary として明示** |
| Y2 | **fatal** | **固定 horizon × 連続グリッドは縮退する。** §6.2 の恒等式で `Σ q(D_in−D_out)` が `q(D_start−D_end)` へ **telescope** し、戦略は「always_on + 余分な4約定」になる。§8.2 の `funding_capture` も §19 F1 も意味を失う | 固定 horizon を **primary から外し Arm E(記述的 robustness)へ降格**。F9 として「縮退の実証」を明記 |
| Y5 | high | **ε が as-of join の tolerance 側にあった。** tolerance を広げても未来参照は防げない(古い一致を許すだけ) | §5.2 を **key の publication-delay シフト + 別個の陳腐化ガード**へ書き換え |
| Y6 | high | **funding 境界 `entry <= s < exit` が誤り。** 決済時刻と約定時刻が一致する位相(h=8h の1位相で必ず発生)で、**建てた瞬間に直前8時間分を受け取る** | **`entry < s <= exit`** へ変更(決済は「s で終わる区間」を精算する) |
| Y7 | high | **M6(片脚欠測で trade 無効化)は exit 側では look-ahead 選択。** h 時間後のバーの状態で entry を条件づけている | **M6a(entry 側 skip)/ M6b(exit 側 roll-forward)**へ分割 |
| Y8 | high | 位相 4系列 × 6 horizon = **24 統計量なのに family を 6 と宣言**。畳み込み関数が未定義。さらに**位相の基準時刻が未指定**で、layer 間で整列が変わりうる | **`GRID_EPOCH` 固定**、4位相は平均で1統計量、Arm E は family 外 |
| Y12 | high | **layer 2 の扱いが自己矛盾。** 「効果量の発見に使わない」と書きながら、promotion gate・MDE・比較基準の3箇所で layer 2 の効果量を数値入力にしている | §13.2 で**用途を正直に列挙**し、**汚染較正であることを明記**。GO 判定を layer 3 の絶対基準へ |
| Y13 | high | **封印は load 時ではなく `normalize_binance` の書き出し時に物理的に効く。** layer 3 のデータは**そもそも生成されない**。既存 cutoff をグローバルに可変化すると **Phase 7 の再現性が壊れる** | **`PHASE8_PROSPECTIVE_START` を別定数化**し **per-target cutoff** に。H5 承認までは `FINAL_OOS_START` のまま。T9 で Phase 7 経路の不変性を検証 |
| Y14 | medium | **markPrice 代理「直近バー値」が未来参照。** `ts` はバー開始なので、`ts <= s` のバーの close は **`s` より後**に確定する | **`ts = s − 5m` のバーの close** と明示 |
| Y15 | medium | exit を `t+h`(行オフセット)で書いていた。§10 は時間単位。欠損バーがあると horizon が狂う | **時刻基準**へ変更 + M6b |
| Y16 | medium | 清算の判定価格・約定価格が未指定 | **不利側 intrabar 極値**で判定(取引所イベントなので look-ahead ではない)、トリガー + スリッページで約定 |
| Y18 | medium | layer 1 の replication gate が**定性的**なのに、失敗すると extension 全体を止める最重要ゲート | **§18.1 に数値を凍結**(データを見る前に) |
| Y19 | medium | 重複あり系列に **p / CI を付けられる artifact スキーマ**だった | §20 で **推論欄を非重複ブロックへ分離** |
| Y20 | low | `basis_rel_ma_*` の説明(左閉窓)と availability(`close_of_bar`)が矛盾。窓完全性条件も未記載 | **`start_of_bar` に統一 + 窓完全性**(contract §5) |

---

## 4. `statistics` レンズの指摘と対応

| id | 深刻度 | 指摘 | v1.2 の対応 |
|---|---|---|---|
| Y1 | **fatal** | (leakage と独立に同じ結論)**entry 規則が無い** | 同上 |
| Y3 | **fatal** | **帰無仮説・検定統計量・p の作り方が定義されていない**のに Holm 補正と `p_raw`/`p_holm` を要求している。Phase 7 の A-projection placebo に相当するものが無い | **§15.0 を新設**。帰無=「entry タイミングは exposure を超える情報を持たない」、実現=B3 randomization(K=1,000 凍結 seed)、`p = (1+#{≥obs})/(1+K)` |
| Y4 | **fatal** | **§18.3 の「layer2 の 0.5 倍」は Phase 7(n_eff 2,185–8,751)の規則。** layer 3(約90日)では h=720h で **3 trade**、h=336h でも数件。比の議論が成立しない | **§16.3 に標本下限**(暦の算術で事前評価可)、**§18.3 を絶対基準へ変更**、`insufficient_sample` を新設 |
| Y9 | high | **B3 が単一 seed の1本の実現値。** 「分布から1本引いたもの」は帰無分布ではない | **randomization 分布**(K_RANDOM=1,000)へ |
| Y10 | high | **常時建玉に近い arm では B3 が原理的に無情報。** 建玉回数と保有期間を一致させると自由度が残らず、対照が戦略自身へ収束する | **`EXPOSURE_GUARD`(0.7)を導入**。それ以上の arm では B3 を報告するが**判定に使わない**(B2 が主対照) |
| Y11 | high | **NARDC の集計が未定義**(分母は trade ごと、分子は総和)。数量 `q` も未定義 | **定額名目 `N0` を凍結**し、**trade 単位 `r_i` を主統計量**に。NARDC は経済的見出しとして併記 |
| — | high | **day-cluster bootstrap の再抽出単位が estimand と不一致。** Phase 7 の単位はバー単位残差、ここでは数週間の trade の和 | §16.2 を **trade 単位の stationary bootstrap** へ。day-cluster は参考 |
| — | high | **MDE に式もゲートも無い。** 事後の言い訳にしかならない | §16.4 に **式を明記しゲート化**。`insufficient_power` を新設 |
| — | high | **0.5 という比の閾値は funding 水準の secular な低下に汚染される。** 機序が再現していても比は下がる | §18.3 を**絶対基準**へ(比を使わない) |
| Y17 | medium | **昇格条件 (d)「break-even ≥ 5bps」が (a)「base_taker で net>0」と代数的に同値。** 独立な条件として機能していない | **(d) を `stress`(10bps)での正値要求**へ = 2倍マージン |
| — | medium | **Holm は「落ちると分かっている horizon」を含めると全体の閾値を下げる。** §7.3 が自ら 8h/24h は通らないと導出しているのに family に入れている | Arm E を **family から外した**ことで解消。Arm R のコスト階層のみが family |
| — | medium | §14.1(b) の「または always_on 自体が対象」という**逃げ道**が、baseline を promotion 対象にしてしまう | Arm 定義を §6.3 で分離し、**always_on は baseline B2 のみ**に確定 |
| Y18 | medium | (leakage と独立に同じ結論)**layer 1 gate が定性的** | 同上 |
| — | low | `L` は分母に決定論的に入るため、感応度として並べると機序と混同されうる | **T16 で `NARDC(L)·(1+1/L)` の不変性を検証** |

---

## 4A. `contract_and_repo` レンズ(v1.3 で反映)

**このレンズだけが実コードを読んで整合を確認した。** 主な指摘:

| id | 深刻度 | 指摘 |
|---|---|---|
| Y21 | **fatal** | §20 が全 layer に `sealed_rows_present == 0` を要求。**layer 3 は封印域の内側なので自己矛盾** |
| Y22 | **fatal** | **guarded loader を迂回**していた。layer 1 の一部は `RESEARCH_START` より古く `load_features` が構造的に返せない。`features_carry.py` が `AVAILABILITY` の**別名前空間**を作る |
| Y23 | high | **重複排除キー衝突**。`KEY_COLS=(source,symbol,ts)` に対し spot と perp は**どちらも `binance`/`BTCUSDT`**。**spot が perp を上書きしうる** |
| Y24 | high | **`source_digest` は ledger 全体のハッシュ**。データセット追加で **Phase 7 の凍結 artifact の digest が変わる** |
| Y25 | high | **layer / 封印境界の窓端規則が無い**。h=720h の entry が 2025-12 に建つと **exit が封印域を読む** |
| Y27 | medium | **`data_contract §8` は「Binance 代理 funding は執行 PnL に使わない」と定めている**。Phase 8 はそれを PnL の中心に置く → **契約改訂が必須** |
| Y30 | medium | **funding 公開遅延の頑健性条件が無い**(Phase 7 は §17-6 で +1バー遅延を GO 条件にしていた) |
| Y33 | low | **恒久ルール3(事前予想の記録)が実装されていない** |

## 4B. `economics` レンズ(v1.4 で反映)

**このレンズが最も重い発見をした。**

| id | 深刻度 | 指摘 |
|---|---|---|
| **Y34** | **fatal** | **ango は既にこの量を自分で測っていた。** [33ヶ月追試](../findings/2026-08-16-5m-tendencies-33mo-retest.md) が Binance BTC perp funding を **2023-11〜2026-07・3,012決済**で測定済み(**+0.69bps/8h・85.4%正・2026-02〜04 は負転**)。**既存封印域の7ヶ月を含む** |
| **Y35** | **fatal** | **B2(always_on)は清算される。** `L=3` なら価格 +32.8% で清算。layer 2 の BTC はそれを超えて上昇している。**追証規則が無ければ always_on は存在し得ない** |
| **Y36** | **fatal** | **コスト算術が誤り。** spot VIP0 taker は **10bps**(perp は 5bps)。**往復は 20bps ではなく 30bps**。在庫の +0.69bps/8h を入れると **h=720h 以外はゼロを超えない** |
| Y37 | high | **`q` が資本量に紐づいておらず、個人スケールが primary endpoint から消えていた**(lot step・`MIN_NOTIONAL` が効かない) |
| Y38 | high | **清算は mark price で起きるのに last price で判定していた。** しかも「mark 系列が無い」という前提自体が誤り(`markPriceKlines` は取得可能) |
| Y39 | high | **無リスク金利ハードルが無い。** 年率1〜3% を「正なので昇格」と判定してしまう |
| **Y40** | high | **失敗様式を取り違えていた。** §6.2 の恒等式より**暴落では carry は儲かる**。危険なのは **melt-up** である |

> **Y34 と Y40 は、外部の監査でなければ出てこない種類の指摘である。**
> Y34 は「自分の repository に既にある測定」を見落としていたもの、
> Y40 は「自分で書いた恒等式の含意」を追い切れていなかったものである。

## 5. 反証(verify)フェーズの状態

各指摘に対する**独立レビュアの反証判定は本記録の作成時点で未完了**である。
したがって上表の深刻度は**指摘者の申告値**であり、第三者の確認を経ていない。

**ango の対応方針**: 反証待ちの指摘であっても、
**修正がプロトコルを弱くしない限り先に反映する**(実行前なので修正は無料である)。
反証によって「不要だった」と判明した修正は、**戻さずに記録として残す**
(過剰に保守的な設計は、誤った設計より害が小さい)。

---

## 6. この監査が言っていないこと

1. **「v1.2 は正しい」とは言っていない。** 5レンズ中2レンズ分の指摘しか反映していない。
2. **「監査を通過した」とは言っていない。** 反証フェーズも残り3レンズも未完了である。
3. **指摘の深刻度は申告値である。** 独立確認を経ていない。
4. **実験は1つも実行していない。データも開いていない。Final OOS も未開封。**

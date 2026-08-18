# Phase 8.1 — BTC spot–perp funding carry:再現プロトコル **v1.8.5**(**FROZEN**)

- 作成日: 2026-08-17(v1) / 改訂: 2026-08-17(**v1.2** → **v1.3** → **v1.4** → **v1.5** → **v1.6** → **v1.7** → **v1.8** → **v1.8.1** → **v1.8.2** → **v1.8.3** → **v1.8.4** → **v1.8.5**)
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

### v1.3 追加(`contract_and_repo` レンズ。**既存コードとの整合**)

| id | 深刻度 | v1.2 に残っていた欠陥 | v1.3 の対応 |
|---|---|---|---|
| **Y21** | **fatal** | §20 が全 layer に `sealed_rows_present == 0` を要求していた。**layer 3 は封印域の内側にあるので自己矛盾** | **2つのフィールドに分離**(§20):`rows_outside_declared_layer_bounds == 0`(全 layer)と `existing_final_oos_rows_read`(H5 承認前は 0 でなければならない) |
| **Y22** | **fatal** | **guarded loader を迂回する設計になっていた。** (a) layer 1 の一部は `RESEARCH_START` より古く `load_features` が構造的に返せない。(b) `features_carry.py` が **`mce.features.AVAILABILITY` とは別の宣言名前空間**を作ってしまう | (a) **layer 1 用の loader を `mce.backtest.data` に明示的に追加**し、迂回ではなく**契約の拡張**として扱う。(b) carry の全列を **`mce.features.AVAILABILITY` へ登録**する(§4.2 / §21.1) |
| **Y23** | high | **正規化の重複排除キー衝突。** `KEY_COLS = (source, symbol, ts)` に対し spot と perp は**どちらも `source="binance"` / `symbol="BTCUSDT"`**。**spot が perp を上書きしうる** | **`market_type` をキーへ追加**(`data_contract §6` の改訂を**同一コミット**で行う)。§21.1 に `docs/data_contract.md` を変更対象として追加 |
| **Y24** | high | **`source_digest` は ledger 全体のハッシュ**。spot / funding / index を ledger に足すと **Phase 7 の凍結 artifact に記録された digest が変わる** | **Phase 7 の3 digest と件数を pin する回帰テストを先に追加**(T22)。Phase 8 のデータセットは **ledger を分けるか digest を dataset 単位に限定**する |
| **Y25** | high | **layer / 封印の境界での窓端規則が無い。** Phase 7 prereg §7 は専用の凍結節を持っていた。h=720h の entry が 2025-12 に建つと、**exit が封印域を読む** | **§16.5 を新設**: entry が admissible なのは `exit_fill_time` と区間内の全決済が **その layer の内側に収まる**ときのみ。除外件数を artifact に記録する |
| **Y26** | medium | §16.2 が「Phase 7 で実装済みの手法を再利用」としていたが、**その実装は存在しない**(`_bootstrap_ci` は private・日単位のみ・ΔR² 専用) | **新規コードであることを明記**し、reps と layer 別 seed を `phase8_prereg.py` へ凍結(§16.2) |
| **Y27** | medium | **`data_contract §8` は「Binance 代理 funding はキャリー統計専用。執行 PnL には使わない」と定めている。** Phase 8 は Binance funding を PnL の中心に置く | **契約 §8 の改訂が必須**。§21.1 に `docs/data_contract.md` を追加し、**コードと同一コミットで改訂**する(契約冒頭の要求) |
| **Y28** | medium | `two_leg.py` が既存 `execution.py` の欠損バー処理を**非互換に再実装**する(既存は次の存在バーで約定し `cancel_after_ms` で打ち切る) | **M6b は既存 `ExecutionConfig.cancel_after_ms` を再利用**する(§9)。独自の打ち切り規則を作らない |
| **Y29** | medium | **J7 クラスの失敗を捕まえるテストが無い**(`log(0) = -inf` が窓を汚染し、4暦月が静かに落ちた) | **T23 / T24 を追加**: 全 carry observable が有限 or null(±inf を許さない)/ 除外期間が artifact に**明細として**残る |
| **Y30** | medium | **funding の公開遅延に対する頑健性条件が無い。** Phase 7 は §17-6 で「+1バー遅延でも正」を GO 条件にしていた | **§18.2 / §18.3 に追加**: `funding_last_settled` を**さらに1決済間隔(8h)遅らせても主統計量が正**であることを昇格・GO の条件にする。+2間隔を感応度として報告 |
| **Y31** | medium | **`break_even_cost_bps` が4約定 trade に対して未定義。** 既存実装は単一銘柄の片道換算 | **§17.2 で定義を明示**: `(コスト前 PnL(funding 込・清算損失除く)) / Σ(4約定の名目) × 1e4` = **1約定あたり bps**。`base_taker` の 5bps と直接比較可能にする |
| **Y32** | low | `costs.py` の ADR は「funding は PnL 外(Q4 の決定)」と記録し、`apply_costs` は非ゼロ funding で `NotImplementedError` を投げる。Phase 8 は funding を PnL の中心に置く | **Phase 8 が two-leg carry PnL についてこの ADR を supersede することを明記**し、`costs.py` の docstring を同一コミットで更新(§21.1) |
| **Y33** | low | **恒久ルール3(事前予想を追試の前に記録し、実行者に見せない)が実装されていない** | **§7.4 を新設**: horizon / arm ごとの事前予想(符号・おおよその大きさ・昇格の可否)を**凍結記録へハッシュとして封入**する |

### v1.4 追加(`economics` レンズ。**執行と資金の現実性**)

| id | 深刻度 | v1.3 に残っていた欠陥 | v1.4 の対応 |
|---|---|---|---|
| **Y34** | **fatal** | **`ango は既にこの量を自分で測っている`。** [2026-08-16 の33ヶ月追試](../findings/2026-08-16-5m-tendencies-33mo-retest.md) が Binance BTCUSDT perp funding を **2023-11〜2026-07・3,012決済**で測定し、**平均 +0.69bps/8h・85.4%が正・2024-03 は年率+37%・2026-02〜04 は3ヶ月連続で負転**と記録している。**これは既存封印域(2026-01〜)の7ヶ月を含む** | **K9 として汚染台帳へ追加**(§13.4)。**外部知識より深刻**である(数値が精密で、既にコミット済みで、封印域に食い込んでいる)。§8 の設計算術の入力を **在庫の +0.69bps/8h に置き換える**(外部 K7 ではなく)。**layer 3 の開始を 2026-08 より後に置く根拠が強化された** |
| **Y35** | **fatal** | **B2(always_on)は清算される。** 初期証拠金 1/L・維持証拠金 0.40% なら、価格が `(1+1/L)/1.004 − 1` 上昇した時点で清算。**L=3 で +32.8%**。layer 2 の BTC は 37k から大きく上昇しており、always_on は**そのままでは存在し得ない** | **§11.4 を新設**: 最小限の**非最適化**証拠金維持規則(維持率が閾値を割ったら USDT を追加)を **trade の定義の一部**として凍結する。**collateral の最適制御はしない**(A3 の主題であり範囲外) |
| **Y36** | **fatal** | **コスト算術が誤り。** Binance の **spot VIP0 taker は 0.10%(10bps)**、USDT-M futures VIP0 taker は 0.05%(5bps)。**往復は 20bps ではなく 30bps**。在庫の +0.69bps/8h を入れると、非重複グリッドの年率コスト負担は h=168h で 15.6%、h=336h で 7.8%、h=720h で 3.7% となり、**h=720h 以外はゼロを超えない** | **§7.2 の暫定値を spot 10bps / perp 5bps へ修正**(H6 は下方修正のみ = 安全側)。**§10 の horizon grid に 1440h(60日)・2160h(90日)を追加**。**`always_on` を経済的な主 arm として昇格**させる |
| **Y37** | high | **`q` が資本量に紐づいていないため、個人スケールが primary endpoint から消える。** NARDC は `q` に対して不変で、lot step(perp 0.001 BTC)・`MIN_NOTIONAL` が効かない | **§11.1 で資本基準 `C`(USDT 建て)を凍結**し `q_i = C·L/((L+1)·S_in,i)`。**lot step へ丸め、`MIN_NOTIONAL` 違反の trade は棄却**して件数を記録 |
| **Y38** | high | **清算は mark price で起きるのに、last price(`perp_close`)で判定していた。** しかも「mark price 系列を持たない」という前提自体が誤り | **`markPriceKlines` を取り込む**(§4.1。2026-08-17 に到達確認済み)。**維持証拠金判定は mark price バーの不利側極値**で行う(§11.3) |
| **Y39** | high | **無リスク金利のハードルが無い。** layer 2 の期間の USD 短期金利は数%あり、**年率1〜3% の carry を「正なので昇格」と判定してしまう** | **§14.1 (a) を `mean(r_i) > r_f` へ変更**。`r_f` は**決定時点で観測可能な代理**を `phase8_prereg.py` に凍結する |
| **Y40** | high | **テール risk を測っていない。** §6.2 の恒等式より、**暴落では perp が安くなり `D_out` が下がるので carry は儲かる**。**危険なのは melt-up**(premium が吹き上がり funding が跳ね、short が清算される)。「清算までの距離」も max drawdown もテール指標ではない | **§17.2 に事前登録 secondary を追加**: horizon × `L` ごとの**清算確率**、**清算時の損失**(再ヘッジ/巻き戻しコスト込)、および**決め打ちの melt-up ストレスシナリオ** |
| **Y41** | medium | **hedge mismatch の列挙が不完全。** ヘッジは PnL 中立だが **証拠金中立ではない**(perp の損は先物ウォレットから即時に引かれ、spot の益は現物ウォレットに未実現で残り、short を支えない) | **M7 / M8 / M9 を追加**(§9)。累積 funding を維持証拠金の計算に含めるかを**明示的に凍結**する |
| **Y42** | medium | §17.1 は「送金の即時性・無料性を仮定しない」と書きながら、**§7.1 のコスト式に送金項が無い**(= 無料・即時を仮定している) | **`transfer_cost_bps` を `TwoLegCostConfig` に明示**し、**0 と根拠**(Binance 内の spot↔futures 振替は無料・ほぼ即時)を書く。仮定を可視化する |
| **Y43** | medium | §7.3 の「20bps(名目に対して)」がどの名目か未指定。exit 側の項は `S_out/S_in` に比例するので**往復コスト自体が確率変数** | §7.3 に「**entry 名目に対する比率、`S_out ≈ S_in` を仮定**」と明記し、**実現往復コストを artifact に記録**する |
| **Y44** | low | §20 が B0..B4 に同一指標を要求しているが、**資本構造が違う**(B1 に perp leg は無く、B0 に拘束資本は無い) | **§12 に baseline ごとの分母を明記**: B1 は `q·S`(証拠金なし)、B0 は同一の `C`(「何もしない」が 0 または `r_f` になるように) |

### v1.6 追加(`replication_fidelity` レンズ。**論文の主張と設計が本当に対応しているか**)

| id | 深刻度 | v1.5 に残っていた欠陥 | v1.6 の対応 |
|---|---|---|---|
| **Y45** | high | **`basis_rel` を「A2 の ρ」と等号で結んでいたが誤り。** A2 の ρ は **`κ(1−e^{−(f−s)}) − (r−r′)`、`κ=1095` で年率化**され、**stablecoin 借入金利 `r` を含む**。単純な相対 basis ではない | **§4.2 に `rho` を別列として正しく定義**。`basis_rel` は記述統計用に残すが **entry/exit 判定には使わない** |
| **Y46** | **fatal** | **A2 は2つの変種を検定している**。Table 7「Unrestricted」(両方向)と Table 8「Long-spot-only」。**Arm R は Table 8 の変種**だが、K8 に記録した Sharpe 1.8 は**別の表の数値**である | **§13.4 K8 を訂正**し、**Arm R がどちらの変種に対応するかを明示**する。比較相手を取り違えない |
| **Y47** | high | **H11 を「未解決・fatal 相当」としていたが、A2 は Table 3 の caption に境界式を逐語で書いている** | **H11 解決**。`ρ_l = κ log(1−C)` / `ρ_u = κ log(1+C)` を §6.3 に明記 |
| **Y48** | high | §3.1 R1 の帰属は正確だが、**A2 §4.2 は「funding はスプレッドを完全には追随しない」とも述べている**。§18.1 の R1 ゲートが厳しすぎると、A2 自身が認めている不完全さで replication 失敗になる | §18.1 の `RHO_MIN` は **A2 が認める不完全さを織り込んだ緩い値**にし、その根拠を併記する |
| **Y49** | high | **layer 1 に下限が無い。** 意図は 2020-01 だが、`splits.assign` は `RESEARCH_START`(2023-11-19)より前を `None` にする | **layer 1 = `2020-01-01 <= ts < 2025-06-01` と明示**し、Y22 の loader 拡張とセットで扱う |
| **Y50** | medium | **momentum 交絡。** A2 は「過去リターンの momentum が futures–spot ギャップを R² > 50% で説明する」と報告している。**`ρ > ρ_u` で建てることは、過去リターンが高い局面で建てることに近い** | **§2 non-goal 1(方向を予測しない)の但し書きとして明記**し、**過去リターンで条件付けた場合の感応度**を secondary に加える。**delta-neutral でも entry タイミングは方向性を帯びうる** |
| **Y51** | medium | **comovement の主張(通貨間で乖離が共変動する)が BTC 単独では検定不能** | §3.2 N1 に**「A2 の3主張のうち comovement は検定しない」と明記**する(黙って落とさない) |
| **Y52** | low | **K8 の表現が不正確。** 「最大 3.5」は BTC の**手数料ゼロ**の数値(Table 6: 3.53)であって「資産横断の最大値」ではない | K8 を**逐語に近い形へ訂正** |

### v1.7 追加(**反証パスを生き延びた3件のみ**)

監査66件のうち**敵対的反証を生き延びたのは3件**である(§5A)。その3件を反映する。

| id | 深刻度 | 欠陥 | v1.7 の対応 |
|---|---|---|---|
| **Y54** | **fatal** | **K8 が A2 の変種を取り違えていた。** Arm R は **long-spot-only 変種**に対応するのに、記録したのは **unrestricted 変種**の数値だった。正しくは BTC・高コスト tier で **Sharpe 1.62 / return 5.49% / 稼働率 14.66% / 平均保有 147.92h**。**年別稼働率は 22.28 / 32.15 / 0.02 / 2.85 / 22.18 %** — **減衰年にはほとんど建たない** | **K8 を訂正**(§13.4)。**layer 2(7ヶ月)での想定 trade 数が極端に少なくなることを意味する**。ただし §16.3 の `MIN_TRADES` を**この数値に合わせて下げてはならない**(K8 由来の閾値較正は §13.4 が禁じている)。**layer 2 を過去へ延ばすこともしない**(K12 の使用済み領域に戻り、唯一の非汚染窓を潰すため) |
| **Y53** | medium | **§3.1 R2 と §18.1 が A2 の二部構成の主張を片方だけ取っていた。** A2 は「mean deviation は小さく統計的に非有意(= 良いベンチマーク)」かつ「mean **absolute** deviation は年率 60〜90% と大きい」と述べている | **§18.1 R2 を二部構成に書き直した。** ただし (i) は「棄却されないこと」なので **confirm 条件にしない**(検出力が低いほど自動的に通ってしまう)。confirm は `mean(|ρ|) >= MAD_MIN` で行う |
| **Y55** | low | **Y45 の伝播漏れ。** §4.2 で `rho` を分離したのに、**§6.3 の Arm R 行は `basis_rel` のまま**で、未定義トークン `theoretical_relation_no_cost` を使っていた。**§18.1 の R2/R5 も `basis_rel` を帯と比較していた** | §6.3 を **`rho > ρ_u` / `rho <= 0`** へ、§18.1 を `ρ` 基準へ修正 |

---

## 1. Primary Research Question

> **A2 が Binance の 2020-01-08 〜 2024-03-11 で報告した
> random-maturity arbitrage(BTC spot ロング / perpetual ショート、
> スプレッドが no-arbitrage 境界を超えたら建て、理論関係へ戻ったら解消)は、
> ango が独立に収集したデータの、先行研究のサンプル外の期間でも再現するか。
> そして個人が負担する two-leg・4約定の taker コストと資金拘束の後に経済性が残るか。**

### 1.0 prior art の掃き出しで問いが変わった(**v1.5**)

初版の文献レビューは **2本の直接的な prior art を見落としていた**(→ §13.4 K11 / K12)。

| 研究 | trade | 期間 | コスト | 結果 |
|---|---|---|---|---|
| **A2** He et al. | 閾値 convergence | 2020-01〜2024-03 | **maker** | 高コスト tier で BTC Sharpe 1.80 / 6.38%。ただし **2022年 0.28% / 2023年 1.11%** |
| **K11** Christin et al. | **まさにこの trade** | 2020-08〜2023-06 | **なし(gross)** | gross 14.26%。**epoch 5(2022-05〜)で 1.03% へ崩落** |
| **K12** Borri et al. | **まさにこの trade・同一 venue** | 2020-08〜2025-05 | **なし(gross)** | 全期間 Sharpe 6.45。**2024年に 4.06 へ低下し、2025年に負転** |

**3本すべてが独立に減衰を報告し、最新のものは gross ですら負である。**
さらに **A2 自身の分解では funding ではなく price convergence が支配的**であり、
**funding 成分単独は 2022年 −1.94% / 2023年 −0.94%** だった。

> **したがって問いは「carry は存在するか」ではない。**
> **「carry は死んだのか。ango はその死を、taker コスト込みで独立に確認できるか」**である。
>
> **これは候補の格下げではない。** 誰も taker コストを差し引いた形で検証していない
> (A2 は maker、K11 と K12 は gross)ため、**経済的な問いは本当に開いている**。
> そして ango の文化では **negative result は一級の成果**である(Phase 3・Phase 7 と同じ)。
> 事前予想(§7.4)には **「死んでいる」を既定の予想として登録する。**

```text
Q1 (replication) : 機序は ango のデータにも存在するか      → 符号と形で判定
Q2 (extension)   : 個人スケールのコスト後に経済性が残るか  → 経済指標で判定
```

**Q1 成立 かつ Q2 不成立は正常な結論である**(Phase 7 の
「statistically positive, economically negative」と同型)。そのとき Q2 で Q1 を否定しない。

---

## 2. Non-goals

1. **方向予測をしない。** delta-neutral を維持する。
   **ただし(Y50)**: A2 は「過去リターンの momentum が futures–spot ギャップを
   **R² > 50%** で説明する」と報告している。したがって **`ρ > ρ_u` で建てる規則は、
   過去リターンが高い局面で建てることに近い**。**保有中は delta-neutral でも、
   entry タイミングは方向性を帯びうる。** これを隠さず、過去リターンで条件付けた
   感応度を secondary として報告する(§17.2)。
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

### 3.5 A2 との**意図的な乖離**(**必ず明示して報告する**)

| 項目 | A2 | ango | 理由 |
|---|---|---|---|
| **手数料** | **maker**(「機関は maker で執行するため」)。spot/futures で Low 2.25/0.18 bps 〜 High 6.75/1.44 bps | **taker**(spot 10 / perp 5 bps) | 個人研究者が**確実に約定できる**前提を採る。ROADMAP §4.4 と Non-goals §2-3(maker fill を仮定しない) |
| **Sharpe の年率化** | Lucca-Moench の**稼働期間スケーリング**(BTC は稼働率 20.06%) | **暦時間**で年率化 | 資金が拘束されている時間を分母から外さない |
| **補助データ** | Aave 借入/貸出金利、Glassnode、Kaiko 板 | 代理を使う(→ H11) | 有償・再配布不可 |

> **ango のコスト前提は A2 の High maker tier のおよそ4倍厳しい。**
> したがって **ango の結果が A2 を下回っても、それは「再現失敗」ではない。**
> 乖離の方向と大きさを**事前に宣言**しておくことで、事後に
> 「コストが違ったから」と言い訳することを防ぐ。

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
| **`MARK`** | **mark price klines**(清算判定用。Y38) | `data/futures/um/monthly/markPriceKlines/BTCUSDT/5m/` | 5m(**2026-08-17 到達確認済**) |

### 4.2 observable

| 列 | 定義 | availability |
|---|---|---|
| `spot_close` / `perp_close` | 各 5m バーの close | `close_of_bar` |
| `basis_abs` | `perp_close − spot_close` | `close_of_bar` |
| `basis_rel` | `(perp_close − spot_close) / spot_close`。**A2 の ρ ではない**(下記 Y45) | `close_of_bar` |
| **`rho`(= A2 の ρ)** | **`ρ = κ·(1 − e^{−(f−s)}) − (r − r′)`、`κ = 1095`**(= 年間の8時間区間数)。`f`,`s` は log 価格 | `close_of_bar` |
| **`r`(金利項。H12 で確定)** | **Aave の変動借入 APR(USDT / USDC / DAI)の等加重平均**。**`r′ = 0`**(Arm R は spot をショートしないため)。**signal_time で利用可能な観測のみ**を使う(point-in-time) | **`start_of_bar`** |
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
| **Arm R(replication)** | **`rho > arb_bound_upper(c)`**(= `ρ_u`) | **`rho <= 0`** | **primary。A2 の random-maturity arbitrage(long-spot-only 変種)** |
| Arm B(baseline) | 期間開始で1回 | 期間終了で1回 | always_on carry |
| Arm E(extension) | 固定 horizon の非重複グリッド | entry + h | **記述的 robustness のみ。promotion 対象外**(Y2) |

**A2 の境界式(H11 解決。Table 3 の caption に逐語で記載されている)**:

```text
ρ_l = κ · log(1 − C)
ρ_u = κ · log(1 + C)        C = 往復コスト(spot + futures の合計)、κ = 1095
```

entry は `ρ > ρ_u`(または `ρ < ρ_l`)、exit は **`ρ` が 0 へ戻ったとき**
(= コスト無しの理論関係)。**exit は境界ではなく 0 である**点に注意する。
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

| シナリオ | spot 片道 | perp 片道 | 往復合計 | 位置づけ |
|---|---:|---:|---:|---|
| `maker_low` | 1 bps | 1 bps | 4 bps | **参考のみ**(maker fill を仮定しない) |
| **`base_taker`** | **10 bps** | **5 bps** | **30 bps** | **primary**(Y36 で修正) |
| `stress` | 15 bps | 10 bps | 50 bps | **昇格ゲート**(§14.1 (d)) |
| — | — | — | — | `transfer_cost_bps = 0`(**Binance 内 spot↔futures 振替は無料・ほぼ即時**。仮定を可視化する。Y42) |

**Y36 の修正理由**: v1.3 は spot・perp とも 5bps としていたが、これは**保守的ではない**。
**Binance spot VIP0 taker は 0.10%、USDT-M futures VIP0 taker は 0.05%** である。
往復は **20bps ではなく 30bps**。H6 による確定は**下方修正のみ**であり安全側。
**確定までは仮置きであることを artifact に明記する。**

### 7.3 4約定コストの含意(**結果ではなく算術**)

`base_taker` で往復 **30 bps**(**entry 名目に対する比率。`S_out ≈ S_in` を仮定** — Y43)。
8時間 funding 1回の受取を `f` とすると、コスト償却に必要な決済回数は `n ≈ 0.0030 / f`。

**入力は外部知識ではなく ango 自身の在庫測定を使う**(Y34):
[33ヶ月追試](../findings/2026-08-16-5m-tendencies-33mo-retest.md) の
**平均 +0.69 bps / 8h**(3,012決済)。これを代入すると

```text
n ≈ 0.0030 / 0.000069 ≈ 43 決済 ≈ 14.5 日
```

**非重複グリッドの年率コスト負担**(1 trade あたり1往復):

| horizon | 年間 trade 数 | 年率コスト負担 | +0.69bps/8h の年率収入 7.56% との差 |
|---|---:|---:|---|
| 8h | 1,095 | 328% | 大幅に負 |
| 24h | 365 | 110% | 大幅に負 |
| 72h | 122 | 37% | 負 |
| 168h | 52 | 15.6% | 負 |
| 336h | 26 | 7.8% | ほぼゼロ |
| **720h** | 12 | **3.7%** | **わずかに正** |
| **1440h** | 6 | **1.8%** | 正 |
| **2160h** | 4 | **1.2%** | 正 |

> **帰結(結果ではなく設計算術)**: **経済的に成立しうるのは月単位以上の保有だけ**である。
> したがって §10 の grid に **1440h / 2160h を追加**し、**`always_on` を経済的な主 arm へ昇格**させる。
> 短い horizon が落ちることは**予想として §7.4 に登録**する
> (事後に「最初から見なかった」と言わないため)。
> **この表は funding の平均値を定数と置いた粗い算術であり、損益の予測ではない。**

> **帰結**: 短い保有は原理的に不利である。Arm R は保有期間を内生に決めるので
> この制約を設計に埋め込む必要はないが、**Arm E の短 horizon が落ちることは予想済み**であり、
> 予想を先に書いておく(事後に「最初から見なかった」と言わないため)。

### 7.4 事前予想の登録(**Y33。恒久ルール3 の実装**)

findings の恒久ルール3 は「**事前予想は追試の前に記録し、検定を実行する者には見せない**」
ことを要求する。v1.2 まではこれが実装されていなかった。

- arm ごと・コスト階層ごとに、**符号・おおよその大きさ・昇格の可否**の事前予想を書く。
- 予想は**別ファイル**に置き、その **sha256 のみ**を `experiments/phase8/carry_freeze.json` に
  封入する。**実行器と実行者は本文を読まない。**
- 実行後に予想を開封し、artifact と突き合わせて findings に併記する。

---

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
  取り込んだ `markPriceKlines` を第2の系列として照合に使う(Y38)。
  やむを得ず `perp_close` を代理にする場合は **`ts = s − 5m` のバー**を使う(Y14)。
  「直近バー」と書くと `ts <= s` のバー、すなわち close が `s` より**後**のバーを掴む。
  なお `perp_close` の代理は **`f > 0` のとき正、`f < 0` のとき負に偏る**ため
  **short に有利な方向へバイアスする**。funding 額に対する影響は小さいが、
  **清算判定には使ってはならない**(§11.3)。
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
| **M6b** | **exit 時**にどちらかの脚の価格が無い | **entry を遡って無効化しない。次に両脚が揃うバーへ roll-forward する**(Y7)。**打ち切りは既存 `ExecutionConfig.cancel_after_ms` を再利用する**(Y28。独自規則を作らない) |
| **M7** | **ヘッジは PnL 中立だが証拠金中立ではない**(Y41)。perp の損は先物ウォレットから即時に引かれ、spot の益は現物ウォレットに**未実現 BTC** として残り short を支えない | §11.4 の対象。**累積 funding を維持証拠金計算に含めるかを明示的に凍結する** |
| **M8** | 数量丸めの残差が**保有中ずっと方向エクスポージャとして残る** | `tracking_error` に含めて記録 |
| **M9** | **spot leg は現物なので清算されないが、perp leg だけが清算されうる**。片脚だけ消えた状態が発生する | §11.3 の清算後規則(再ヘッジ or 巻き戻し)を凍結 |

`tracking_error` を毎バー記録し分布を報告する。**閾値による除外はしない。**

---

## 10. Arm E の候補 horizon(**promotion 対象外**)

```text
H = { 8h, 24h, 72h, 168h, 336h, 720h, 1440h, 2160h }      ← 1440h / 2160h を Y36 で追加
```

**`always_on` は §12 の baseline B2 であると同時に、Y36 により経済的な主 arm でもある。**
§7.3 の算術上、コストを最も薄められるのが「建てっぱなし」だからである。
ただし **B2 は §11.4 の証拠金維持規則が無ければ清算されて存在し得ない**(Y35)。

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
資本基準 C(USDT 建て)を凍結し、そこから数量を導く(Y37):
    q_i = C · L / ((L + 1) · S_in,i)
    q_i を perp / spot の lot step へ丸める
    丸め後の名目が MIN_NOTIONAL を割る trade は棄却し、件数を artifact に記録する
    deployed_capital = C                ← 全 trade・全 layer で同一
```

**Y37 の修正理由**: v1.3 は `N0`(定額名目)としており、`q` が資本量に紐づいていなかった。
そのため NARDC が `q` に対して不変になり、**個人スケールを難しくしている当のもの**
(perp の lot step 0.001 BTC、`MIN_NOTIONAL`)が primary endpoint から消えていた。

- `C` と `L` を凍結する(`L` の暫定値 3。H6 で確定)。
- 感応度として `L ∈ {1,2,3,5}` を報告するが **primary は凍結した1つ**。
  `L` は分母に決定論的に入るため、**`NARDC(L)·(1+1/L)` が清算ゼロ時に一定**であることを
  テストで検証する(§21 T16)。

### 11.2 turnover / exposure

`turnover` と **`exposure`(建玉していた時間の割合)を arm ごとに必ず記録する**(Y10)。

### 11.3 清算(**Y16 / Y38 で明確化**)

- **Binance USDT-M の清算は mark price で起きる。** したがって判定は
  **`markPriceKlines` の不利側 intrabar 極値**(short perp なので mark の high)で行う。
  **`perp_close`(last price)で判定してはならない**(Y38)。
  `markPriceKlines` は 2026-08-17 に到達確認済み(§4.1)。
- **これは look-ahead ではない** — 清算は戦略の意思決定ではなく**取引所のイベント**であり、
  不利側極値で評価するのが保守側である。
- 強制決済は **トリガー価格 + スリッページ**より良い価格では約定しない。
  清算後は **凍結した規則で再ヘッジするか巻き戻す**(どちらかを事前に選ぶ)。
- 維持証拠金 tier は H6 の確定後に凍結。清算件数と**清算時損失**を artifact に記録する。

### 11.4 証拠金維持規則(**Y35。B2 が存在するための最低条件**)

初期証拠金 `1/L`・維持証拠金率 `m` のとき、short perp は価格が
`(1 + 1/L)/(1 + m) − 1` 上昇した時点で清算される(**`L=3`・`m=0.4%` で約 +32.8%**)。
layer 2 の BTC はこれを大きく超えて上昇しているため、
**追証を一切しない `always_on` は現実に存在し得ない。**

```text
維持証拠金率が MARGIN_TOPUP_TRIGGER を割ったら、
    予備資金から USDT を追加して MARGIN_TOPUP_TARGET まで戻す
予備資金が尽きたら清算を受け入れる
```

- **最適化しない。** 閾値は凡庸な決め打ちを1組だけ凍結する(恒久ルール6 の精神)。
  動的な collateral 制御は A3 の主題であり**本 Phase の範囲外**(§2-3)。
- **予備資金は `deployed_capital` に含める**(含めなければ資本を過少申告することになる)。
- 追証の回数・総額を artifact に記録する。

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

**baseline ごとの分母(Y44)**: B1(buy_and_hold_spot)は perp leg を持たないので `q·S`、
B0(always_flat)は同一の資本基準 `C`(「何もしない」が 0 または `r_f` になるように)、
B2/B3/B4 は §11.1 の `C`。**同じ分母を機械的に当てはめない。**

**B3 の適用範囲(Y10)**: `exposure < EXPOSURE_GUARD`(凍結値 0.7)の arm に対してのみ
**昇格・NO-GO の判定条件として使う**。exposure がそれ以上の arm では
**B3 は報告するが判定に使わない**(自由度が無く原理的に無情報のため)。
その場合は **B2 との比較が主たる対照**になる。

---

## 13. split と外部知識汚染

### 13.1 3層(**X2 / X3 で境界を修正**)

```text
layer 1  literature_in_sample      2020-01-01 <= ts < 2025-06-01
         = max(A2 2024-03-11, A1 2024-07, K11 2023-06-23, K12 2025-05-31) を月境界へ切り上げ
layer 2  contaminated_confirmation 2025-06-01 <= ts < 2026-01-01        (7 ヶ月のみ)
layer X  phase8_contaminated       2026-01-01 <= ts < 2026-09-01        ← 読まない
layer 3  phase8_prospective_final  ts >= 2026-09-01
```

**H5 は承認された(2026-08-17 決定ログ。制約つき)。**

| 決定 | 実装 |
|---|---|
| `PHASE8_PROSPECTIVE_START = 2026-09-01T00:00:00Z` | `mce.backtest.splits` に**新規定数として追加**(`phase8_layer()` も追加) |
| **`FINAL_OOS_START` を変更も弱化もしない** | **`2026-01-01` のまま。1文字も触っていない**(T35 で機械検査) |
| `2026-01-01` 〜 `2026-08-31` は Phase 8 の汚染域であり、**Phase 8 の結果評価で決して読まない** | `PHASE8_CONTAMINATED_BAND` として定数化。§20 の `phase8_contaminated_rows_read == 0` で機械検査 |

- 既存 split(`research` / `validation` / `final_oos`)の**意味は変わらない**。
  `final_oos` は従来どおり 2026-01-01 以降すべてであり、
  **Phase 8 の汚染域(layer X)はその部分集合**である。
- **layer X は「後で使う」窓ではない。** Phase 8 の結果評価に対して恒久的に閉じている。
  K9(ango 自身が 2026-07 まで funding を測定済み)と文献の 2026 年言及により、
  **この窓は Phase 8 にとって既知**だからである。
- **layer 3 は 2026-09-01 以降であり、本タスクの時点では 1 バーも存在しない。**
  したがって **freeze 時点で layer 3 を読むことは物理的に不可能**である。

**境界は2度動いた。**

| 版 | layer 1/2 境界 | layer 2 の長さ | 理由 |
|---|---|---|---|
| v1 | 2023-11-19 | 26ヶ月 | A1 の cover date から誤って推定 |
| v1.2 | 2024-08-01 | 17ヶ月 | X2(A1 の実サンプルは 2024-07 まで) |
| **v1.5** | **2025-06-01** | **7ヶ月** | **K12(Borri et al.)が同一 trade・同一 venue を 2025-05-31 まで使用済み** |

> **layer 2 が7ヶ月しかないことは、この候補の最大の弱点である。**
> §16.3 の標本下限を暦の算術で先に評価すると、**長い horizon はほぼ確実に
> `insufficient_sample` になる**。**これは実行前に分かることであり、
> 実行してから「効果が無かった」と書いてはならない。**

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
| **K8** | **⚠ Y54 で訂正。変種を取り違えていた。** A2 の **unrestricted 変種**(両方向)は Sharpe 1.8 / 稼働率 20.06% / 平均保有 134.94h。**しかし Arm R が対応するのは long-spot-only 変種**であり、その BTC・高コスト tier の数値は **Sharpe 1.62 / return 5.49% / 稼働率 14.66% / 平均保有 147.92h**。**年別の稼働率は 22.28 / 32.15 / 0.02 / 2.85 / 22.18 %**(平均保有 111.94 / 198.36 / 1.00 / 124.00 / 185.50 h)。**maker 手数料**・**稼働期間スケーリング**での年率化。分解では total 13.70% = price convergence 8.64% + funding 5.06%、**funding 成分は 2022年 −1.94% / 2023年 −0.94%** | 2020-01-08〜2024-03-11 | `VERIFIED-FULL` |
| **K13** | **A2 は post-2022 について「ρ の7日移動平均はほとんどの期間 −50% 前後に留まり、符号は負の領域で安定するように見える」と述べている** | 2022〜2024-03 | `VERIFIED-FULL` |
| K7 | (**未検証**)Hyperliquid 単一 venue carry 17.9%(2024)/ 3.6%(2025) | 2024–2026 | `UNVERIFIED` |
| **K9** | **ango 自身の在庫測定**(Y34): Binance BTCUSDT perp funding、**3,012決済**、**平均 +0.69 bps / 8h**、**85.4% が正**、2024-03 は年率 **+37%**、**2026-02〜04 は3ヶ月連続で負転** | **2023-11 〜 2026-07** | **在庫**([33ヶ月追試](../findings/2026-08-16-5m-tendencies-33mo-retest.md)) |

**K8 は文献由来として最も汚染的な数値である。** 報告された risk-adjusted の大きさを
実行前に知ってしまった。**いかなる閾値の設定にも使ってはならない。**

> **K9 は K8 より深刻である。**
> (i) **数値が精密**(3,012決済の実測)、(ii) **既にコミット済み**で取り消せない、
> (iii) **既存封印域(2026-01〜)の7ヶ月を含む**。
> すなわち **ango は封印域の funding 水準を既に部分的に知っている。**
>
> **帰結**: `phase8_prospective_final_start` は **2026-08 より後**でなければならない。
> 選定メモ §8 の案(2026-09-01)はこの条件を満たすが、
> **その根拠は「文献を読んだから」ではなく「自分で測ってしまったから」である。**
> §7.3 の設計算術も、外部の K7 ではなく**在庫の K9 を入力に使う**
> (どうせ知っているものを、知らないふりで外部値に置き換えない)。

---

## 14. 昇格規則

### 14.1 layer 2 → layer 3

```text
Arm R を、事前登録した各コスト階層 c について評価し、以下を全て満たすものだけ昇格:
  (a) 主統計量 mean(r_i) > r_f                    (primary cost scenario。Y39)
      r_f = 決定時点で観測可能な無リスク金利の代理(phase8_prereg.py に凍結)
  (b) B2(always_on)を上回る
  (c) exposure < EXPOSURE_GUARD の場合のみ: B3 の randomization 帰無を棄却(§15.0)
  (d) stress シナリオでも mean(r_i) > 0            ← Y17 で (a) と独立にした
  (e) n_trades >= MIN_TRADES['layer2']             ← Y4
  (f) 多重比較補正後に有意(§15)
  (g) funding_last_settled をさらに 1 決済間隔(8h)遅らせても mean(r_i) > 0   ← Y30

昇格 0 件 → layer 3 を開かずに negative result で閉じる(§19 F1)
```

**(d) の変更理由(Y17)**: v1 の「break-even ≥ 5bps」は primary が `base_taker`(5bps)
であるため **(a) と代数的に同値**で、独立な条件として機能していなかった。
`stress`(10bps)での正値要求に置き換えることで **2倍のマージン**を要求する。

**(g) の追加理由(Y30)**: Phase 7 の事前登録 §17-6 は
「X 列をさらに1バー遅らせても ΔR² > 0」を GO 条件にしていた。
funding の**公開遅延は実測していない**ので、同じ発想の頑健性条件を置く。
+2 決済間隔は感応度として報告する(判定には使わない)。

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

> **Y26**: さらに、**その「実装済みの手法」は再利用できない。**
> Phase 7 の `_bootstrap_ci` は private・日単位のみ・ΔR² 専用にハードコードされている。
> **本 Phase の bootstrap は新規コードである。** reps と layer 別 seed を
> `phase8_prereg.py` へ凍結する。

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

### 16.5 窓端と封印境界の規則(**Y25。Phase 7 prereg §7 に相当するものが v1.2 に無かった**)

```text
ある layer において entry が admissible なのは、次を全て満たすときに限る:
  (1) entry_fill_time      が その layer の内側
  (2) exit_fill_time       が その layer の内側
  (3) 区間内の全ての funding 決済 s が その layer の内側
  (4) 清算判定に必要な全バーが その layer の内側
```

- **絶対規則**: いかなる layer の評価も **`ts >= 2026-01-01` のバーを読まない**
  (H5 が承認され layer 3 が有効化されるまで)。
- 条件を満たさず除外した entry の**件数と理由を artifact に記録する**(§20)。
- **この規則が無いと、h=720h の entry が 2025年12月に建った時点で
  exit が封印域を読む。** v1.2 はこれを見落としていた。

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

`break_even_cost_bps` / trade 単位 Sharpe / max drawdown / 最大清算距離 /
turnover / **exposure** / `funding_capture` / `tracking_error` 分布 / B1 との相関。

**`break_even_cost_bps` の定義(Y31。4約定なので「片道」は曖昧)**:

```text
break_even_cost_bps = (コスト前 PnL(funding 込・清算損失除く)) / Σ(4約定の名目) × 1e4
                    = 1約定あたりの bps
```

既存 `costs.break_even_cost_bps` は単一銘柄の turnover 換算であり**そのままでは使えない**。
この定義なら §7.2 の脚別 taker 率と直接比較できる。

### 17.4 テール risk(**Y40。事前登録 secondary**)

**この戦略の失敗様式を取り違えてはならない。**
§6.2 の恒等式 `PnL = q(D_in − D_out) + Funding` より、
**暴落では perp が相対的に安くなり `D_out` が下がるので carry は儲かる。**
危険なのは **melt-up**(premium が吹き上がり、funding が跳ね、short が清算される)である。

したがって以下を**事前登録した secondary** として必ず報告する。

| 指標 | 定義 |
|---|---|
| 清算確率 | horizon × `L` ごと |
| 清算時損失 | 清算手数料 + 再ヘッジ / 巻き戻しコストを含む |
| 追証の回数・総額 | §11.4 |
| **melt-up ストレス** | **決め打ちの**シナリオ(価格 +X% かつ premium +Y bps を Z 時間で)。**結果を見てから作らない** |

「清算までの距離」と max drawdown は**テール指標ではない**ので、これらの代わりにしない。

### 17.3 primary にしないもの

方向正解率・hit rate / gross return / 単年の最良値。

---

## 18. GO / NO-GO

### 18.1 layer 1(replication gate。**Y18 で数値化**)

**layer 1 のデータを見る前に `phase8_prereg.py` へ数値を凍結する。**
凡庸で標準的な定義を決め打ちし、駄目なら駄目と報告する(恒久ルール6 の精神)。

```text
R1 confirmed : corr( f(s), 直前8時間の basis_rel 平均 ) >= RHO_MIN
               ※ A2 §4.2 自身が「funding はスプレッドを完全には追随しない」と述べているため
                  RHO_MIN は緩い値にする(Y48)
R2 confirmed : A2 の主張は二部構成であり、片方だけを取ってはならない(Y53)
               (i) mean(rho) は 0 から大きく離れない(= 良いベンチマークである)
               (ii) mean(|rho|) は大きい(A2 は年率 60〜90% と報告)
               → 実装: mean(|rho|) >= MAD_MIN を confirm 条件にする。
                  (i) は「棄却されないこと」なので confirm 条件にしない
                  (検出力が低いほど自動的に通ってしまうため)
R5 confirmed : |rho| の年次平均が非増加、または回帰の時間トレンド係数 < 0
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
                      かつ funding を +1 決済間隔 遅らせても > 0        ← Y30
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
protocol, prereg_sha256, prereg_module_sha256, prior_register_sha256, source_commit, uv_lock_sha256
manifests(spot / perp / funding / index / premium), source_digest
layer, layer_bounds(assert 済み)
rows_outside_declared_layer_bounds == 0    ← Y21(全 layer)
existing_final_oos_rows_read               ← Y21(H5 承認前は 0 でなければならない)
window_edge_exclusions                      ← Y25(件数と理由)
funding_interval_hours_distribution        ← X5 の検算
funding_lag_robustness: { plus_1_interval, plus_2_intervals }   ← Y30
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
| **`docs/data_contract.md`** | **§6 の重複排除キーへ `market_type` を追加(Y23)。§8 に Binance funding の availability 宣言を追加し「代理 funding は執行 PnL に使わない」を Phase 8 について改訂(Y27)** | **変更(コードと同一コミット。契約冒頭の要求)** |
| `src/mce/binance_vision.py` | spot / fundingRate / indexPriceKlines のパス追加。**ledger を dataset 単位に分離するか digest 範囲を限定し、Phase 7 の `source_digest` を変えない**(Y24) | 変更 |
| `src/mce/backtest/data.py` | **layer 1 用 loader の追加**(`RESEARCH_START` より古い窓を、迂回ではなく契約の拡張として扱う。Y22) | 変更 |
| `src/mce/features.py` | **carry 列を `AVAILABILITY` へ登録**(別名前空間を作らない。Y22) | 変更 |
| `src/mce/binance_rest.py` | 公式 REST の funding(`markPrice` 付き)取得 | **新規**(X6) |
| `src/mce/normalize_binance.py` | 上記の正規化。**per-target cutoff**(Y13) | 変更(**既存 Phase 7 経路の既定値は不変**) |
| `src/mce/features_carry.py` | `basis_*` / `funding_last_settled` / `arb_bound_*` + availability 宣言 | 新規 |
| `src/mce/carry_quality.py` | 品質ゲート(グリッド・重複・欠測・spot–perp 整合・封印) | 新規 |
| `src/mce/backtest/two_leg.py` | two-leg 執行器(4約定・funding・清算・mismatch) | **新規**(既存 engine を壊さない) |
| `src/mce/backtest/costs.py` | `TwoLegCostConfig` 追加。**docstring の「funding は PnL 外(Q4 の ADR)」を Phase 8 の two-leg carry については supersede する旨を同一コミットで更新**(Y32) | 変更(既存 `CostConfig` の挙動は不変) |
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
| **T22** | **Phase 7 の3 `source_digest` と件数(klines 72 / premium 72 / metrics 1,948)が変わらない** | §21.1 / Y24 |
| **T23** | **carry observable に ±inf が無い**(有限 or null のみ) | J7 / Y29 |
| **T24** | **除外期間が artifact に明細として残る**(件数だけでなく理由つき) | §20 / Y29 |
| **T25** | **spot と perp が重複排除で衝突しない**(`market_type` がキーに入っている) | Y23 |
| **T26** | **窓端規則**: exit や決済が layer 外へ出る entry が除外される | §16.5 / Y25 |
| **T27** | **`existing_final_oos_rows_read == 0`**(H5 承認前) | §20 / Y21 |
| **T28** | **funding を +1 決済間隔 遅らせた系列が計算され、判定に使われる** | §14.1 (g) / Y30 |
| **T29** | **事前予想レジスタの sha256 が freeze 記録に封入されている** | §7.4 / Y33 |
| **T30** | **清算判定が mark price の不利側極値で行われる**(`perp_close` を使わない) | §11.3 / Y38 |
| **T31** | **lot step 丸めと `MIN_NOTIONAL` 棄却が効いている**(件数が記録される) | §11.1 / Y37 |
| **T32** | **証拠金維持規則が発火し、予備資金枯渇時に清算する** | §11.4 / Y35 |
| **T33** | **昇格条件 (a) が `> r_f` であって `> 0` ではない** | §14.1 / Y39 |
| **T34** | **spot 片道が perp 片道より高い**(`base_taker` の脚別レート) | §7.2 / Y36 |

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
- [ ] §16.5 の窓端規則(T26)
- [ ] §20 の artifact 全フィールド(T27 を含む)
- [ ] `data_contract` §6 / §8 の改訂(Y23 / Y27)
- [ ] Phase 7 `source_digest` の回帰テスト(T22)
- [ ] §7.4 の事前予想レジスタ(T29)

---

## 22. freeze blocker(v1.2 時点)

| # | 事項 | 状態 |
|---|---|---|
| ~~H4~~ | Binance `calc_time` の semantics | **✅ 解決**(= 決済時刻。X4) |
| ~~H2~~ | A1 のサンプル期間 | **✅ 解決**(2019-03〜2024-07。dated futures。X1/X2) |
| **H5** | layer 3 を設けるか / firewall 改訂(freeze v2)の可否 | **未解決・fatal** |
| **H6** | spot leg の執行前提・fee 表・margin tier・`N0`・`L` | **未解決・高** |
| ~~H11~~ | A2 の `arb_bound(c)` の実装式 | **✅ 解決**(Table 3 caption: `ρ_l = κ log(1−C)` / `ρ_u = κ log(1+C)`。Y47) |
| ~~H12~~ | ρ の金利項 `r` | **✅ 解決**(決定ログ 2026-08-17): **Aave 変動借入 APR(USDT/USDC/DAI)の等加重平均、`r′ = 0`、point-in-time**。**Kenneth-French daily RF は事前登録した感応度であって primary ではない** |
| ~~H5~~ | layer 3 と firewall | **✅ 承認(制約つき)**。§13.1 |
| ~~H6~~ | 執行前提・fee・margin tier | **✅ 大部分解決**。残るのは taker commission のみ(下記) |
| **H13** | **BTCUSDT USD-M の taker commission の権威ある値** | **未解決・freeze の前提として要求されたが、本環境では取得不能**(§22.1) |
| H10 | 公式 REST `markPrice` を primary にするか | 未解決・中(**推奨: する**) |
| H9 | BTC 単独か ETH を足すか | 未解決・中 |
| H7 | ToS 上の利用可否 | 継続して要確認 |
| H8 | *Alpha Illusion* P1–P6 を報告規準として採用するか | 未解決・低(**推奨: 採用**) |

### 22.1 H13 — 実行できなかった凍結前要求(**正直な申告**)

決定ログは「freeze の前に `GET /fapi/v1/commissionRate`(read-only USER_DATA)で
BTCUSDT USD-M の taker commission を確定し、生レスポンス・取得時刻 UTC・digest を
記録せよ」と指示した。**この指示は本環境では実行できなかった。**

| 項目 | 実測(2026-08-17) |
|---|---|
| エンドポイント | `https://www.binance.com/fapi/v1/commissionRate?symbol=BTCUSDT&timestamp=…` |
| HTTP | **401** |
| 生レスポンス | `{"code":-2014,"msg":"API-key format invalid."}` |
| 原因 | `commissionRate` は **USER_DATA** であり、**API key と HMAC-SHA256 署名が必須**。本環境に Binance の資格情報は存在しない(環境変数・設定ファイルとも無し) |
| 発注 | **していない**(指示どおり) |

**この1個の定数を推測で埋めることはしない。** 代わりに:

1. `phase8_prereg.py` の `PERP_TAKER_BPS` を **`5.0`(FAQ の worked example 由来・`probable`)**
   として置き、**`COMMISSION_RATE_STATUS = "pending_authenticated_read"` を併記**する。
2. **`spot` 側は確定値**(公式 fee ページの live 読み取りで VIP-0 taker `0.100%`)。
3. **freeze は実行する。** 理由: これは**設計の未確定ではなく、単一パラメータの実測待ち**であり、
   確定しても**設計は1文字も変わらない**。凍結対象は設計である。
4. **ただし実験の実行はブロックする**: `COMMISSION_RATE_STATUS` が `resolved` に
   なるまで experiment runner を起動しない(T36 で機械強制)。
5. 実測できたときは **生レスポンス・取得時刻・digest を `carry_freeze.json` の
   `commission_rate` ブロックへ追記**する(**設計の再凍結には当たらない**)。

**もしこの扱いが決定ログの意図と異なるなら、freeze を巻き戻して再凍結する。**
現時点の判断は「**設計を凍結し、実測待ちの1定数だけを明示的に未確定として持つ**」である。

---

## 23. このプロトコルが言っていないこと

1. **carry が儲かるとは言っていない。** 損益を1つも計算していない。
2. **A2 が正しいとも間違っているとも言っていない。** 独立に確認する対象である。
3. **layer 2 で正の carry が出ても発見ではない**(K1/K2/K8 で符号も大きさも既知)。
4. **本文書は事前登録ではない。** §22 が解決するまで凍結しない。
5. **§7.3 の算術は結果ではない。** 外部知識由来の桁を代入した設計判断である。
6. **v1 の設計は「動かしてみて駄目だった」のではない。** 一度も実行せずに、
   全文取得と敵対監査だけで訂正した。**実行前に直せたことがこの改訂の要点である。**

---

## 24. v1.8.1 修正条項(**パラメータ確定のみ。仮説は変更していない**)

- 承認: 2026-08-17 決定ログ
- 契機: `two_leg.py` の実装が露出させた**凍結仕様の穴 3件**と、**清算会計の誤り 1件**
  ([two_leg_conformance_notes_v1](two_leg_conformance_notes_v1.md))
- **変更していないもの**: Primary Research Question / Non-goals / 情報集合 /
  arm 定義 / horizon 集合 / コスト階層 / family / 多重比較補正 / layer 境界 /
  昇格規則 / GO-NO-GO / negative result 条件 / 封印
- v1.8 の凍結記録は `experiments/phase8/carry_freeze.json` として**不変のまま残す**。
  v1.8.1 の凍結記録は `experiments/phase8/carry_freeze_v1_8_1.json` に**新規作成**する。

### 24.1 G1 — 予備資金(§11.1 × §11.4 の非両立を解消)

```text
MARGIN_RESERVE_USDT   = 2000.0
POSITION_CAPITAL_USDT = C − R = 8000.0
サイジング             q = (C − R)·L / ((L + 1)·S_in)
deployed_capital      = C = 10000.0   (予備資金を含む。§11.4)
```

**R の導出**(恣意的な決め打ちではない): 予備資金を
**初期証拠金1トランシェ分**と定義する。

```text
R = (C − R)/(L + 1)   … 右辺は position capital に対する初期証拠金
⇒ R(L + 2) = C  ⇒  R = C/(L + 2) = 10000/5 = 2000
```

内訳の検算(primary `C=10000, L=3`):

| 項目 | 金額 |
|---|---:|
| spot 名目 `q·S_in` | 6000 |
| 初期証拠金 `q·P_in/L` | 2000 |
| 予備資金 `R` | 2000 |
| **合計 = `deployed_capital`** | **10000** |

- **`R` は leverage 感応度をまたいで 2000 に固定する。** `L` を変えても
  `POSITION_CAPITAL = 8000` は動かない。
- **T16 の修正**: 丸め前の厳密不変量の基準を **`C` から `POSITION_CAPITAL_USDT` へ**変える。

  ```text
  q_raw · S_in · (1 + 1/L) = C − R = 8000   （全ての L で厳密に成立）
  ```

  丸め後は lot 量子化の分だけずれる(v1.8 の申告どおり)。

### 24.2 G2 — funding と証拠金(§9 M7 の未凍結を解消)

```text
FUNDING_COUNTS_TOWARD_MARGIN = True
```

**正負いずれの funding も先物ウォレット残高を動かす。** 受取だけを反映して
支払を反映しない、という非対称な扱いはしない。

### 24.3 G3 — 清算後の規則(§11.3 の未凍結を解消)

```text
POST_LIQUIDATION_RULE = "unwind"
```

- **再ヘッジは実装しない。**
- 強制清算は **perp 脚を実際の清算約定で終了させる**。
- 残った **spot 脚は、清算バーの後で最初に因果的に執行可能な spot open** で解消する。
  必要なら §9 M6b の roll-forward 意味論をそのまま使う。
- **清算から spot 解消までの naked spot エクスポージャを `tracking_error` に記録する。**

### 24.4 G4 — イベント順序(**v1.8 の記述を訂正**)

**v1.8 の実装と conformance note は「清算判定 → 追証 → funding」と書いていたが、
これは誤りであり、正しい順序を以下に凍結する。**

```text
funding 決済境界において:
  1. 適格な funding を先物ウォレットへ適用する
  2. 不利側 mark 経路で証拠金・追証を評価する
  3. TOPUP_TRIGGER は維持証拠金より上にあるので、清算より先に処理する
  4. 追証の後、なお維持証拠金以下であれば清算する
```

`EVENT_ORDER = "funding_then_margin_then_topup_then_liquidation"`

**根拠**: `TOPUP_TRIGGER`(0.010)は `MAINT_MARGIN_RATE`(0.004)より**上**にある。
したがって追証は清算より必ず先に到達する事象であり、順序として自然である。
funding を先に適用するのは、決済が起きた時点で残高が実際に動くからである。

### 24.5 G5 — 清算会計の修正(**v1.8 実装の誤りの是正**)

v1.8 の実装には次の欠陥があった。**runner 作業の前に直す。**

| # | 欠陥 | 修正 |
|---|---|---|
| a | 強制清算の**後**に、予定していた perp exit 価格を使っていた | **使わない。** perp 脚は清算約定で終了する |
| b | 清算後にも通常の `cost_perp_out` を計上していた | **計上しない。** 強制決済に通常の taker 手数料は掛からない |
| c | `liquidation_loss` が清算約定の PnL と**二重計上**しうる | **価格損失は `q(P_in − P_liq)` に一度だけ現れる。** `liquidation_loss` は **清算清算手数料(clearance fee)だけ**を表す |
| d | 清算時刻・清算約定・spot 解消時刻/価格が記録されていなかった | **フィールドを追加する** |

**追加フィールド**: `liquidation_ts` / `liquidation_fill` /
`spot_unwind_ts` / `spot_unwind_fill` / `liquidation_fee_usdt`。

**恒等式の一般化**: 脚が別時刻で終了しても §6.2 の恒等式は保たれる。

```text
D_out := P_exit_actual − S_exit_actual
         （通常時は同一バーの open、清算時は P_liq と S_unwind）
PnL_gross = q(D_in − D_out) + Funding      … 清算経路でも成立する
```

**清算経路版の恒等式テストを追加する**(§21.2 T35)。

### 24.6 H14 — 清算コストの意味論(**未解決。実験をブロックする**)

`liquidation_slippage_bps = 0.0` は v1.8 が凍結した値ではない。**ゼロを黙って維持しない。**

**確認できたこと(2026-08-17 実測)**:

| 事項 | 出所 | 結果 |
|---|---|---|
| Liquidation Clearance Fee が**存在する** | 公式 FAQ(取得済) | 「維持のために供された資産の一部が控除され Liquidation Clearance Fee として Binance へ支払われる」「適用される Liquidation Clearance Fee rate と建玉の名目価値に基づいて計算される」 |
| 清算のトリガ条件 | 同上 | `Collateral = Initial Collateral + Realized PnL + Unrealized PnL < Maintenance Margin`(**本プロトコルの判定と一致**) |
| 約定の性質 | 同上 | Smart Liquidation。IOC で市場へ流し、未約定分は Bankrupt Position として Insurance Fund が処理 → **約定は成行であり、short にとってトリガー価格以上になりうる** |
| **fee rate の数値** | risk bracket API / trading-rules ページ | **取得できない。** brackets payload に fee 項目は無く(`fee|liq|clear|penalt` に一致するキー 0件)、trading-rules の表は JS 描画で非 JS 取得では "No Data"、`leverageBracket` は 401 |

```text
LIQUIDATION_CLEARANCE_FEE_RATE = None
LIQUIDATION_FEE_STATUS         = "pending_authoritative_read"
```

**H13 と同じ扱いとする**: 値が `resolved` になるまで **experiment runner を起動しない**。
`two_leg` はこの2つを**必須引数**として要求し、既定値を持たない。

### 24.7 v1.8.1 で追加するテスト

| # | テスト | 対応 |
|---|---|---|
| **T35** | **清算経路でも脚形と basis 形の PnL が一致する** | §24.5 |
| **T36** | **清算後に予定 perp exit 価格を使わない / `cost_perp_out` を計上しない** | §24.5 a,b |
| **T37** | **`liquidation_loss` が価格 PnL を二重計上しない** | §24.5 c |
| **T38** | **spot 解消が清算バーより後の最初の因果的 open で行われる** | §24.3 |
| **T39** | **naked spot 期間が `tracking_error` に現れる** | §24.3 |
| **T40** | **イベント順序が funding → margin → topup → liquidation である** | §24.4 |
| **T41** | **H14 未解決なら実験がブロックされる** | §24.6 |

---

## 25. 仕様の優先順位(specification precedence)

**この節は本文書の読み方を定める。以降の改訂もこの規則に従う。**

```text
同一フィールドについて複数の記述がある場合、
**後の凍結改訂節が先の記述を supersede する。**
先行する矛盾した記述は、削除せず**歴史的な監査証跡として残す**。
```

適用規則:

1. **番号の大きい改訂節が勝つ。** §24(v1.8.1)は §1–§23(v1.8)の同一フィールドを上書きする。
   §26 以降も同様に、それ以前を上書きする。
2. **上書きされた記述は消さない。** 「なぜそう決めたか」「何が誤っていたか」は
   研究記録の一部である(Phase 3 / Phase 7 と同じ方針)。
3. **上書きの有無が曖昧な場合は、改訂節に明示的な訂正表を置く**
   (§24.4 が §4 の順序記述を訂正したのがその例)。
4. **`src/mce/phase8_prereg.py` は常に最新の凍結値のみを持つ。**
   歴史的な値はモジュールに残さず、本文書と凍結記録に残す。
5. 本規則は**遡及して適用する**。すなわち §24 は本節より前に書かれているが、
   §1–§23 に対する優先権を持つ。

**既知の supersede 一覧**(網羅ではなく、混乱しやすいもの):

| 上書きした節 | 上書きされた記述 | 内容 |
|---|---|---|
| §24.1 | §11.1 の `q = C·L/((L+1)·S_in)` / §6.1 の `N0` | `q = (C−R)·L/((L+1)·S_in)`、`R = 2000` |
| §24.1 | §11.1 の T16 基準 `C` | 基準は `POSITION_CAPITAL_USDT` |
| §24.2 | §9 M7 の「明示的に凍結する」(未凍結だった) | `True` |
| §24.3 | §11.3 の「どちらかを事前に選ぶ」(未凍結だった) | `"unwind"` |
| **§24.4** | **§8 / §11 周辺の順序記述(「清算 → 追証 → funding」)** | **funding → margin → topup → liquidation** |
| §24.5 | §11.3 の清算損失の扱い | 価格損失は一度だけ。`liquidation_fee_usdt` は clearance fee のみ |
| §24.6 | `liquidation_slippage_bps = 0.0`(暗黙の既定) | **未凍結。H14 として実験をブロック** |
| **§26** | **§4.2 の `r` の記述(Aave の版・network・market を特定していない)** | **H15 として未解決登録** |
| **§27** | **§26 の「未解決」と、そこで提案した version-current proxy** | **部分 proxy を採用。V4 へは移行しない** |
| **§28** | **`MAX_STALE_SECONDS = 9h`(funding 用)を rho にも適用していた記述** | **系列ごとに分離。rho は 24h** |

---

## 26. H15 — Aave 金利市場の同定(**未解決。実験をブロックする**)

- 調査記録: [h15_aave_source_investigation_v1](h15_aave_source_investigation_v1.md)
- 契機: §4.2 / H12 は `aave_variable_borrow_apr` としか定めておらず、
  **Aave の version・network・market・データ提供元が一意に定まらない**

### 26.1 A2 の記述水準(全文走査の結果)

**A2 は Aave の版・ネットワーク・market・提供元を一切書いていない。**
言及は7箇所のみで、確定できるのは次だけである。

- 対象は **USDT / USDC / DAI の3ステーブルコイン**、**等加重平均**
- **日次**
- perp > spot の側(= Arm R)では **borrowing rate** を使う(supply rate ではない)
- **金利データの開始日は 2020-01-08** であり、**A2 のサンプル開始日はこれに律速されている**

### 26.2 唯一の強い手がかりと、その限界

`2020-01-08` は **Aave V1 の Ethereum mainnet ローンチ日**と一致する(公式 changelog)。
しかし A2 のサンプル(〜2024-03-11)は **V1(2020-01-08)→ V2(2020-12-03)→
V3 Core(2023-01-27)** の3世代をまたぎ、**接合方法は記載されていない**。

### 26.3 ango の層との非対称(**新規に判明した設計問題**)

| 層 | 期間 | 現存する Aave 世代 |
|---|---|---|
| layer 1 | 2020-01 〜 2025-06 | V1 → V2 → V3 Core |
| layer 2 | 2025-06 〜 2026-01 | V3 |
| **layer 3** | **2026-09 〜** | **V4**(2026-03-30 以降) |

**layer 3 は A2 が一度も見ていない世代の上で評価されることになる。**

### 26.4 凍結する状態

```text
RATE_MARKET_IDENTITY_STATUS = "unresolved_source_fidelity_limitation"
```

- **H13 / H14 と同じ扱い**: 解決するまで experiment runner を起動しない。
- **Aave の履歴 adapter を実装しない。**
- **純粋な数学層(ρ・境界・Arm R シグナル)は実装してよい。**
  `r` を**明示的な入力**にすることで、ソース同定と独立に検証できるからである。
- 提案 proxy は調査記録 §5 に **1つだけ**記載した。**採否は人間が決める。**
  **本改訂は proxy を採用していない。**

---

## 27. H15 解決 — Aave 金利市場を**部分 proxy として**採用する

- 承認: 2026-08-17 決定ログ
- 調査記録: [h15_aave_source_investigation_v1](h15_aave_source_investigation_v1.md)
- **§25 の優先順位規則により、本節は §4.2 / §26 の `r` に関する記述を supersede する。**

### 27.1 位置づけ(**厳密再現ではない**)

```text
RATE_SOURCE_FIDELITY = "partial_proxy_not_exact_A2"
```

> **A2 の厳密な再構成であるとは主張しない。**
> A2 は version / network / market / 提供元を書いていない(§26)。
> 以下は **部分的な source fidelity を持つ proxy** であり、そう明記して報告する。

**§26 で提案した "canonical-Ethereum, version-current" をそのままは採用しない。**
V4 の扱いが異なる(下記 27.2)。

### 27.2 凍結する市場と接合

| 期間 | 適用する市場 |
|---|---|
| 2020-01-08 〜 2020-12-03 | **Aave V1**(Ethereum mainnet) |
| 2020-12-03 〜 2023-01-27 | **Aave V2**(Ethereum mainnet) |
| 2023-01-27 〜 **以降ずっと** | **Aave V3 Core**(Ethereum mainnet) |

- **network は Ethereum mainnet のみ。** L2 を含めない。
- **V4 へは移行しない。** 理由: **V4 は担保依存のリスクプレミアムによって
  借入金利の構造そのものを変える**一方、**V3 は引き続き利用可能な market として
  存在する**。したがって Phase 8 は V3 Core に留まる方が系列として一貫する。
  (V4 の Ethereum ローンチ 2026-03-30 は参照として記録するが**使わない**。)
- **接合日は provenance に必ず記録する**(§27.6)。平滑化・補間・遡及再計算をしない。

> **帰結**: §26.3 で挙げた「layer 3 が V4 世代になる」問題は**解消した**。
> layer 1 は V1→V2→V3、layer 2 と layer 3 はいずれも **V3 Core** である。

### 27.3 系列の特定化と basket

```text
rate   : variable borrow APR          ← 明示的な proxy specialization
assets : USDT / USDC / DAI の等加重平均
```

- `variable` は **A2 に根拠のある値ではなく、明示的に選んだ proxy 特定化**である
  (V1/V2 には stable borrow rate もあった。§26.1)。**そう明記して報告する。**
- **3成分すべてを要求する。** どれか1つでも欠けたらその日は **r なし**とする。
  **黙って basket 構成を変えない**(2成分平均への退化を禁じる)。

### 27.4 観測の時刻規約

```text
毎日 00:00 UTC の point-in-time スナップショット
その時刻**以前**に確定したチェーン状態のみを使う
補間・平滑化をしない(RATE_INTERPOLATION = "none")
```

**スナップショットの「年齢」は、その日 00:00 UTC のスナップショット時刻から測る。**
基礎となる reserve 更新イベントの時刻からではない(§28 と対で読むこと)。

### 27.5 感応度は維持する

**Kenneth-French daily RF は事前登録した感応度として維持する**(§4.2 のまま)。
primary の置換ではない。

### 27.6 provenance の要求

artifact に次を必ず残す: 使用した版と接合日、日次スナップショット時刻、
3成分それぞれの生値、欠測日の一覧(補完していないことの証跡)、
`RATE_SOURCE_FIDELITY` の値。

---

## 28. H16 — 陳腐化ガードを系列ごとに分離する(**v1.8.2 の実装の誤りを是正**)

**v1.8.2 までは単一の `MAX_STALE_SECONDS = 9h` しか無く、
`point_in_time_rate()` がそれを既定にしていた。これは funding 系列の定数であり、
A2 の Aave 金利入力は日次なので誤りである**(9h では同じ暦日の午前中に陳腐化する)。

```text
FUNDING_MAX_STALE_SECONDS = 9  * 3600      … funding(8h 間隔 + 余裕)
RATE_MAX_STALE_SECONDS    = 24 * 3600      … Aave 日次スナップショット
```

- `point_in_time_rate()` は **`RATE_MAX_STALE_SECONDS` を使う**。
- **`MAX_STALE_SECONDS` は廃止する**(§25 規則4: モジュールは最新の凍結値のみを持つ)。
- **funding の 9h が ρ に影響してはならない。** テストで固定する。

---

## 29. source-sensitivity disposition

```text
if sign(Aave proxy での最終的な経済判定) != sign(Kenneth-French RF での判定):
        → source_sensitive と分類する。**GO とはしない。**
```

**根拠**: 結論が金利ソースの選択で反転するなら、それは機序についての結論ではなく
**ソース選択についての結論**である。§27 が部分 proxy であることを認めた以上、
この分類は必須である。

`source_sensitive` は NO-GO でもない。**「この設計では判定できない」**という第3の帰結であり、
§19 の negative result 条件とは別に記録する。

### 29.1 v1.8.3 で追加するテスト

| # | テスト |
|---|---|
| **T42** | 日次レートが**同じ UTC 日のあいだ有効**であり続ける |
| **T43** | 翌日のスナップショットが欠けたら、凍結した rate horizon を過ぎて陳腐化する |
| **T44** | **funding の 9h 定数が ρ に影響しない** |
| **T45** | 3ステーブルコインすべてが必要(1つ欠けたら r なし) |
| **T46** | 欠測日をまたいで**補間しない** |
| **T47** | V4 へ移行しない(接合が V3 Core で止まる) |
| **T48** | `source_sensitive` の分類規則が凍結されている |

---

## 30. v1.8.4 修正条項(**入力データ源の確定。仮説は変更していない**)

**適用範囲**: Aave 金利入力の (i) 源と経路の定義、(ii) 完全性の定義、(iii) 有効値の扱い。
**変更していないもの**: 仮説、family、layer 境界、昇格規則、コスト、証拠金規則、
`FINAL_OOS_START`、封印、H13 / H14 のブロッカー状態。

§25 の優先順位により、**本節は同一フィールドについて §27 の記述を supersede する**。
§27 の先行記述は削除せず歴史的監査証跡として残す。

根拠となる実測: [aave_source_probe_findings_v1](aave_source_probe_findings_v1.md)。

### 30.1 D1 — source of truth と access route

Aave 自身の subgraph は利用不能である(hosted service は HTTP 301 で sunset、
decentralized gateway は API key 必須)。§27 の source 指定は
"Aave's own protocol subgraph / **historical protocol state** where available" という
**or** であり、後者を採る。これは**以前から許されていた source 定義への適合**であって、
別プロバイダへの差し替えではない。

```text
RATE_SOURCE_OF_TRUTH      = "aave_contract_state_on_ethereum_mainnet"
RATE_ACCESS_ROUTE         = "archive_rpc_eth_call"
RATE_ACCESS_PROVIDER_ROLE = "transport_not_economic_source"
RATE_CHAIN_ID             = 1
```

**RPC 提供者は transport であって経済的なデータ源ではない。** したがって提供者を
替えても source は変わらない。ただし観測が**どのチェーンのどのブロックの state か**は
検証可能でなければならないため、**全観測**が次を保持する:

```text
RATE_PROVENANCE_REQUIRED = ("chain_id", "block_number", "block_timestamp", "block_hash")
```

`chain_id != 1` の観測は integrity error として破棄する。

### 30.2 H17 — 完全性を **reserve list membership** で定義する(option B + protocol membership semantics)

**問題**(v1.8.3 までの穴): `getReserveData()` は**未上場 reserve に対しても revert せず
全語ゼロを成功応答として返す**。「3資産すべてを要求」を*読み取りの成否*で判定すると、
未上場 reserve が **0% として basket に混入する**。実測では V3 Core の USDT が
2023-01-27 から 2023-02-13 まで未上場であり、2023-01-27 は3資産とも未上場で
平均が丸ごと 0.0000% になっていた。

**解決**: 「3資産が揃っている」を次のように再定義する。

> USDT / USDC / DAI の凍結アドレスが、**rate 観測に使ったのと同じ履歴ブロック**において、
> protocol の**初期化済み(configured)reserve list の member である**こと。

世代ごとの primitive(**V1 だけ名前が違う。共通名で呼べると仮定しない**):

| 世代 | primitive | selector |
|---|---|---|
| aave_v1 | `getReserves()` | `0x0902f1ac` |
| aave_v2 | `getReservesList()` | `0xd1946dbc` |
| aave_v3_core | `getReservesList()` | `0xd1946dbc` |

```text
RATE_COMPLETENESS_RULE   = "initialized_reserve_list_membership_at_observation_block"
RATE_MEMBERSHIP_BLOCK_RULE = "same_block_as_rate_read"
```

**いずれかの成分が初期化されていない日**:

- `mean_apr = null`
- 欠落/未初期化の成分を記録する
- **0 で代替しない**(`RATE_ZERO_SUBSTITUTION_ALLOWED = False`)
- **basket を2資産へ縮めない**(`RATE_TWO_ASSET_FALLBACK_ALLOWED = False`)
- **前の Aave 世代を延長しない**(`RATE_GENERATION_EXTENSION_ALLOWED = False`)
- **凍結 splice 日を動かさない**(`RATE_SPLICE_DATES_MOVABLE = False`)
- **補間も forward-fill もしない**(`RATE_FORWARD_FILL_ALLOWED = False`)

**全語ゼロ構造体の検出は独立した cross-check としてのみ残す**
(`RATE_ZERO_STRUCT_DIAGNOSTIC = "independent_cross_check_only"`)。
membership と食い違ったら:

```text
RATE_INTEGRITY_DISAGREEMENT_ACTION = "emit_integrity_error_and_no_rate_value"
```

**どちらか一方の解釈を選ばない。** integrity error を出し、その日は値を出さない。

**期待される帰結**: 既にプローブした V3 launch 期(2023-01-27〜2023-02-13)は
「不完全な basket の日」から「レート欠測の日」へ変わる。
**この期待を日付のハードコード規則にしてはならない**
(`RATE_LAUNCH_GAP_DERIVATION = "derived_from_historical_reserve_membership"`)。
欠測は**履歴上の reserve membership から導出**される。

欠測日の下流での扱いは §28(H16)のとおり: `RATE_MAX_STALE_SECONDS = 24h` を
超えた時点で r は陳腐化し、**シグナルが成立しない**。値は補完されない。

### 30.3 O1 — launch 期の有効値を加工しない

V1→V2 接合直後(2020-12-03 以降)の V2 レートは 0.53%〜21.2% の範囲で激しく振れ、
USDT 単体では 51.669% を記録する日がある。これは**未初期化ではなく実データ**であり、
薄商いに由来する実際の借入金利である。

```text
RATE_VALUE_TREATMENT = "no_filter_no_clip_no_smoothing_no_winsorization"
```

**有効な非ゼロの launch 期金利を filter / clip / smooth / winsorize しない。**
極端さを理由に落とすことは、結果を見てからの標本選択に等しい。

### 30.4 v1.8.4 で追加するテスト

| # | テスト |
|---|---|
| **T49** | 未初期化 reserve は **0% として basket に入れない** |
| **T50** | 初期化済み reserve の**本物の 0% 借入金利**は有効な 0% 観測として残る |
| **T51** | membership は **rate 読み取りと同一の履歴ブロック**で検査される |
| **T52** | 3資産のうち1つでも欠ければ**その日の basket 全体が null** |
| **T53** | V2→V3 の接合は **2023-01-27 のまま** |
| **T54** | **2資産へ縮退する経路が存在しない** |
| **T55** | 全語ゼロ診断と membership の**食い違いは出力をブロックする** |
| **T56** | 食い違い時に**どちらの側にも寄せていない** |
| **T57** | 全観測が chain id / block number / timestamp / hash を保持する |
| **T58** | mainnet 以外の chain id を拒否する |
| **T59** | `hint` による探索の高速化が**答えを変えない** |
| **T60** | launch 期の極端だが有効な値を**加工していない** |
| **T61** | reserve list primitive が**世代ごとに凍結**されている(V1 のみ別名) |

---

## 31. v1.8.5 §31 — H14b 強制清算の執行モデル(**H14b を解決する**)

**適用範囲**: 清算時の執行価格、mark 経路の観測可能性、gate の順序、H14a の条件化。
**変更していないもの**: 仮説、family、layer 境界、昇格規則、コスト、証拠金規則、
イベント順序(§24.4)、清算会計(§24.5)、`FINAL_OOS_START`、封印、**H13**。

§25 の優先順位により、本節以降は同一フィールドについて §24.6 を supersede する。

```text
LIQUIDATION_EXECUTION_MODEL = "adverse_trade_extreme_capped_at_bankruptcy"
```

short perp について:

```text
trigger_price    = (margin + q·entry) / (q(1 + mmr))
bankruptcy_price = trigger_price · (1 + mmr)
candidate        = max(trigger_price, perp_high)
liquidation_fill = min(candidate, bankruptcy_price)
```

### 31.1 2つの価格の役割は排他である

| 量 | 役割 | 供給源 |
|---|---|---|
| **`mark_high`** | **清算判定のみ**(trigger-only) | `mark_price_5m`(markPriceKlines) |
| **`perp_high`** | **執行価格の代理のみ**(execution-proxy-only) | `klines_5m`(perp の約定) |

**入れ替えてはならない。** 入れ替えると、板に無い価格で約定したことにするか、
清算されない局面で清算したことにするかのどちらかになる。
`test_mark_high_is_trigger_only_and_perp_high_is_execution_only` が、
入れ替えたときに binding と fill が変わることを固定する。

### 31.2 固定滑りの廃止

**`liquidation_slippage_bps` は廃止した。** 滑りは板の深さ・清算の連鎖・
そのバーのボラティリティで決まるのであって、取引所が公表する定数ではない。
`UNFROZEN_PARAMETERS` から削除し、`TwoLegConfig` からも消した。
ソースに当該識別子が存在しないことをテストで固定する。

### 31.3 破産価格による上限

上限は、**利用者の建玉に帰属する損失が破産境界を越えて伸びるのを防ぐ**。
破産境界を越えた市場損失は取引所の保険基金 / ADL 機構が負担するものであり、
**清算された口座へ再び計上されるべきものではない**。

`bankruptcy − trigger = mmr`(tier 1 で 40 bps)であるため、
どの枝が効いたかを必ず記録する:

```text
fill_rule_binding ∈ {"floor", "observed", "cap"}
```

### 31.4 執行代理が無いバー

清算バーに `perp_high` が無ければ、**清算を評価しない**。
`disposition = liquidation_state_unknown` とし、**清算が無かったとも仮定しない**。

---

## 32. v1.8.5 §32 — mark 経路の観測可能性(**機械可読。join で消えない**)

**欠測の Vision mark バーを inner join で消してはならない。**
canonical な5分タイムラインを端から端まで構成し、mark データと**品質状態**を付ける。
欠測バーも**行として残る**(値は `None`。捏造しない)。

```text
MARK_PATH_STATUSES = (
    "observed",             # Vision に実在し mark_samples > 0
    "verified_repair",      # 一次情報で復元し、重複窓で完全一致した
    "route_unverified",     # 経路が塞がれていて未判定。**source の欠測ではない**
    "stale_unverified",     # mark_samples == 0(前値横引き)。未検証
    "source_unobservable",  # 一次情報が応答した上で復元できない/不一致
)
MARK_PATH_ACCEPTABLE = ("observed", "verified_repair")
```

### 32.1 現況(2026-08-18 の実測に基づく)

| 対象 | 状態 |
|---|---|
| **P1**(Vision 欠測 8 区間 2,318 本) | **`route_unverified`**。`source_unobservable` では**ない** |
| **P2**(`mark_samples == 0` 4 区間 43 本) | **`stale_unverified`** |

egress が地域制限で塞がれているため一次情報に到達できていない
([P1/P2 プローブ所見](p1_p2_mark_availability_probe_v1.md))。
**「取れなかった」を「存在しない」と書かない。**
許可された地域からプローブを再実行して `candidate_deterministic_repair` が付けば、
その区間は `verified_repair` へ昇格し、`observed` と同等に扱える。

**前値の横引きを「バー内の不利側 mark 経路」の証拠として扱わない。**
横引きは「その5分間に mark の更新が無かった」ことしか意味しない。

### 32.2 建玉中の扱い

建玉中のバーのうち **1本でも** `observed` / `verified_repair` 以外があれば:

- その trade を**落とさない**
- 清算が起きなかったと**仮定しない**
- **経済指標より前に layer を中断**し `disposition = liquidation_state_unknown`

---

## 33. v1.8.5 §33 — gate の順序(**この順序でしか評価しない**)

```text
mark 経路の観測可能性 → 清算検出 → 清算件数 → H14a の手数料 gate → 経済指標
```

**観測可能性を満たさない経路を `liquidation_count == 0` と数えてはならない。**
数えれば「観測できなかった」が「清算は起きなかった」に化ける。
`evaluate_gates()` は観測可能性で先に返し、**そこで件数を見ない**。

---

## 34. v1.8.5 §34 — H14a の条件化

```text
liquidation_count == 0                        → H14a は拘束しない(non-binding)
liquidation_count >  0 かつ 手数料が未解決     → liquidation_model_blocked
```

**ゼロ手数料での代替は決して行わない。**
`liquidation_count` は会計上の件数であって経済的な帰結の指標ではない。
この分岐に return も PnL も要らない。

`liquidation_model_blocked` と `liquidation_state_unknown` は GO でも NO-GO でもない。
§29 の `source_sensitive` と同じく**「この設計では判定できない」**という帰結である。

---

## 35. v1.8.5 §35 — H13 は Arm-R シグナル生成の**唯一の hard blocker**

実測 taker rate は**コスト依存のエントリ境界**に入る:

```text
ρ_u(C) = κ · log(1 + C)          C は往復コスト(taker を含む)
Arm R entry:  ρ > ρ_u(C)
```

H13 が未解決である限り境界そのものが未定であり、**経験的な Arm-R シグナルを
生成できない**。v1.8.5 は **experiments を解禁しない**。

---

## 36. v1.8.5 で追加するテスト

| # | テスト |
|---|---|
| **T62** | 固定滑りが設定にも凍結集合にもソースにも存在しない |
| **T63** | fill が破産価格を越えない(`cap`) |
| **T64** | perp_high がトリガーより良いとき下限が効く(`floor`) |
| **T65** | バンド内では観測値が効く(`observed`) |
| **T66** | `fill_rule_binding` が常に凍結値のいずれか |
| **T67** | **mark_high と perp_high を入れ替えると結果が変わる** |
| **T68** | 清算バーに `perp_high` が無ければ `liquidation_state_unknown` |
| **T69** | 清算経路でも損益恒等式が成り立つ |
| **T70** | 欠測バーが**行として残る**(inner join で消えない) |
| **T71** | `mark_samples == 0` が `stale_unverified` になる |
| **T72** | プローブの分類が状態へ正しく写る |
| **T73** | **遮断が `source_unobservable` へ降格しない** |
| **T74** | 建玉中の許容外状態が `liquidation_state_unknown` を出し、清算なしと数えない |
| **T75** | gate が観測可能性で先に返り、件数を見ない |
| **T76** | H14a が件数 0 で非拘束、件数 > 0 かつ未解決で `liquidation_model_blocked` |
| **T77** | P1 が `route_unverified`、P2 が `stale_unverified` に凍結されている |

# Phase 8.1 — Aave 履歴レート源の可用性プローブ 所見 (v1)

- 日付: 2026-08-18 (UTC)
- 対象凍結版: 報告時 **v1.8.3** → **v1.8.4 で解決済み**(§7 を参照)
- 作業区分: **入力データ再構成のみ**。rho を BTC 実データへ適用していない。
  entry も return も PnL も生成していない。Phase 8 実験は一切実行していない。
- Layer 1/2/3 の outcome データ、Final OOS、封印済み prior register は**読んでいない**。
- **H13(taker commission)と H14(liquidation slippage)は実験ブロッカーのまま。**
  本書は新たに **H17** と **O1** を追加報告した。**§1〜§6 は報告時点の記述である**(v1.8.4 の決定は §7)。

---

## 0. 結論(先に)

指示は「凍結された source assumption と V1/V2/V3 の履歴アクセスが違っていたら、
黙って別プロバイダへ差し替えず、止めて報告せよ」であった。**2点が違っていた。**
どちらも黙って回避せず、以下に報告する。

| # | 種別 | 内容 | 私の対応 |
|---|---|---|---|
| **D1** | source アクセス | Aave 自身の subgraph は**利用不能**。凍結文の第2の選択肢「historical protocol state」を使った | 差し替えではなく**凍結文に列挙された方**を採用。ここで報告 |
| **H17** | 凍結規則の穴 | **未上場 reserve は成功応答として 0% を返す**。「3資産すべてを要求」は読み取り成否で判定するため 0% が平均へ混入する | 検出フラグを実装し記録。**平均の値は凍結どおり変更していない**。判断は人間へ |
| **O1** | データ特性 | 接合1直後(2020-12-03〜)の V2 レートが 0.53%〜21.2% で激しく振れる | **実データであり adapter の欠陥ではない**。記録のみ |

---

## 1. D1 — source アクセスが凍結の第1想定と違う

凍結文(§27 決定ログ)の primary source 指定は

> Aave's own protocol subgraph / historical protocol state where available

であり、**2つの選択肢の or** である。前者を先に試し、**利用不能**を実測した。

| route | 実測(2026-08-18T11:46Z) |
|---|---|
| hosted service `api.thegraph.com/subgraphs/name/aave/protocol-v2` | **HTTP 301**(Cloudflare。hosted service は sunset 済み) |
| decentralized gateway `gateway.thegraph.com/api/subgraphs/id/JCNW…81zk` | HTTP 200 だが body は `{"errors":[{"message":"auth error: missing authorization header"}]}` — **API key 必須**、本セッションは保持していない |

したがって **後者「historical protocol state」** を採った。すなわち Ethereum archive
node に対する `eth_call`(`getReserveData(address)`, selector `0x35ea6a75`)を、
日次で解決した**過去ブロック番号を明示して**実行する。

- これは**別プロバイダへの無断差し替えではない**。凍結文が列挙した2番目の経路である。
- ただし「Aave 自身の subgraph」を使えていない事実は**源の忠実度の低下**であり、
  既存の `RATE_SOURCE_FIDELITY = "partial_proxy_not_exact_A2"` に加えて記録する。
- archive アクセスは無認証の public RPC 4本で**実際に履歴 eth_call が通ることを実測**した
  (`DEFAULT_RPC_ENDPOINTS`)。プローブ本体は `eth-mainnet.public.blastapi.io` で実行。
- **利点**: subgraph の indexing ロジックを経由せず、**チェーン上の state をそのまま**読む。
  §27.4 の「その時刻以前のチェーン state のみを使う」という要求とは、むしろ整合が良い。
- **欠点**: subgraph が持つ「reserve が上場済みか」等の派生フィールドが無い。
  これが H17 を招いた直接の原因である。

**人間の判断が要る点**: この経路変更を v1.8.3 の枠内の実装詳細として受け入れるか、
provenance に明示的な `RATE_ACCESS_ROUTE = "archive_eth_call"` を足して再凍結するか。
**私は決めていない。**

---

## 2. H17 — 未上場 reserve の 0% が凍結の完全性検査を通り抜ける

### 2.1 事実

凍結規則(§27.4)は

> require all three assets otherwise emit null

である。この「揃っている」の判定は **読み取りが成功したか** で行われる。ところが
Aave の `getReserveData()` は、**その世代にまだ上場していない reserve に対しても
revert せず、全語ゼロの構造体を成功応答として返す**。

実測(V3 Core Pool `0x87870Bca…4E2`, block 16501588 = 2023-01-28 00:00Z 直前):

```
USDT : words=15  nonzero=0   →  configuration=0, liquidityIndex=0,
                                 variableBorrowIndex=0, lastUpdateTimestamp=0,
                                 aToken=0x0000…0000
USDC : words=15  nonzero=13  →  liquidityIndex=1.000075103,
                                 lastUpdateTimestamp=1674861287,
                                 aToken=0x98c2…f5c,  variableBorrowRate=3.8599%
```

USDT は **decode 誤りではなく、未初期化の reserve** である。同ブロックの USDC は
健全に読めている。つまり `variableBorrowRate = 0` は「読めた 0%」として扱われ、
凍結規則の完全性検査を**通過する**。

### 2.2 影響範囲(実測)

`experiments/phase8/aave_splice_probe_v1.json`(接合窓25日)より:

| 日付 | 世代 | 未初期化 | 凍結規則の mean_apr |
|---|---|---|---|
| 2023-01-26 | aave_v2 | — | 2.9994% |
| **2023-01-27** | aave_v3_core | **USDT, USDC, DAI** | **0.0000%** ← 3資産とも未上場。完全な捏造値が「完全」として通る |
| 2023-01-28 | aave_v3_core | USDT | 2.3514% |
| 2023-01-29 … 2023-02-13 | aave_v3_core | USDT | 1.5%〜2.3% |
| **2023-02-14** | aave_v3_core | — | USDT が初めて生きる (0.4692%) |

- V3 Core の **USDT reserve は 2023-01-27 から 2023-02-13 まで 18 日連続で未初期化**。
  2023-02-14 に上場。
- USDC / DAI は 2023-01-27 のみ未初期化、2023-01-28 から健全。
- 影響: 2023-01-28〜2023-02-13 の 17 日間、平均は **真値のおよそ 2/3**(USDT の 0% が
  1/3 の重みで入る)。凍結された **splice 日 2023-01-27 の 1 日は平均が丸ごと 0%**。
- 接合1(V1→V2, 2020-12-03)では未初期化は**発生していない**。V2 は3資産を launch 時点で
  上場済み。

### 2.3 何をしたか / しなかったか

**した**:
- `ReserveReading.reserve_uninitialised`(全語ゼロ判定。**世代非依存**で、新たな
  構造体 index を凍結せずに済む)を実装。
- `DailyObservation.uninitialised_reserves` / `.contaminated_by_uninitialised` に記録。
- プローブ成果物に `h17_uninitialised_reserve_days` として集計。
- 単体テストで「**0% はそのまま平均へ入る**」ことを固定(凍結規則を黙って変えていない証拠)。

**しなかった**(いずれも凍結規則の変更になるため):
- 0% を欠測(null)へ倒すこと。
- V2 を凍結 splice 日より後ろへ延長すること。
- 3資産バスケットの構成を一時的に2資産へ落とすこと。
- 該当日を黙って落とすこと。

### 2.4 人間の判断を要する選択肢(**私は選ばない**)

| 案 | 内容 | 代償 |
|---|---|---|
| **A** | 凍結規則を維持。汚染日も 0% 込みで使う | r が過小 → `rho = κ(1-e^{-(f-s)}) - (r-r')` が過大 → Arm R のエントリが偽陽性に振れる。18日分 |
| **B** | 「未初期化 reserve は欠測」と再定義し、当該日を null にする(§27.4 の完全性判定を *読み取り成否* から *reserve 稼働* へ変更)。再凍結が要る | 2023-01-27〜02-13 の 18 日が rate 欠測 → §H16 の `RATE_MAX_STALE_SECONDS = 24h` によりシグナル不成立(補完しないので「値の捏造」は起きない)。**忠実度は上がるが標本が減る** |
| **C** | splice 日を 2023-02-14(3資産が V3 で揃う日)へ動かし、それまで V2 を使う | 凍結した splice 日の変更。V2 は当該期間も稼働しているので技術的には可能だが、「版の切替日」の定義を後から結果に合わせて動かす形になり、**preregistration の趣旨に反する疑いが強い** |
| **D** | バスケットを一時的に稼働資産のみへ | 「USDT/USDC/DAI を等加重、3つ揃うことを要求」という凍結を破る。**明示的に禁止された「黙って構成を変える」に該当する**ため、やるなら再凍結必須 |

私の所見(**決定ではない**): **B** が最も preregistration に整合する。欠測を欠測として
扱うだけで、閾値も期間も結果を見て動かさない。ただし **B は凍結規則の変更であり、
再凍結(v1.8.4)を伴う**。C は結果依存の日付移動に見えるため避けるべきと考える。

---

## 3. O1 — 接合1直後の V2 レートの激しい振れ(**実データ**)

```
2020-12-02  aave_v1   mean= 8.1521%   USDT= 5.850 USDC=10.964 DAI= 7.643
2020-12-03  aave_v2   mean= 0.5322%   USDT= 0.840 USDC= 0.001 DAI= 0.756
2020-12-04  aave_v2   mean=16.7330%   USDT=12.066 USDC=29.482 DAI= 8.651
2020-12-05  aave_v2   mean= 2.8342%
2020-12-06  aave_v2   mean=13.8945%   USDC=34.614
2020-12-08  aave_v2   mean=21.2078%   USDT=51.669
```

- **未初期化ではない**(全日 `nonzero_word_count > 0`)。V2 launch 直後の
  **利用率が薄いための実際の金利変動**である。adapter の欠陥ではない。
- ただし「無リスク金利 r」の proxy として 51.669% の USDT borrow APR を使うことの
  経済的妥当性は別問題である。**本書では扱わない**(閾値やフィルタを後付けすると
  結果依存の設計変更になるため)。事実として登録するに留める。
- 同様の薄商い期は V1 期(2020-01-08 の genesis 近傍で3資産とも空応答 → 正しく null)
  にも存在する。

---

## 4. 凍結された想定のうち、実測で確認できたもの

| 凍結項目 | 実測結果 |
|---|---|
| Ethereum mainnet のみ | ✓ 全観測が mainnet |
| V1 → V2 → V3 Core の半開区間 | ✓ 境界の両側で期待どおりの世代 |
| **V4 へ移行しない** | ✓ V4 ローンチ日 2026-03-30 も `aave_v3_core`(3.4965%) |
| 00:00:00Z を target とし `block.timestamp <= target` の**最新**ブロック | ✓ 全観測で `block_timestamp <= target_ts` |
| RAY(1e27)を **APR** として解釈。**APY へ変換しない** | ✓ `apr_decimal == raw_ray / 1e27` をテストで固定 |
| 資産を**ティッカーだけで同定しない** | ✓ 世代ごとにアドレス+decimals を凍結して検証 |
| 補間・平滑化なし | ✓ 実装に補間経路が無い |
| 3資産すべてを要求 | ✓ ただし **H17 の穴あり**(上記) |
| provenance 一式(§27.6) | ✓ 版/network/market, target ts, block number/ts/hash, token address, raw RAY, 小数 APR, 平均, endpoint, raw 応答 SHA-256, 取得 UTC 時刻 |

`rate_word_index` は世代ごとに**実測して確定**した。V2 と V3 はどちらも index 4 だが
**構造体の並びが異なり偶然一致しているだけ**であり、テストで別々に固定してある。

---

## 5. 成果物と hash

| path | SHA-256 |
|---|---|
| `src/mce/aave_rates.py` | `047dbe287461574caa21e5e7478421d01ca20bab921508ef55bcbc4539cbcd3a` |
| `src/mce/aave_probe.py` | `7351253684185a3412775f22fcc94b5d5abdc732df974c0fb350aa2e5729c075` |
| `tests/test_aave_rates.py` | `af3447d68725b05b70be32a116741d0050085e8953d8a423d4828c65ddf535bd` |
| `experiments/phase8/aave_availability_probe_v1.json` | `4e57c8dee77e48917da00c771677d9c125b55fb1981df4d12289a55704fdc084` |
| `experiments/phase8/aave_splice_probe_v1.json` | `c0b0b4f9c4841aaaff60057f45f5acc8bc5f1e196e9baff4a540c94eeef5e2fd` |

上記 hash は**本書作成時点の内容**に対するものであり、凍結不変量ではない
(v1.8.3 の凍結対象は `carry_freeze_v1_8_3.json` に列挙された artifact のみ)。

生の外部応答は**版管理へ入れていない**。プローブ成果物に入っているのは
provenance(ブロック識別子・token アドレス・生 RAY 値・変換後 APR・応答の SHA-256)
だけである。

再現:

```
uv run python -m mce.aave_probe --mode availability \
    --json experiments/phase8/aave_availability_probe_v1.json
uv run python -m mce.aave_probe --mode splice \
    --json experiments/phase8/aave_splice_probe_v1.json
```

---

## 6. ブロッカーの現況

| # | 内容 | 状態 |
|---|---|---|
| H13 | BTCUSDT USD-M taker commission(認証読み取り) | **未解決 — 実験ブロッカー** |
| H14 | `liquidation_slippage_bps` | **未解決 — 実験ブロッカー** |
| **H17** | 未上場 reserve の 0% が完全性検査を通る | **未解決 — 人間の判断待ち**。rate 系列の確定を妨げる |
| O1 | 接合1直後の薄商いレート | 記録のみ。判断不要(判断すると結果依存の設計変更になる) |

**H17 が未解決の間、rate 系列を確定させない。** 系列を確定させれば、そのまま
2023-01-27 の 0.0000% を「観測された無リスク金利」として下流へ流すことになる。

---

## 7. v1.8.4 での決定(2026-08-18。**§1〜§6 は報告時点の記述として残す**)

人間の決定により、**D1 は適合として受理**、**H17 は option B を protocol membership
semantics で強化して解決**、**O1 は加工しないことを凍結**した。
凍結条文は [protocol §30](carry_replication_protocol_v1.md#30-v184-修正条項入力データ源の確定仮説は変更していない)。

### 7.1 D1 の決定

archive contract-state 経路は、**以前から許されていた source 定義への適合**として受理された。

```text
RATE_SOURCE_OF_TRUTH      = "aave_contract_state_on_ethereum_mainnet"
RATE_ACCESS_ROUTE         = "archive_rpc_eth_call"
RATE_ACCESS_PROVIDER_ROLE = "transport_not_economic_source"
```

RPC 提供者は transport であって経済的な源ではない。全観測が
chain id / block number / block timestamp / block hash を保持する。

### 7.2 H17 の決定 — §1 の選択肢のうち **B**(membership で強化)

「3資産が揃っている」を、**rate 観測と同じ履歴ブロック**における
**初期化済み reserve list membership** として再定義した。
§2.4 の C(splice 日移動)と D(basket 縮小)は**採用されなかった**。

primitive は世代ごとに凍結した。**V1 だけ名前が違う**:
`getReserves()`(V1) / `getReservesList()`(V2, V3 Core)。

再実行後の結果(**同じプローブ日、同じブロック番号**):

| 日付 | v1.8.3 の挙動 | **v1.8.4 の挙動** |
|---|---|---|
| 2023-01-27 | `mean_apr = 0.0000%`(「完全」として通過) | **欠測**。missing = USDT, USDC, DAI |
| 2023-01-28 〜 2023-02-13 | `mean_apr` が真値の約 2/3 | **欠測**。missing = USDT |
| 2020-12-08 | 21.2078%(USDT 51.669%) | **21.2078% のまま**(O1: 加工しない) |
| 2020-01-08 | 欠測 | **欠測**(reserve list 自体が空応答) |

接合窓25日のうち **10日が欠測**、可用性プローブ17日のうち **4日が欠測**。

**integrity error は 0 件**である。すなわち membership 判定と全語ゼロ診断は、
実測したすべての日で**一致した**。これは片方が他方の言い換えではないことを確認したうえでの
一致であり、cross-check として機能している。

欠測は **membership から導出**されている。実装に `2023-01-27` などの
日付リテラルは存在せず、テスト `test_h17_launch_gap_is_derived_not_hard_coded` が
それを機械的に固定している。

### 7.3 O1 の決定

`RATE_VALUE_TREATMENT = "no_filter_no_clip_no_smoothing_no_winsorization"`。
2020-12-08 の USDT 51.669% を含む launch 期の有効値は**そのまま残す**。

### 7.4 変わっていないもの

仮説、family、layer 境界、昇格規則、コスト、証拠金規則、`FINAL_OOS_START`、封印、
そして **H13(taker commission)と H14(liquidation slippage)の実験ブロッカー状態**。
v1.8.4 は入力データ源のみを扱い、**実験を解禁していない**。

---

## 8. 系列再構成で判明した取得側の欠陥(**凍結規則の変更ではない**)

v1.8.4 封印後に日次系列の再構成を始めたところ、**取得パイプライン側の欠陥**が出た。
凍結規則の問題ではないので再凍結は不要だが、記録する。

### 8.1 D2 — transport の失敗が「レートが無い日」と区別できていなかった

`eth.merkle.io` が非 JSON 応答を返す状態になり、その endpoint に割り当てた
202 日分が**すべて `mean_apr = null`** になった。凍結アダプタの挙動自体は正しい
(`note` に「ブロック解決に失敗」と残る)。しかし系列に載せてしまうと、

- **protocol state としての欠測**(§30.2。reserve が未上場)
- **私の取得が失敗しただけの日**

が同じ null として並ぶ。これは経済的な記録を偽ることになる。

**対処**(`src/mce/aave_series.py`。**凍結対象外**):

- 各行を `complete` / `missing_by_protocol` / `integrity_error` / `transport_failure` へ分類する。
  判定は adapter の error 文言による。「空応答(」で始まるものは protocol state の不在、
  それ以外の RPC error は transport の失敗。
- `transport_failure` は **観測として採用しない**。検証済み endpoint を巡回して再取得する。
- 規定回数で取れなければ**系列を書かずに中断**する。
  取得できなかった日を「レートが無い日」として記録しない。
- manifest に `days_transport_failure` を出し、テストで **0 であること**を固定する。

**凍結規則は変えていない。** §30.2 の null は protocol state についての言明であり、
本節はその言明を取得失敗で汚さないための取得側の規律である。

### 8.2 D3 — 既定 endpoint 一覧の記述が実態と違っていた

凍結済み `aave_rates.py` の `DEFAULT_RPC_ENDPOINTS` には
「認証不要で履歴 eth_call が通ることを実測したもの」と注記していた。再実測の結果:

| endpoint | 再実測(2026-08-18) |
|---|---|
| `eth-mainnet.public.blastapi.io` | ✓ cid=1, reserve list 25 件, USDT 2.6039524726649894% |
| `rpc.mevblocker.io` | ✓ 同一の値 |
| `gateway.tenderly.co/public/mainnet` | ✓ 同一の値 |
| `eth.merkle.io` | **✗ `eth_chainId` が非 JSON**(現時点で利用不能) |

3本は**同一ブロックで完全に同じ値**を返す。これは transport が経済的な源では
ないこと(§30.1)の実地確認でもある。

`DEFAULT_RPC_ENDPOINTS` は凍結モジュール内にあるため**編集しない**。
一覧は候補であって、実際に使う endpoint は実行時に `chain_id` 検証を通ったものだけである。
系列再構成では検証済みの3本のみを指定した。

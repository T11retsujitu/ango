# Phase 8.1 — Aave 日次レート入力系列の再構成メモ (v1)

- 日付: 2026-08-18 (UTC)
- 対象凍結版: **v1.8.4**(`experiments/phase8/carry_freeze_v1_8_4.json`)
- 作業区分: **入力データ再構成のみ**。BTC データへ join せず、rho / シグナル /
  return / PnL / Phase 8 の帰結を一切計算していない。
- **本文書は凍結対象ではない。** 凍結後に判明した*取得側*の事実を記録する。
  凍結規則(§30)は本文書によって変更されない。

---

## 0. 凍結違反を1件出したので先に記録する

v1.8.4 封印後、私は本節の内容を**凍結済みの**
`docs/phase8/aave_source_probe_findings_v1.md` に追記した。同文書は
`carry_freeze_v1_8_4.json` の `probe_findings` として SHA-256 で凍結されている。

`test_frozen_spec_was_not_edited_after_the_freeze` が**これを検出して失敗した**。
凍結文書を封印時の内容へ復元し、内容は本文書(凍結対象外)へ移した。
凍結記録・ハッシュはいずれも変更していない。

検出機構は意図どおり働いた。ただし**私が凍結後に凍結artifactへ書き込もうとした**
という事実は残るので、ここに明記する。

---

## 1. D2 — transport の失敗が「レートが無い日」と区別できていなかった

系列の再構成を始めたところ、`eth.merkle.io` が非 JSON 応答を返す状態になり、
その endpoint に割り当てた **202 日分がすべて `mean_apr = null`** になった。

凍結アダプタの挙動自体は正しい(`note` に「ブロック解決に失敗」と残る)。
しかしそのまま系列に載せると、

- **protocol state としての欠測**(§30.2。reserve が未上場)
- **私の取得が失敗しただけの日**

が同じ null として並ぶ。これは経済的な記録を偽ることになる。

**対処**(`src/mce/aave_series.py`。**凍結対象外**):

- 各行を `complete` / `missing_by_protocol` / `integrity_error` / `transport_failure`
  へ分類する。判定は adapter の error 文言による。「空応答(」で始まるものは
  protocol state の不在、それ以外の RPC error は transport の失敗。
- `transport_failure` は **観測として採用しない**。検証済み endpoint を巡回して再取得する。
- 規定回数で取れなければ**系列を書かずに中断**する。
  取得できなかった日を「レートが無い日」として記録しない。
- manifest に `days_transport_failure` を出し、テストで **0 であること**を固定する。

**凍結規則は変えていない。** §30.2 の null は protocol state についての言明であり、
本節はその言明を取得失敗で汚さないための**取得側の規律**である。

---

## 2. D3 — 既定 endpoint 一覧の記述が実態と違っていた

凍結済み `aave_rates.py` の `DEFAULT_RPC_ENDPOINTS` には
「認証不要で履歴 eth_call が通ることを実測したもの」と注記していた。再実測の結果:

| endpoint | 再実測(2026-08-18) |
|---|---|
| `eth-mainnet.public.blastapi.io` | ✓ cid=1, reserve list 25 件, USDT 2.6039524726649894% |
| `rpc.mevblocker.io` | ✓ 同一の値 |
| `gateway.tenderly.co/public/mainnet` | ✓ 同一の値 |
| `eth.merkle.io` | **✗ `eth_chainId` が非 JSON**(現時点で利用不能) |

対象ブロックは 12545218(2021-06-01 00:00Z 直前)。
3本は**同一ブロックで完全に同じ値**を返す。これは transport が経済的な源では
ないこと(§30.1)の実地確認でもある。

`DEFAULT_RPC_ENDPOINTS` は凍結モジュール内にあるため**編集しない**。
一覧は候補であって、実際に使う endpoint は実行時に `chain_id` 検証を通ったものだけである。
系列再構成では検証済みの3本のみを指定した。

**注記の訂正**: 凍結時点の「4本すべてで実測済み」という記述は、
少なくとも `eth.merkle.io` については**現時点で成り立たない**。
凍結文の編集は行わず、ここに訂正として残す。

---

## 3. 系列の所在と版管理

| 種別 | path | 版管理 |
|---|---|---|
| 系列本体 | `data/phase8/aave_daily_rate_v1.jsonl` | **入れない**(`data/*` は gitignore) |
| manifest | `data/manifests/aave_daily_rate_v1.json` | 入れる |
| ビルダ | `src/mce/aave_series.py` | 入れる(**凍結対象外**) |
| テスト | `tests/test_aave_series.py` | 入れる |

系列の各行は provenance のみを持つ:
chain id / block number / block timestamp / block hash / token address /
生 RAY / 小数 APR / 応答 SHA-256 / reserve list の SHA-256 と要素数。
**生の外部応答は保存しない。** reserve list のアドレス列も保存しない
(ブロックから再取得でき、SHA-256 で同一性を確認できるため)。

manifest は要約のみを持ち、**日次のレート値そのものは持たない**。

---

## 4. 再現手順

```
# 断片ごとに取得(検証済み endpoint を明示する)
uv run python -m mce.aave_series \
    --endpoint https://eth-mainnet.public.blastapi.io \
    --endpoint https://rpc.mevblocker.io \
    --endpoint https://gateway.tenderly.co/public/mainnet \
    --start 2020-01-08 --end 2026-08-18 \
    --out data/phase8/aave_daily_rate_v1.jsonl \
    --manifest data/manifests/aave_daily_rate_v1.json
```

分割実行して結合してもよい。`hint` は探索の高速化であって答えを変えないため
(T59)、分割の仕方は系列の内容に影響しない。`--merge` は日付順に結合し、
**内容の食い違う重複**と**暦日の欠番**を拒否する。

---

## 5. 再構成の結果(2026-08-18)

| 項目 | 値 |
|---|---|
| 期間 | 2020-01-08 〜 2026-08-18(**2415 日**、暦日が連続) |
| 完全 | **2396 日**(coverage **0.9921**) |
| 欠測(protocol state) | **19 日** |
| **transport 失敗** | **0** |
| **integrity error** | **0** |
| sha256 | `18f73bb1777e4465a90837bfa3169d141ac2bc75b09292789036094e511102bb` |

世代別:

| 世代 | 日数 | 完全 |
|---|---|---|
| aave_v1 | 330 | 329 |
| aave_v2 | 785 | **785** |
| aave_v3_core | 1300 | 1282 |

### 5.1 欠測 19 日の内訳(**すべて membership から導出**)

| 期間 | 日数 | 欠落成分 | 理由 |
|---|---|---|---|
| 2020-01-08 | 1 | USDT, USDC, DAI | V1 genesis。**reserve list 自体が空応答** |
| 2023-01-27 | 1 | USDT, USDC, DAI | V3 Core 接合日。3資産とも未上場 |
| 2023-01-28 〜 2023-02-13 | 17 | USDT | V3 Core の USDT が未上場 |

2023-02-14 に USDT が V3 Core へ上場し、そこから連続する(1.9762%)。
**日付をハードコードした規則は無い。** 上表はすべて履歴上の
`getReservesList()` membership から導出された結果である。

v1.8.3 の規則のままなら、この 19 日のうち 18 日は
**0% を混ぜた「完全な」観測**として系列に載っていた。

### 5.2 値の範囲(**加工していない**)

| | 日付 | 値 |
|---|---|---|
| 最小 | 2020-12-03 | 0.5322%(V2 launch 日) |
| 中央値 | — | 5.1042% |
| 最大 | 2024-12-06 | **42.319%**(USDT 40.273 / USDC 57.690 / DAI 28.994) |

最大値は V2 launch 期ではなく **2024-12-06 の V3 Core** である。すなわち
極端値は「初期の薄商い」だけの現象ではない。§30.3 のとおり
**filter / clip / smooth / winsorize を一切していない**。

接合の連続性(**平滑化していないので跳ぶ**):

```text
2020-12-02  aave_v1       8.152%
2020-12-03  aave_v2       0.532%    <- 接合1。跳ぶが、いずれも有効な実測値
2023-01-26  aave_v2       2.999%
2023-01-27  aave_v3_core  欠測      <- 接合2。0% で埋めない
2023-02-13  aave_v3_core  欠測
2023-02-14  aave_v3_core  1.976%
```

### 5.3 この系列で**していないこと**

- BTC データへ join していない
- rho を計算していない
- シグナル / entry / return / PnL を生成していない
- Phase 8 の帰結を評価していない
- Layer 1/2/3 の outcome、Final OOS、封印済み prior register を読んでいない

**H13(taker commission)と H14(liquidation slippage)は実験ブロッカーのまま。**

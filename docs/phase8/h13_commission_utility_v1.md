# Phase 8.1 — H13 資格情報ユーティリティ (v1)

- 日付: 2026-08-18 (UTC)
- 対象凍結版: **v1.8.4**(現行)
- 状態: **実装済み・テスト済み。ただし H13 は未解決**(本環境に資格情報が無いため)

---

## 1. できること — 1つだけ

```text
GET /fapi/v1/commissionRate?symbol=BTCUSDT      (signed USER_DATA)
```

`src/mce/binance_commission.py`。**これ以外の対外動作を持たない。**

## 2. できないこと(テストで固定)

| 制約 | 実装 | テスト |
|---|---|---|
| 署名付き **GET のみ** | POST / PUT / DELETE の経路がモジュールに存在しない | `test_no_write_method_exists_in_the_module` |
| **注文を出さない** | path allowlist は `/fapi/v1/commissionRate` の1本のみ | `test_dangerous_paths_are_refused_before_sending`(order / batchOrders / positionRisk / leverage / transfer / listenKey / allOpenOrders / marginType) |
| **資金を動かさない・レバレッジを変えない** | path の禁止語による二重防御 | 同上 |
| 注文系パラメータを渡せない | `FORBIDDEN_PARAMS`(side / quantity / price / type / leverage / …)。大文字小文字を無視 | `test_order_parameters_are_refused`, `test_case_insensitive_forbidden_params` |
| **拒否は送信前** | `_check_permitted()` を署名より前に呼ぶ | 拒否時に runner が1度も呼ばれないことを検査 |
| **資格情報を表示しない** | `Credentials.__repr__` / `__str__` が伏せる | `test_credentials_are_redacted_in_repr_and_str` |
| **API key を argv に載せない** | curl の設定を **stdin** から渡す(`curl --config -`) | `test_api_key_is_not_placed_in_argv` |
| **secret を送信しない** | secret は HMAC 計算にしか使わない | `test_secret_never_appears_in_the_request` |
| **資格情報を保存しない** | 記録は5項目のみ | `test_record_contains_only_the_five_permitted_fields` |
| **生の応答本文を保存しない** | 保持するのは SHA-256 だけ | 同上 |
| ドキュメントの 4bps を実測値にしない | 該当リテラルがソースに無いことを検査 | `test_documented_four_bps_example_is_not_used_as_a_measured_value` |

署名は query 文字列の HMAC-SHA256。**実際に送る query と署名対象が一致すること**を
`test_signature_covers_every_query_parameter` で固定し、プリミティブ自体は
RFC 4231 test case 2 で固定した。

**注意**: 端から端までの署名が Binance に受理されるかは、
**実際に認証済み呼び出しを行うまで検証できない**。ここで検証したのは
署名の構成と安全性の制約であって、取引所側の受理ではない。

## 3. 記録する5項目

```json
{
  "symbol": "BTCUSDT",
  "maker_commission_rate": 0.0,
  "taker_commission_rate": 0.0,
  "response_sha256": "<64 hex>",
  "retrieved_at_utc": "<ISO8601 UTC>"
}
```

## 4. H13 の現況 — **未解決**

本セッションの環境に `BINANCE_API_KEY` / `BINANCE_API_SECRET` は**存在しない**。
確認は**値を表示せず**、環境変数名の有無だけで行った。資格情報ファイルも無い。

したがって **H13 は解決していない**。ドキュメントの 4 bps を実測値として
代入することは禁じられており、行っていない。

### 4.1 資格情報を持つ環境での実行手順

```bash
export BINANCE_API_KEY=...      # 読み取り専用の API key を推奨
export BINANCE_API_SECRET=...
uv run python -m mce.binance_commission \
    --symbol BTCUSDT \
    --json experiments/phase8/h13_commission_rate_v1.json
```

出力は5項目のみで、資格情報も生の本文も表示されない。

**API key は読み取り専用(取引権限なし)で十分である。** この道具は
USER_DATA の GET しか行わないため、取引権限を付与する必要がない。

### 4.2 値が得られた後の手順(**順序を守る**)

1. `experiments/phase8/h13_commission_rate_v1.json` を得る
2. **狭い H13 パラメータ確定の修正条項**(v1.8.5 想定)を作る
   - `PERP_TAKER_BPS` を実測値へ差し替える
   - `COMMISSION_RATE_STATUS` を `resolved_authenticated_read` にする
   - 記録(hash / 取得時刻 / symbol / maker / taker)を凍結記録へ入れる
   - **同時に H14 の分割**(H14a / H14b)を凍結記録へ登録する
3. 凍結ハッシュを再計算し、**v1.8.4 を保存したまま**新しい不変の凍結記録を作る
4. **再凍結してから**、その値を実験に使う

`phase8_prereg.py` の SHA-256 は凍結不変量そのものなので、
**同じ凍結を主張したまま黙って編集することはできない。**

## 5. 現況のブロッカー

| # | 内容 | 状態 |
|---|---|---|
| **H13** | BTCUSDT USD-M taker commission | **未解決**(道具は完成。資格情報待ち) |
| **H14a** | 清算 clearance fee 率 | **未解決** |
| **H14b** | 強制清算の執行/滑りモデル | **未解決。草案のみ**([h14_split_and_h14b_draft_v1](h14_split_and_h14b_draft_v1.md)) |

# ローカル常時収集の運用層

OKX の板・BBO・約定は historical API が無く、**落とし損ねた時間は後から復元できない**。
したがってローカル収集では、collector 本体よりも「収集を止めない」「異常を黙って
混ぜない」「研究に使える状態だけを昇格する」運用層が価値を決める。

この文書は、その運用層(supervisor / quality gate / 日次 ingest / health ledger /
Vision 差分同期)の使い方と設計境界をまとめる。collector 自体の仕様は
[microstructure_collection.md](microstructure_collection.md) を参照。

## 責務の分割

| 層 | module | 見るもの | 見ないもの |
|---|---|---|---|
| collector | `mce.collect_microstructure` | WebSocket 受信と raw 書き込み | 品質判定・再起動方針 |
| supervisor | `mce.collector_supervisor` | exit code / signal / 空き容量 / clock raw の有無 | 取引所データの内容 |
| quality gate | `mce.session_gate` | closed raw の完全性 | 再起動・容量 |
| 日次 ingest | `mce.daily_ingest` | gate 結果 → 正規化 → 収集日台帳 | 取引判断 |
| Vision 同期 | `mce.binance_vision` | 公開済み履歴 zip の差分 | prospective feed |

supervisor が板 sequence を見ないのは意図的である。プロセス健全性と市場データの
整合性を同じ層で混ぜると、「再接続すれば正常に見える」誘因が生まれる。
板 sequence gap は gate と day manifest 側にだけ現れる。

## 1. supervisor(収集を止めない)

```sh
uv run python -m mce.collector_supervisor -- --inst-id BTC-USDT-SWAP
```

`--` 以降は collector へそのまま渡る。supervisor は child の終了を待ち、
`run_ledger.jsonl` に 1 run = 1 行を確定させてから、必要なら再起動する。

| 終了種別 | 判定 | 再起動 |
|---|---|---|
| `clean_exit` | exit code 0(`--duration` 満了など) | しない |
| `operator_stop` | SIGINT / SIGTERM | しない |
| `crash` | それ以外の異常終了 | 上限まで、full jitter の指数backoff |
| `start_failed` | child を起動できなかった | crash と同じ扱い |

**無限に再起動しない。** 次のいずれかで fail-closed 停止し、
`data/analysis/alerts/` に artifact を残す。

- 連続異常終了が `--max-consecutive-failures`(既定 4)に達した
- 再起動総数が `--max-restarts`(既定 12)を超えた
- 空き容量が `--min-free-disk-bytes`(既定 5 GiB)を割った
- clock quality raw が取得できない(`--skip-clock-quality-guard` で無効化可)

`--stable-run-seconds`(既定 300)以上続いた run は「一度は安定した」とみなし、
連続失敗の系列を数え直す。1 時間ごとの計画再接続を crash 扱いで積み上げない。

run ledger の主な列:

| 列 | 意味 |
|---|---|
| `run_id` / `supervisor_id` / `restart_ordinal` | 再起動と raw session を接続する |
| `collector_config_sha256` / `collector_argv` / `source_commit` | 設定・実装の再現性 |
| `termination_kind` / `exit_code` / `signal` / `start_error` | 停止理由の区別 |
| `stderr_tail` | 異常終了の一次情報 |
| `last_raw_path` / `last_raw_received_at_ns` | 欠損区間の上端を確定する |
| `backoff_seconds` / `duration_seconds` | 再起動ループを監査する |
| `disk_free_bytes` / `clock_quality_status` | 収集を続けてよい状態だったか |

`last_valid_book_seq` は supervisor では取らない(内容判断をしないため)。板 sequence の
最終値は gate ledger の `books_last_seq_id` と day manifest の `sequence_gaps` に出る。

## 2. quality gate と quarantine

```sh
uv run python -m mce.session_gate --output data/analysis/gate_summary.json
```

判定は 3 値だけで、「たぶん大丈夫」を作らない。

```text
closed *.jsonl.gz
    ├─ valid    → normalized へ昇格してよい
    ├─ invalid  → quarantine へ隔離し、理由を台帳化する
    └─ pending  → まだ close していない / 落ち着いていない
```

`valid` の必要条件は、既存の soak 検査(gzip footer・JSON parse・frame 連番・
subscribe ACK・lifecycle・板 sequence・約定照合)に加えて:

| 追加チェック | 失敗時 |
|---|---|
| clock quality sample が 1 つも無い | `invalid`(`clock_quality_missing`) |
| clock sample が session 窓を覆っていない | `invalid`(`clock_quality_uncovered`) |
| 一定時間以上の session で必須 channel が完全無受信 | `invalid`(`channel_silent`) |

原則:

- **不完全な区間をゼロ埋め・補間・暗黙 skip しない。**
- **invalid を削除しない。** `data/quarantine/` へ raw 配下の相対構造のまま移し、
  同じ場所に `*.quality.json`(理由と quality report 全文)を置く。
- `.jsonl.gz.partial` は常に対象外で、要約の `open_partial_files` に出る。
- 直近 `--settle-seconds`(既定 60)以内に更新された file は `pending` として次回に回す。
- 一度判定した path は再判定しない。日次ジョブから何度呼んでも結論は変わらない。

## 3. 日次 ingest と収集日 manifest

```sh
uv run python -m mce.daily_ingest --output data/analysis/daily_ingest.json
```

gate → 正規化 → manifest → health ledger を 1 回で通す。`valid` な session だけが
`data/normalized/okx/microstructure/v3` へ入る。同じ raw の再実行は同じ shard を
再利用し、manifest も同じ内容を再生成する。

`data/analysis/collection_days/collection_day_manifest_<YYYY-MM-DD>.json`:

```json
{
  "date_utc": "2026-08-16",
  "expected_channels": ["trades", "bbo-tbt", "books", "trades-all"],
  "covered_intervals": [],
  "uncovered_intervals": [],
  "sessions_valid": 0,
  "sessions_invalid": 0,
  "sequence_gaps": 0,
  "clock_status": "pass|fail|missing",
  "raw_digest": "...",
  "normalized_shard_digests": [],
  "quarantined_paths": [],
  "invalid_reason_codes": [],
  "source_commit": "..."
}
```

`covered_intervals` は **必要な全 stream が同時に生きていた区間**だけを指す。
public だけが生きていて business が落ちていた時間は covered ではない。
当日の manifest は `now` までを評価対象にし、未来を無収集として数えない。
UTC 日をまたぐ session は両日へ切り分けて計上する。

## 4. health ledger と alert

`data/analysis/collector/health_ledger.jsonl` に日次 1 行を追記する
(収集時間、最長無収集、valid/invalid session 数、sequence gap、clock 品質、
quarantine 件数、空き容量、正規化件数)。

alert は外部サービスへ送らず、まず `data/analysis/alerts/` に JSON を残す。

| kind | 条件 |
|---|---|
| `collection_gap` | 最長無収集が `--max-uncovered-seconds`(既定 900)を超えた |
| `quarantined_sessions` | その日に invalid session があった |
| `disk_below_floor` / `clock_quality_unavailable` / `consecutive_failure_limit` / `restart_budget_exhausted` | supervisor の fail-closed 停止 |

外部通知は、無収集が一定時間を超えた場合にだけ後から足せばよい。価格・収益の
アラートや自動判断は、収集の完全性が安定するまで入れない。

## 5. Binance Vision の差分同期

再取得可能な履歴ファイルなので、prospective feed とは別の頻度・別の障害モデルで扱う。

```sh
# ledger watermark から「最後の閉じた period」までだけを追加取得する
uv run python -m mce.binance_vision --through-latest-closed

# 取得せず、dataset ごとの可用性だけを出す
uv run python -m mce.binance_vision --report-only --report data/analysis/vision_availability.json
```

- **watermark**: `download_ledger.jsonl` で最後に `saved` / `cached` を確認できた period。
  `absent` と `checksum_mismatch` は watermark を進めない(公開が遅れているだけの
  period を「取得済み」と誤認しないため)。
- **公開遅延**: `--publication-lag-days`(既定 2)を引いた日付を基準に、日次 dump は
  その日まで、月次 dump は**閉じた月**までを対象にする。未確定の当日・当月は取りに行かない。
- **availability report**: `saved` / `cached` / `absent` / `retryable_error` を period ごとに出す。
  「取れた」「公開されていない」「取りに行って失敗した」を混ぜない。
- **retry budget**: HTTP 429 等は再試行するが、上限を使い切った period は
  `retryable_error` として ledger に明示してから中断する(黙って飛ばさない)。
- **source digest の窓**: `source_digest(..., since=..., through=...)` で period 窓を
  明示できる。差分同期で新しい period を足しても、凍結時の窓で再計算すれば
  Phase 7 の凍結 digest は動かない。

## 6. 推奨する運用形態

| アプローチ | トレードオフ |
|---|---|
| **A. ローカル PC で常時収集(推奨)** | データが最初からローカルにある。PC・ネットワーク・電源の維持が必要 |
| **B. 閉じた期間だけ手動同期** | 最も軽い。Vision には適するが、WebSocket の板・BBO・約定には永久欠損が残る |
| **C. 常時稼働のリモート収集機 + ローカル同期** | 自宅 PC 停止中も収集できる。運用・セキュリティ・同期の複雑度が増す |

**A + B の組み合わせ**を推奨する。ローカル PC で supervisor 越しに collector を常時
走らせ、日次で gate → normalize → manifest を実行する。並行して Vision の履歴 zip を
日次/月次で差分同期する。

高頻度の WebSocket 収集を毎時ごとに新しいタスクとして起動する方式は採らない
(開始遅延・接続の断片化・状態喪失があるため)。収集は決定的な常駐プロセス、
研究分析は必要なときに別プロセス、という分離を保つ。

### 常駐化の例(systemd user unit)

```ini
[Unit]
Description=Ango OKX microstructure collector supervisor

[Service]
WorkingDirectory=%h/ango
ExecStart=/usr/bin/env uv run python -m mce.collector_supervisor -- --inst-id BTC-USDT-SWAP
Restart=on-failure
# supervisor 自身が fail-closed した場合は再突入させない。人が理由を見てから戻す。
RestartPreventExitStatus=1
KillSignal=SIGTERM
TimeoutStopSec=60

[Install]
WantedBy=default.target
```

`KillSignal=SIGTERM` と十分な `TimeoutStopSec` は必須である。SIGKILL で落とすと
gzip footer を閉じられず、書きかけの `.partial` がそのまま残る。

日次ジョブ(timer / cron)は正規化と台帳更新だけを行う:

```sh
uv run python -m mce.daily_ingest
uv run python -m mce.binance_vision --through-latest-closed
```

## 7. この層に混ぜないもの

- 収集プロセスが strategy を選ぶ機能
- collector のログを見て閾値・特徴量・split を変更する機能
- quality failure を再接続や補間で「正常」に見せる機能
- raw を削除して容量を空ける自動 cleanup
- 収集器と自動売買を同じプロセス・同じ権限で動かす機能

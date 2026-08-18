# OKX microstructure live collection

BTC-USDT-SWAP の方向探索ではなく、OFI・板枯れ・吸収・執行コストを後から検証する
ための raw データを今から蓄積する。OKX は板と BBO の historical API を提供しないため、
欠損した過去を後から復元することはできない。

## 収集する feed

認証不要の production WebSocket を2接続使う。

| 接続 | URL | channel | 内容 |
|---|---|---|---|
| public | `wss://ws.okx.com:8443/ws/v5/public` | `trades` | taker注文・価格・source単位の集約約定。`count` / `seqId`あり |
| public | 同上 | `bbo-tbt` | 1段のfull snapshot。変化時、最速10ms |
| public | 同上 | `books` | 初回400段snapshot、その後100msのincremental update |
| public | 同上 | `instruments` (`instType=SWAP`) | contract・tick・lotの日中変更 |
| business | `wss://ws.okx.com:8443/ws/v5/business` | `trades-all` | 非集約の個別約定 |

public の4 channelは1本の接続で購読し、同じ raw stream に保存する。OKX が保証する
集約約定と板の逐次配信、および実際の受信順を維持するためである。`trades-all` は個別
約定を得るために必要だが別サービスなので、それ単独のlocal受信時刻を板との厳密な
順序とみなしてはならない。

subscribe例:

```json
{
  "id": "4a8652ec424041858a209874",
  "op": "subscribe",
  "args": [
    {"channel": "trades", "instId": "BTC-USDT-SWAP"},
    {"channel": "bbo-tbt", "instId": "BTC-USDT-SWAP"},
    {"channel": "books", "instId": "BTC-USDT-SWAP"},
    {"channel": "instruments", "instType": "SWAP"}
  ]
}
```

`books5` はBBO/400段板と重複するため使わない。10msの `books50-l2-tbt` と
`books-l2-tbt` はVIP/loginが必要なので、この認証不要collectorの対象外である。

公式仕様:

- [OKX API guide](https://www.okx.com/docs-v5/en/)
- [Market-data best practices](https://www.okx.com/docs-v5/trick_en/)
- [Order-book checksum deprecation](https://www.okx.com/en-us/help/okx-order-book-channels-checksum-field-deprecation)

## 実行

疎通確認と保存形式の確認にはdurationを指定する。

```sh
uv run python -m mce.collect_microstructure --duration 60
```

durationを省略すると SIGINT / SIGTERM まで継続する。

```sh
uv run python -m mce.collect_microstructure
```

主なオプション:

```text
--inst-id BTC-USDT-SWAP
--raw-dir data/raw
--duration SECONDS
--heartbeat-seconds 20
--pong-timeout-seconds 10
--subscribe-timeout-seconds 10
--reconnect-min-seconds 1
--reconnect-max-seconds 60
--session-rotation-seconds 3600
--clock-sample-seconds 60
--skip-instrument-metadata  # fixture専用。通常運用では使わない
--skip-clock-quality        # fixture専用。使うとT0評価不可
```

接続後、要求した全channelのsubscribe ACKが揃うまでsessionをready扱いしない。20秒
受信が無ければ文字列 `ping` を送り、10秒以内に `pong` が無ければ再接続する。通常の
切断、JSON不正、板sequence gap、service-upgrade notice `64008` は新しい接続へ移り、
指数backoff（full jitter、最大60秒）を挟む。subscribe errorは設定不良の可能性が高い
ためfail-fastする。ACK timeoutは一時障害として再接続する。rawの破損failure domainを
1時間に限定するため、readyから3600秒で計画再接続し、新sessionのsnapshotから再開する。

collectorはWS開始前と各00:00 UTCにREST instruments metadataをraw保存し、日中変更は
public `instruments` channelで受ける。RESTを初期state、WS pushを受信時点からの更新として扱う。
collector hostの時計品質は、起動時と以後60秒ごとにread-only Linux
`adjtimex(modes=0)` で別rawに保存する。これを取得できないhostではcollectorは
fail-closedする。

## raw stream

接続sessionごとに次のファイルを作る。

```text
data/raw/okx/ws/public/YYYY/MM/DD/<UTC-start>_<session>.jsonl.gz
data/raw/okx/ws/business/YYYY/MM/DD/<UTC-start>_<session>.jsonl.gz
data/raw/okx/rest/instruments/YYYY/MM/DD/<UTC>_<ns>_<inst>.jsonl.gz
data/raw/host/clock_quality/YYYY/MM/DD/<UTC>_<wall-ns>_<monotonic-ns>_<id>.jsonl.gz
```

WS書込み中のファイルは末尾が `.jsonl.gz.partial` で、downstreamへ公開しない。正常終了時に
gzip footerを閉じ、fileとdirectoryをfsyncしてから `.jsonl.gz` へatomic renameする。partialが
残っていればclean closeしなかった証拠であり、完全rawとして扱わない。

各JSONLレコード:

| 列 | 意味 |
|---|---|
| `schema_version` | raw wrapper schema。現在1 |
| `stream` | `public` / `business` |
| `session_id` | 再接続ごとに変わるUUID |
| `frame_no` | local/out/inすべてを含むsession内連番 |
| `direction` | `in` / `out` / `local` |
| `kind` | `frame`, `subscribe`, `ping`, `pong`, 接続eventなど |
| `received_at_ns` | local wall clockのUnix ns |
| `monotonic_ns` | clock補正の影響を受けないlocal順序時計 |
| `payload` | 受信・送信した文字列。parse前の空白も保持 |

subscribe ACK/error、ping/pong、接続・再接続理由も同じstreamへ書く。raw writeが失敗
した状態で受信だけを継続すると回復不能な「見えない欠損」になるため、write errorは
collector全体を停止させる。

## `books` sequence rule

`books` は接続ごとに次の状態機械で検証する。

1. `action=snapshot` は `prevSeqId=-1` でなければならず、これを最初の有効stateとする。
2. updateは `prevSeqId == 直前のseqId` の場合だけ適用可能とする。
3. `prevSeqId == seqId == 直前seqId` かつ asks/bidsが空なら、約60秒無更新時の正当な
   heartbeatである。
4. `prevSeqId == 直前seqId` かつ `seqId < prevSeqId` はmaintenance時の正当なsequence
   resetである。以後は小さい新seqを基準に継続する。
5. それ以外、またはsnapshot前のupdateはgap。現在stateを無効化し、接続を閉じて
   新しいsubscriptionのsnapshotから始める。

2026-06-23以降、`books` の `checksum` fieldは残るが値は常に0である。CRC32検証は
行わず、`seqId` / `prevSeqId` のみを使う。RESTの現在snapshotを途中のWS deltaへ
継ぎ足すこともしない。

## 分析上の限界

- `books` は100ms窓の純変化であり、窓内の A→B→A は配信されない。得られるのは
  market-by-priceの100ms net flowで、全注文のadd/cancel履歴ではない。
- top-of-bookは10msの `bbo-tbt` で補えるが、2段目以降の10ms変化はVIP feedなしでは
  観測できない。
- SWAPの `sz` はBTCでなく契約枚数。正規化時は起動時点のinstrument metadata
  (`ctVal`, `ctMult`, `ctValCcy`, `tickSz`, `lotSz`)を保存してbase量へ換算する。
- `source=1` のRPI約定はorganic BBO外で発生しうる。v1はorganic `books` を収集する
  ため、吸収分析ではRPI約定を分離する。
- exchange timestampはms、local受信はnsだが、別WebSocket間のlocal到着順はexchange
  の因果順を保証しない。hostの時計同期状態はperiodic `adjtimex` rawとlagで判定する。

rawは再取得不能なので削除せず、normalized/featureをrawから再生成する。最初の24時間で
channel別 message数、bytes/day、再接続、sequence gap、event timestampから受信までの
lagを測定してから容量計画を決める。

## 品質確認と正規化

closed rawのsoak reportはgzip/JSON、frame連番、ACK、lifecycle、channel件数、lag、板sequence、
約定照合を一括確認する。不正sessionを黙ってskipせず、report全体をinvalidにする。

```sh
uv run python -m mce.microstructure_quality data/raw/okx/ws \
  --output data/analysis/microstructure_quality.json
```

closed sessionだけをimmutable Parquet shardへ正規化する。

```sh
uv run python -m mce.normalize_microstructure \
  data/raw/okx/ws/public/YYYY/MM/DD/*.jsonl.gz \
  data/raw/okx/ws/business/YYYY/MM/DD/*.jsonl.gz \
  data/raw/okx/rest/instruments/YYYY/MM/DD/*.jsonl.gz
```

既定出力は `data/normalized/okx/microstructure/v3` 。`trades`、`bbo`、
`book_messages`、`book_levels`、`instrument_metadata`、`session_controls` を
normalized schema version / arrival UTC日/hourでpartitionする。REST初期metadataとWS更新は
同一schemaで `origin` を区別し、受信時刻から因果的に適用できる。全行にraw座標と
archive/logical SHA-256を残し、同じclosed rawの再実行は同じshardの内容を検証して再利用する。

## 常時収集の運用

上のCLIを手で回す代わりに、supervisorと日次ingestを使う。

```sh
# collectorのプロセス健全性を監督し、run ledgerを残す(fail-closedあり)
uv run python -m mce.collector_supervisor -- --inst-id BTC-USDT-SWAP

# closed rawをvalid/invalid/pendingへ判定し、validだけを正規化して収集日台帳を更新する
uv run python -m mce.daily_ingest
```

supervisorは板sequenceなど取引所データの内容を判断せず、exit code、signal、空き容量、
clock quality rawの有無だけを見る。連続異常終了・容量不足・clock品質欠落では再起動せず
fail-closedし、`data/analysis/alerts/` へ理由を残す。invalid sessionは削除せず
`data/quarantine/` へ隔離するため、normalizedには `valid` なsessionだけが入る。

運用層の詳細(判定条件、収集日manifest、health ledger、Binance Visionの差分同期)は
[local_collection_ops.md](local_collection_ops.md) を参照。

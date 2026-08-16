# Microstructure v1 — OFI・板枯れ・吸収の事前凍結プロトコル

- 凍結日: 2026-08-16
- 状態: **定義凍結・収集中・未検定**
- 対象: OKX `BTC-USDT-SWAP` のみ
- 目的: OHLCV方向探索の終了後、約定フロー・BBO・板に、表示流動性を実際に成行で
  取りに行っても残る短期方向エッジがあるかを、3仮説だけ一度確認する。
- 非目的: OHLCV条件の救済、maker約定の仮定、barrier探索、予測horizon探索、複数signalの
  組合せ探索、板から個々のadd/cancelを復元すること。

First-touch v1はValidationの主設定でgross +0.24bps、15bps後 −14.76bpsとなり棄却済みで、
OHLCVのみの方向探索は終了した。本v1はその結果を受けた別データ族のprospective検定であり、
first-touchや深いイベントv1の条件を特徴量へ持ち込まない。

## 1. 観測可能範囲と禁止する解釈

認証不要のproduction WebSocketを2接続で保存する。

| 接続 | channel | v1での用途 |
|---|---|---|
| public | `trades` | organic aggressive flow。板と同一接続内の受信順を使う |
| public | `bbo-tbt` | 最良気配とL1数量。変化時のfull snapshot、最速10ms |
| public | `books` | 400段snapshot + 100ms incremental update。10bps depthを再構築 |
| public | `instruments` (`instType=SWAP`) | REST初期state後の日中contract/tick/lot変更を保存 |
| business | `trades-all` | 個別約定の件数・数量照合だけに使う |

次を解析全体の制約とする。

1. publicの4 channelは同じ接続の `frame_no` 順に適用する。特徴量の時点はexchange timestamp
   ではなく、collectorが実際に観測できた `received_at_ns` とする。
2. `trades-all` は別接続なので、板との前後関係、signal生成、public feedの穴埋めには使わない。
3. `books` は100ms内の純変化である。板枯れは **10bps表示depthのnet depletion proxy** と呼び、
   cancel、約定、replenishmentを個別注文単位へ分解したとは主張しない。
4. `bbo-tbt` より深い10ms板は観測しない。VIP専用の `books50-l2-tbt` / `books-l2-tbt` を
   後から混ぜない。
5. `source=1` のRPI約定はorganic板の外で起こりうるため、主signalから除外する。RPI比率は
   記述統計として別掲するが、主結果の救済には使わない。
6. 表示板は約定保証でもqueue positionでもない。本v1は固定サイズを表示板へ当てた保守的な
   executable proxyであり、実発注の採用判定ではない。

## 2. 収集期間と時系列split

### 2.1 起点

collector起動後、まず連続24時間をsoak期間とする。この間にchannel別件数、保存量、再接続、
sequence gap、lag、時刻同期を確認し、signal/returnは計算しない。全channelの購読ACK、instrument
metadata、下記のraw integrityを確認できた後、最初の00:00 UTCを `T0` としてmanifestへ一度だけ
記録する。結果を見て `T0` を移動しない。

起点後の窓は次で固定する。

| 窓 | 期間 | 用途 |
|---|---|---|
| Calibration | `[T0, T0+60日)` | 無ラベルの分布quantileだけを固定。将来returnを生成しない |
| Validation | `[T0+60日, T0+120日)` | 3主仮説の一回目の検定 |
| Final | Validation判定後に登録する `TF` から60日 | Validation通過仮説だけのprospective追試 |

- 最初のラベル検定までの最低収集期間は、soakを除いて**連続120暦日**。
- `TF` はValidation reportと通過仮説一覧のhashを保存した**後**、最初に来る00:00 UTCとする。
  したがって完全確認には最低180暦日超が必要で、ValidationとFinalは重ならない。
- Validation不通過仮説についてFinal特徴量・損益を計算しない。3仮説全て不通過ならFinal窓を
  開かない。raw収集は判定と無関係に継続してよい。
- eventの60秒warmupとentryが同一splitかつ同一の有効public session内に入る場合だけ候補にする。
  split境界とhourly計画rotationは事前に時刻が既知なので、exit予定時刻がそれらを跨ぐ候補は
  `q` 時点で除外する。entry後の予期しない切断・gapを見て事後除外してはならず、下記の
  execution-model failureとして扱う。
- 運用障害がquality gateを落とした場合、後ろの日を足して同じv1を延長しない。ラベル未閲覧なら
  原因修正後に新しい起点を事前登録し、別manifestでやり直す。ラベル閲覧後なら当該v1は終了する。

## 3. raw・正規化のquality gate

quality reportを先に確定し、**全項目通過前にsplitの将来returnを生成してはならない**。
Calibration、Validation、実施する場合のFinalで個別に判定する。

### 3.1 raw integrity

1. 全gzipがEOFまで展開でき、全行がJSONとして読める。wrapperの `schema_version=1`、stream、
   session_idが一貫し、各sessionの `frame_no` がlocal/out/inを含め0から1ずつ増えること。
2. sessionは対象channelすべてのsubscribe ACKと `subscriptions_ready` 後だけ有効。subscribe error、
   raw write error、解釈不能なframeを黙って捨てたsessionは使用不可。
3. `books` は `prevSeqId=-1` のsnapshotから開始し、各updateの `prevSeqId` が直前の有効
   `seqId` と一致すること。空の同一seq heartbeatと仕様どおりのmaintenance resetだけを許可する。
   gap後は即無効とし、再接続後の新snapshotまで板を継ぎ足さない。REST snapshotをWS deltaへ
   接続しない。checksumは廃止後常時0なので判定に使わない。
4. 公開feedのready wall-timeはsplit全体で99.5%以上、各UTC日で98%以上。businessはsplit全体
   98%以上、各日95%以上。無効区間と、新snapshotで再開した後の60秒warmupからsignalを
   作らない。予期しない無効化より前の60秒を事後に消してはならず、既にentryしたeventは
   execution-model failure規則に従う。
5. 有効な1秒decision gridがsplitの95%以上残ること。単一UTC日、週末、13–15時UTCを恣意的に
   落としてこの値を満たすことは禁止する。

### 3.2 clock・cross-feed整合性

1. collector起動時と以後60秒ごとに、Linux kernelのread-only `adjtimex(modes=0)` から
   `state` / `status` / `offset` / `maxerror` / `esterror` / `precision` をraw保存する。
   `TIME_ERROR` でなく `STA_UNSYNC` も立っていないsampleが100%、予定60秒gridに対する
   欠測が1%未満、絶対offsetのp99が20ms以下、最大が100ms以下であること。
   `maxerror` / `esterror` は選別なしでreportする。clock観測が使えないhostでは
   collectorをfail-closedとし、`T0` を開始しない。
2. channelごとの `received_at - exchange_ts` はp99が500ms以下、p99.9が2秒以下。負lagが
   −100ms未満のframeはclock異常としてsplit不通過にする。
3. 各整数秒で最新のorganic BBOと再構築 `books` のL1価格を比較し、双方が有効な時点の98%以上で
   bid/askが一致すること。不一致秒はsignal不可とする。
4. public `trades` の `count` 合計・契約数量とbusiness `trades-all` の行数・契約数量を、両接続が
   readyな区間だけUTC日単位で照合する。相対差0.5%以内の日が各splitで55/60日以上必要。
   別接続間の受信順一致は要求しない。
5. crossed/locked organic book、非正価格・数量、tick/lot違反は無効。該当decision秒が0.01%を超える
   splitは不通過とする。重複tradeの自然keyと重複除去数を全件報告する。

### 3.3 instrument metadataと単位

起動時および各UTC日にREST instrument metadata (`ctVal`, `ctMult`, `ctValCcy`, `tickSz`, `lotSz`,
contract type)をraw保存し、同じpublic接続の `instruments` channelで日中変更も受ける。RESTを
canonical initial state、WS pushを受信時点からのeffective updateとする。metadata欠落・仕様変更時は
新metadata取得まで無効区間とする。

```text
ctValCcyがBTC:   base_qty = sz × ctVal × ctMult
ctValCcyがUSDT:  base_qty = sz × ctVal × ctMult / price
USDT notional:   base_qty × price
```

契約枚数・BTC数量・USDT notionalを全て保持する。上記で表現できないcontract仕様へ変わった場合は
勝手に換算せずv1を停止する。

## 4. 因果的な共通時点・板状態

decision時点 `q` は `T0` を原点とする整数UTC秒。特徴量には `received_at_ns < q` のpublic frame
だけを使い、同一timestampなら `frame_no` 順に適用する。split内で接続が変わった場合は、以前の
板・rolling windowを引き継がない。

- `mid(q) = (best_bid(q)+best_ask(q))/2`
- organic tradeのsideは buy=`+1`、sell=`−1`。
- trade notionalは約定価格でUSDT換算する。
- depthはsnapshotから価格別数量を再構築し、quantity=0のlevelを削除する。
- 10bps bid depthは `[mid×(1−0.001), mid]`、ask depthは `[mid, mid×(1+0.001)]` の
  表示USDT notional合計。
- rolling区間は左閉右開 `[q−w,q)`。ちょうど `q` に受信したframeは次のdecisionへ回す。

各特徴量のCalibration 95%点は、UTC hour 24区分 × weekday/weekend 2区分の48 strataごとに、
ラベルを使わず固定する。95%点は昇順の `ceil(0.95×n)` 番目(nearest-rank、補間なし)。各stratumに
40,000以上の有効秒が無ければCalibration不通過。quantile表と入力raw/code hashをmanifestへ保存し、
Validation/Finalで更新しない。

quantile母集団は、M1=`|OFI10|` が定義できる全有効秒、M2=`|DEP|` が定義できる全有効秒
（`max(x_a,x_b)>=0.50` で事前filterしない）、M3=`V10` が定義できる全有効秒とする。eventの
追加条件はquantile固定後に適用する。

## 5. 固定する3主仮説

候補は次の3つだけ。threshold、window、方向、horizonのgrid searchを行わない。long/shortは別候補に
分けず、signalが指定するsigned tradeを1本の両側仮説として検定する。

### M1: L1 OFI continuation

連続する `bbo-tbt` snapshot `j−1 → j` について、base quantityを `B` / `A`、価格を
`P^b` / `P^a` とし、標準的なL1 OFI incrementを次で定義する。

```text
e_j = 1[P^b_j >= P^b_{j-1}] B_j - 1[P^b_j <= P^b_{j-1}] B_{j-1}
    - 1[P^a_j <= P^a_{j-1}] A_j + 1[P^a_j >= P^a_{j-1}] A_{j-1}

OFI10(q) = sum(e_j, received_at in [q-10s,q))
           / median(B+A, bbo snapshots received in [q-60s,q))
```

分母が0、snapshotが2件未満、60秒warmup不完備なら無効。`|OFI10|` が該当stratumのCalibration
95%点以上でevent。`sign(OFI10)` 方向へ入り、固定horizon **30秒**のcontinuationを検定する。

### M2: 10bps book net-depletion continuation

整数秒ごとの10bps depthを `D_b(q)`, `D_a(q)` とする。baselineは `q−60s` から `q−5s` までの
56個の整数秒stateのmedianで、56個全てが有効な場合だけ計算する。

```text
x_b(q) = 1 - D_b(q) / median(D_b(q-60), ..., D_b(q-5))
x_a(q) = 1 - D_a(q) / median(D_a(q-60), ..., D_a(q-5))
DEP(q) = x_a(q) - x_b(q)
```

`max(x_a,x_b) >= 0.50` かつ `|DEP|` が該当stratumのCalibration 95%点以上でevent。ask側枯れ
(`DEP>0`)はlong、bid側枯れ(`DEP<0`)はshort、固定horizon **30秒**。これは100ms net depthの
continuation仮説であり、cancel原因の仮説ではない。

### M3: organic aggressive-flow absorption reversal

organic public tradesについて10秒のsigned notional `A10` と総notional `V10` を作る。

```text
A10(q) = sum(side_i × notional_i, received_at in [q-10s,q))
V10(q) = sum(notional_i,          received_at in [q-10s,q))
u(q)   = sign(A10(q))
r10(q) = log(mid(q) / mid(q-10s)) × 10,000
```

次を全て満たすときだけabsorption eventとする。

1. `V10` が該当stratumのCalibration 95%点以上
2. `|A10| / V10 >= 0.70`
3. `|r10| <= 1.0bps`
4. aggressive buyならask 10bps depth、aggressive sellならbid 10bps depthが、
   `q` で `q−10s` の80%以上まで残っている

方向はaggressive flowの逆 (`−u`)。固定horizon **60秒**のreversalを検定する。RPI約定、
`trades-all` の到着順、将来のreplenishmentはsignalへ使わない。

## 6. event採用と固定執行モデル

各仮説を独立に時刻順走査し、条件を満たす最初のeventを採用する。同一仮説では採用 `q` から
horizon終了までの後続eventを捨て、`q_next >= q+H+1秒` から再開する。future outcomeを見ない
greedy cooldownであり、仮説間の重複は許す。組合せ損益はv1で計算しない。

### 6.1 発注時点とサイズ

- signal確定 `q` から固定250msのdecision/transport latencyを置く。entry stateは
  `received_at_ns < e=q+250ms` の最新BBO/板、exit stateは
  `received_at_ns < x=q+H+250ms` の最新BBO/板とする。境界と同値のframeは次のstateへ回し、
  未来frameを待って価格を選ばない。
- 固定entry notionalは **10,000 USDT**。entry側400段を浅い順に消費し、表示約定notionalが
  10,000 USDT以下となる**最大のlot-multiple契約数**を選ぶ。同じ契約数をexitし、VWAPと
  契約数の相互依存を解消する。
- longはentryでaskを、exitでbidを浅いlevelから順に消費する。shortはentryでbidを、exitでaskを
  消費し、400段organic板のVWAPを使う。spreadと表示market impactはこの時点でgrossに入る。
- entry時に必要数量を400段で満たせなければ、観測可能な非流動性filterとしてtradeしない。
  exit時に満たせないeventが1件でもあれば、その仮説・splitは執行モデル不成立でpass不可とし、
  都合よくeventを除外しない。
- entry時点でBBOとbooks L1価格が不一致、session/gap/metadataが無効ならeventを採らない。これは
  `e` までの情報だけで決める。entry成立後に予期しない切断、sequence gap、metadata無効化、
  BBO-books不一致、exit state欠落が1件でも起きた場合、当該eventを消さず、その仮説・splitを
  **execution-model failureでpass不可**とする。予定rotation/split境界を跨ぐ候補の事前除外だけを
  例外とする。

### 6.2 損益とコスト

long/shortの表示板VWAPによるfee前log PnLは次とする。

```text
long:  gross_bps = log(exit_bid_vwap / entry_ask_vwap) × 10,000
short: gross_bps = log(entry_bid_vwap / exit_ask_vwap) × 10,000
```

- 10bps: taker 5bps/sideの通常Lv1 fee想定
- 12bps: fee + slippage 1bp/side
- **主判定: max(15bps, 実口座往復taker fee + 5bps)** をgrossから控除

15bps基準なら、表示spread・表示impactに加えて2.5bps/sideの未観測slippage stressを負う。
口座・地域tierで実feeが高い場合は高い方を使う。低fee、maker rebate、post-only、RPI改善での救済は
禁止。保有は最大60秒なのでfundingは0とする。

補助として同じ時点のsigned mid markoutを報告するが、採否は表示板VWAPの主コスト後損益だけで
決める。TP/SL、途中MFE、最良exitの探索は行わない。

## 7. 固定する統計手順

各仮説・splitで、n、long/short、active UTC日、weekday/weekend、UTC hour、gross、10/12/主コスト後
mean/median、勝率、profit factor、日次損益、6個の固定10日blockを選別なしで報告する。

- eventのcluster日は `q` のUTC日。splitの全60日（日次表示ではzero-event日も0として保持）を
  報告し、推論ではeventを含むUTC日のeventを丸ごと保持して20,000回のday-cluster bootstrapを
  行い、event-weighted meanの片側95%下限と両側95% percentile CIを出す。
- one-sided p値は、帰無平均0の下でUTC日ごとのPnL合計へ同じRademacher符号を掛ける20,000回の
  cluster randomizationで計算し、`p=(1+#(null>=observed))/(20,001)` のadd-one規則を使う。
  bootstrapも同じseedを使い、Validation seed=`20260816`、Final seed=`20260817`。
- 検定族はM1/M2/M3の3つで固定。各窓でone-sided p値へHolm補正し、family-wise alpha=0.05。
  N不足・Validation不通過・未開封の仮説も `p=1` としてfamily sizeを3のまま保つ。
- bootstrap/randomizationの実装、raw/normalization/feature/labelコード、依存lockfileを
  Validation前にhash化する。結果閲覧後のseed・cluster・検定変更は禁止。

探索的な別horizon、threshold、片側だけ、時間帯だけ、weekend除外、RPI込み、`trades-all`版、
maker版はv1 reportで計算しない。signal間相関とRPI比率はラベルを伴わない記述統計だけ許可する。

## 8. 事前pass条件

各仮説を個別に判定する。Validationで次を全て満たした仮説だけFinalへ進む。

1. splitの全quality gate通過
2. `n >= 1,000`、long/short各 `>=300`、eventを含むUTC日 `>=50`、weekend日 `>=12`
3. 主コスト後mean `>0`、day-cluster片側95%下限 `>0`、Holm補正p `<=0.05`
4. long、short、weekday、weekendの主コスト後meanがそれぞれ `>0`
5. 固定10日blockのうち5/6以上で主コスト後mean `>0`、かつ各blockを1つずつ除いた6通りの
   leave-one-block-out meanが全て `>0`
6. exitで400段不足が0件

Finalでも同じ1–6を全て要求し、さらに次を要求する。

7. 主コスト後meanの符号が一致し、Final meanがValidation meanの50%以上

ValidationとFinalを通過して初めて **provisional survivor**。表示板proxyと単一取引所・短期間の
制約があるため、それだけで実運用採用とはしない。小額paper/live shadowで実fill、reject、latency、
slippageを別途確認する。

## 9. kill criteriaと再探索の境界

- quality gate不通過時は「edgeなし」ではなく**検定無効**。同じ壊れた窓の欠損補間・REST補完・
  良い日だけの抽出はしない。
- sample gate不通過、Validationの2–6いずれか不通過、Finalの2–7いずれか不通過は、その仮説の
  **v1棄却**。threshold、horizon、cooldown、方向、サイズ、コストを緩めずFinal/再追試へ進めない。
- 10bpsや12bpsではプラスでも主コスト15bpsで不通過なら棄却。maker、RPI、時間帯限定、片側限定、
  signal合成で救済しない。
- 1日・1方向・weekdayだけに依存して共通gateを落とす結果は、その局面を理由に昇格させない。
- Validation通過後Final落ちならv1を終了し、同じFinal dataでv2を作らない。新仮説は全結果を公開し、
  定義を別versionとして凍結し、その**後に始まる**prospective windowでのみ検定する。
- 3仮説が全て棄却されても、raw収集とvol/liquidity/執行品質の記述研究は継続してよい。ただし、
  それを理由にOHLCV方向探索を再開しない。

## 10. 事前成果物

Validationを開く前に、少なくとも次を機械生成して保存する。

1. `T0`、split境界、instrument metadata履歴、WS/REST/host clock raw file一覧とSHA-256
2. channel/session別ACK・uptime・bytes・frame・gap・lag・clock quality report
3. normalization schema、重複・RPI・public/business照合report
4. 48 strata × 3特徴量のCalibration 95% threshold表
5. protocol、実行code、依存lockfileのcommit/hashと固定random seed
6. labelを含まない仮説別予定event件数、long/short・日・時間帯分布

Validation/Final reportは、全候補・全gate・未開封候補を含むmanifestを出し、手作業で表から候補を
消さない。主要値は凍結文書とrawだけを渡した独立実装で再計算する。

trade重複のnatural keyは `(channel, instId, source, tradeId)` とする。同一key・同一payloadは
raw座標を残して重複扱い、同一key・異内容はquality failureとする。`trades` と `trades-all` は
channelが異なるため互いを重複除去せず、日次照合だけを行う。

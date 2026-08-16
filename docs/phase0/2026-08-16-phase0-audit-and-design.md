# Phase 0 監査・設計レポート — Deterministic Judge

- 作成日: 2026-08-16
- 対象: [ROADMAP.md](../../ROADMAP.md)(BTC Alpha Discovery Research Roadmap)の Phase 0
- 前提コミット: `46ea7f4`(Market Condition Explorer PoC、branch `claude/market-condition-explorer-poc-383slf`)
- スコープ: **調査・Gap Analysis・Phase 0 設計のみ**。本レポートではコードを変更していない
  (追加したのは ROADMAP.md の収録と本レポートのみ)。

---

## 1. Current Repository Audit

### 1.1 Directory tree

```text
ango/
├── README.md                  … PoC の構成・スキーマ・運用手順(設計ルール含む)
├── ROADMAP.md                 … 本タスクで収録した上位設計(添付ファイルの原文)
├── pyproject.toml             … 依存: httpx / polars / duckdb、dev: pytest
├── uv.lock
├── .gitignore                 … data/ を全て除外(データはローカル専用)
├── docs/
│   ├── data_sources.md        … 取引所 API 比較と OKX 採用理由(疎通実測付き)
│   └── findings/              … 検証済み結論の台帳(凍結仮説・恒久ルール)
│       ├── README.md
│       ├── 2026-08-16-5m-tendencies-33mo-retest.md
│       └── 2026-08-16-regime-classifier-v1-spec.md
├── src/mce/
│   ├── config.py              … パス・対象銘柄(BTC-USDT-SWAP 5m)・source/market_type 定数
│   ├── okx.py                 … OKX v5 public API クライアント(rate limit / retry)
│   ├── ingest.py              … 取得 CLI(冪等・差分再開・バックフィル・raw 保存)
│   ├── normalize.py           … OKX 形式 → 共通スキーマ(UTC ms、未確定足除外)
│   ├── store.py               … raw JSONL(gzip 追記)/ normalized Parquet マージ(重複排除)
│   ├── features.py            … features Parquet 生成(過去・**先読み**リターン、funding/OI 結合)
│   └── report.py              … DuckDB による件数・期間・欠損サマリ
└── tests/                     … 19 テスト(全て成功を確認済み)
    ├── test_normalize.py      … スキーマ / UTC / 未確定足除外
    ├── test_store.py          … マージ冪等性 / 重複排除 / ts_range
    ├── test_ingest.py         … ページング停止判定(差分・バックフィル・API下限)
    └── test_features.py       … 欠損バー安全性 / 窓の完全性 / as-of tolerance / 20d系
```

data/ 配下(gitignore 済み・ユーザーのローカルにのみ存在)は README と findings から:

- `data/raw/{okx,binance}/…`(JSONL gzip / Binance Vision 月次 zip 原本)
- `data/normalized/ohlcv/okx_BTC-USDT-SWAP_5m.parquet`(2023-11-19〜、約 28.8 万行)
- `data/normalized/funding_rate/{okx_BTC-USDT-SWAP, binance_BTCUSDT}.parquet`
- `data/normalized/open_interest/okx_BTC-USDT-SWAP_5m.parquet`(直近約5日のみ)
- `data/features/okx_BTC-USDT-SWAP_5m.parquet`

### 1.2 Module 責務と data flow

```text
OKX v5 public API
   │  okx.py        HTTP・rate limit・retry のみ。解釈はしない
   ▼
ingest.py           CLI。ページング制御・差分再開・バックフィル
   │                取得した生レスポンスは store.append_raw で必ず raw に残す
   ▼
data/raw/           JSONL gzip、追記のみ(immutable。重複許容)
   │  normalize.py  OKX 固有形式 → 共通スキーマ。未確定足(confirm != "1")除外
   ▼
data/normalized/    Parquet。store.merge_parquet が (source, symbol, ts) で重複排除
   │                tmp → rename の原子的書き込み。欠損は補間しない
   ▼
features.py         normalized から全再生成(決定的・冪等)
   ▼
data/features/      1ファイルに observable 特徴量と fwd_return_* が同居 ← 分離対象
   │
   ▼
report.py / DuckDB  集計・欠損検出・Historical Condition Search(SQL)
```

### 1.3 Feature flow(features.py の計算内容)

| 列 | 計算方法 | 時点整合性の性質 |
|---|---|---|
| `return_5m`, `return_1h` | ts 完全一致 join(行シフトではない) | 基準バー欠損なら null。close[t] 時点で確定 |
| `volume_ratio_20` | rolling_sum_by 窓 `[ts−100m, ts)` closed="left"、ちょうど20本必須 | 分母は過去のみ。分子(当バー volume)は close[t] で確定 |
| `fwd_return_5m/1h/4h` | **未来の close** との ts 一致 join | **ラベル。observable ではない** |
| `drift_20d` | close / close(ts−20d) − 1、ts 一致 join | close[t] で確定 |
| `realized_vol_20d` | return_5m の rolling_std_by 窓 `[ts−20d, ts)` closed="left"、90% カバレッジ必須 | 過去バーのみ使用 |
| `funding_rate` | as-of join(backward、tolerance 9h) | ts(バー開始時刻)以前に決済確定した直近値のみ |
| `oi`, `oi_usd` | ts 完全一致 join | スナップショット時刻=ts と仮定 |

### 1.4 Test coverage

19 テスト・全成功(本環境で `uv run pytest` を実行して確認)。

- カバー済み: normalize のスキーマ/UTC/未確定足、store の冪等マージ、ingest のページング
  停止判定(無限ループ回避含む)、features の欠損バー安全性・窓完全性・as-of tolerance
- 未カバー: report.py(表示のみ)、okx.py の HTTP 層(ネットワーク依存)、
  そして backtest/execution/cost/metrics は**存在しない**(Phase 0 の対象)

### 1.5 Dependencies

httpx / polars / duckdb + pytest のみ。ROADMAP の「Python + Parquet + DuckDB の軽量構成」
にすでに一致しており、Phase 0 で新規依存はほぼ不要(必要になり得るのは YAML 用の
`pyyaml` 程度。JSON にすればゼロ)。

### 1.6 現状の良い設計(Phase 0 でそのまま土台にできるもの)

1. **raw / normalized の分離と raw の追記専用性** — ROADMAP の "immutable raw data" を
   ほぼ満たしている。正規化バグはやり直せる。
2. **UTC ms・provenance 列(symbol / source / market_type)** — Data Contract の中核が
   すでにある。Binance funding 代理データも source="binance" で分離済み。
3. **ts 完全一致 join によるリターン計算** — 欠損バーを跨いだ誤リターンが構造的に
   発生しない。ROADMAP の Missing Bar Test はこの設計の追認になる。
4. **rolling 窓が closed="left" + 完全性required** — 現在バー混入・部分窓の暗黙容認がない。
5. **冪等マージ・原子的書き込み・差分再開** — 再現性の基礎。
6. **欠損を補間しない**(検出のみ)。
7. **docs/findings の研究衛生** — 凍結仮説・OOS 機械判定・実効N補正・オラクル上限・
   独立監査という運用が既に確立している。これは ROADMAP の
   experiment artifact / audit の**手動プロトタイプ**であり、Phase 0 はこれを
   コード化する作業と位置づけられる。

### 1.7 現状の技術的負債

1. **observable と label の同居**(最重要) — `data/features/*.parquet` 1ファイルに
   `fwd_return_*` が observable 特徴量と並んでいる。条件検索用途では正しい設計だが、
   backtester から見ると look-ahead leakage の温床。→ §3, §4 で対策。
2. **available_time の概念が暗黙的** — バー ts はバー**開始**時刻で、close は ts+5m まで
   確定しない。この規約は README にも書かれていない。close[t] 起点の feature に
   ts(開始時刻)が付いているため、「行 t の値はいつ知り得たか」が読み手の解釈に
   依存する。→ §3.7。
3. **manifest 不在** — processed data のハッシュ・行数・期間・欠損数を記録する仕組みが
   なく、実験 artifact から「どのデータで評価したか」を機械的に固定できない。
4. **normalized は in-place 更新** — マージのたびに同一パスを書き換えるため、
   過去の実験時点のデータ状態を後から特定できない(manifest ハッシュで解決可能。
   ファイル自体のバージョニングは PoC には過剰)。
5. **report.py の 5分リターンが lag(行シフト)** — 欠損バー非対応。表示専用なので実害は
   ないが、features 層の規約と不整合。backtester では絶対に流用しないこと。
6. **backtest 層が完全に不在** — execution / cost / metrics / split / artifact は全て未実装。
7. 軽微: `.gitignore` が data/ を丸ごと除外しており、小さな manifest も置き場がない。

---

## 2. ROADMAP Gap Analysis

| ROADMAP requirement | Current status | Gap | Priority | Proposed change |
|---|---|---|---|---|
| **Data Contract** | 実質7割ある: UTC ms・provenance 列・重複排除・欠損非補間・raw/normalized 分離・未確定足除外 | 契約が README 記述のみでコード化されていない。バー ts=開始時刻という時刻規約が未文書化。manifest なし | **P0** | `docs/data_contract.md` に契約を明文化(特に時刻規約)+ `mce/manifest.py` で normalized/features/labels のハッシュ・期間・欠損数を記録 |
| **Observable Feature separation** | 単一 features ファイルに observable と fwd_* が同居 | 物理分離なし。loader guard なし | **P0** | features を `data/features/`(observable のみ)と `data/labels/`(fwd_*)へ物理分離。backtest 側 loader は features のみ読み、`fwd_` 接頭辞列を検出したら例外 |
| **label / forward-return separation** | fwd_return_5m/1h/4h は生成箇所が features.py 内で明確(3列のみ) | 同上。また命名規約(label は fwd_ 接頭辞)が暗黙 | **P0** | `mce/labels.py` に生成を隔離。命名規約を contract に明記。「label は評価専用」を loader レベルで強制 |
| **Temporal Integrity** | 構造的にはかなり良い: ts-join・closed="left"・as-of backward。ただし「行 t の feature は close[t] まで使えない」が暗黙 | reference_time / available_time が概念として存在しない。Future Mutation Test なし | **P0** | 列ごとの available_time をスキーマ表(contract 文書+コード内定数)で宣言(§3.7 の案)。observable 全列に対する Future Mutation Test を追加 |
| **Execution** | なし(PoC は「シグナル生成は目的ではない」と明言) | signal at close[t] → fill at open[t+1] の実装が丸ごと不足 | **P0** | `mce/backtest/execution.py`: long/short/flat の position 状態機械、next-bar open fill、holding period、欠損バー時の fill 規則(§7 Q2) |
| **Transaction Cost** | findings に「OKX taker 5bps/片道」という運用基準があるのみ | コスト model なし。fee/spread/slippage/funding の分離なし | **P0** | `mce/backtest/costs.py`: bps 建て成分(fee+spread+slippage)を config で分離、シナリオ(base/low/stress)と break-even cost 算出。funding は当面 PnL 外(§7 Q4) |
| **Backtester** | なし | engine 全体が不足 | **P0** | `mce/backtest/engine.py`: observable features + strategy(固定 baseline)→ signal → execution → cost → 結果。乱数は明示 seed のみ |
| **Metrics** | findings で手動計算(bps/件、t 値、実効N)の運用実績あり | コード化ゼロ | **P0** | `mce/backtest/metrics.py`。初期必須: total/annualized return, Sharpe, MaxDD, turnover, trade count, hit rate, exposure, break-even cost。同時実装が安い: Sortino, profit factor。後回し: DSR/PBO/SPA(Phase 5) |
| **Experiment artifact** | docs/findings の Markdown 台帳として手動運用 | 機械可読 artifact(config・manifest hash・commit hash・seed・結果)なし | **P0** | `mce/experiments.py`: run ごとに JSON を `experiments/runs/` へ保存。findings 台帳は「人間向け結論」として併存 |
| **Research / Validation split** | findings で in-sample/OOS を日付で手動運用(凍結規律あり) | split がコード化されていない。loader 強制なし | **P0** | `mce/backtest/splits.py` + リポジトリにコミットする split 定義ファイル。loader が split 名でしかデータを渡さない |
| **Sealed OOS readiness** | なし(概念は findings の恒久ルールに萌芽あり) | sealed 区間の定義・アクセス遮断なし | **P0**(定義のみ)/ 封印実施は Phase 1 終了時 | split 定義に `final_oos` を宣言し、通常 loader からは読めない構造にする。評価器の実行は Phase 6 まで封印 |
| **Audit readiness** | 手動監査(独立エージェント再計算)の実績あり。恒久ルール文書あり | temporal integrity の自動テストなし。DSR/PBO/SPA なし(これは後続 Phase で正しい) | **P0**(テスト)/ P3(統計監査) | Phase 0 では §6 の6テスト(Future Mutation / Execution Delay / Cost Monotonicity / Determinism / Missing Bar / Forward Leakage)を実装。統計監査は Phase 5 で追加 |

ROADMAP Phase 0 の feature リストと既存実装の差分:

| ROADMAP feature | 既存 | 備考 |
|---|---|---|
| return / rolling return | ✅ `return_5m`, `return_1h` | 追加ホライズンは window パラメータ化で対応 |
| realized volatility | ✅ `realized_vol_20d` | 短期窓(例: 1h/4h)版は未実装 |
| range | ❌ | high−low 系。H6 検証で SQL 直書きの実績はある |
| ATR | ❌ | 未実装 |
| volume z-score | △ `volume_ratio_20`(比率であり z-score ではない) | z-score 版は未実装。比率版も observable として有効なので両方残してよい |
| moving-average slope | ❌ | 未実装 |
| clock phase | ❌ | minute_mod_15 等。Phase 1B の前提。実装は軽い |

**方針: Phase 0 では feature の網羅は不要**(ROADMAP も「最初は少数に限定」)。分離と
時刻規約を先に固め、不足 feature は Phase 1A/1B が必要とする時点で追加する。

---

## 3. Leakage Risk Audit

判定: **Safe** / **Potential risk** / **Unsafe**(strategy feature として参照された場合)

| # | 項目 | 判定 | 根拠と対策 |
|---|---|---|---|
| 3.1 | `fwd_return_5m/1h/4h` | **Unsafe** | 定義上、未来の close を参照するラベル。生成箇所は `features.py` の `close_*m_later` join(1箇所に集中しており隔離は容易)。参照可能箇所は features Parquet を読む全経路(README の SQL 例、findings の検証 SQL)。**対策**: `data/labels/` へ物理分離し、backtest loader は features のみ読む+`fwd_` 列検出で例外。評価・条件検索は features ⋈ labels の明示 join でのみ可能にする |
| 3.2 | `return_5m`, `return_1h`(過去リターン) | **Safe** | ts 一致 join・基準バー欠損で null。close[t] 確定値。available_time = close[t] の規約下で問題なし |
| 3.3 | `volume_ratio_20` | **Safe** | 分母窓 `[ts−100m, ts)` closed="left" は現在バーを含まず、20本完全時のみ有効。分子は当バー volume(close[t] 確定) |
| 3.4 | `drift_20d` / `realized_vol_20d` | **Safe** | drift は close[t] と close[t−20d](ts-join)。vol 窓は closed="left" + 90% カバレッジ必須 |
| 3.5 | Funding as-of join | **Potential risk** | backward + tolerance 9h で「ts 以前に決済確定した直近値」のみを使うため方向は安全(むしろ ts=バー開始時刻に対する join なので5分ぶん保守的)。リスクは (a) fundingTime の意味(決済時刻)への依存が暗黙、(b) 境界 fundingTime == ts の equality 込み(決済済みなので実害なし、要文書化)、(c) **Binance 代理 funding**(source="binance")を OKX 執行の PnL に使うと水準・クランプ差で歪む(findings が既に注意書き済み)。**対策**: contract に fundingTime 意味論を明記。backtest の cost/PnL には当面 funding を入れない(§7 Q4)。使う場合は source を artifact に記録 |
| 3.6 | OI 一致 join | **Potential risk** | rubik 統計エンドポイントのスナップショット公開遅延が未検証(ts 時点の OI が ts に取得可能だったかは API 実測が必要)。さらに遡及約5日のため、歴史的バックテストには実質使えない(cron 蓄積が前提)。**対策**: Phase 0 では OI を observable feature に昇格させない(蓄積と遅延実測が済むまで raw/normalized 保持のみ)。ROADMAP でも funding/OI interaction は Phase 7 |
| 3.7 | timestamp 規約(バー ts の意味) | **Potential risk** | 最大の暗黙仮定。OKX candle ts は**バー開始時刻**であり、行 t の close・volume は ts+5m まで観測不能。現状は「探索用」なので問題化していないが、backtester が「行 t の feature を時刻 ts に使える」と誤解すると 5 分の先読みになる。**対策(available_time の実装レベル、§4.4)**: 全行に物理列を持たせるのではなく、(1) contract 文書に「バー ts=開始時刻、observable 列の available_time = ts + bar_duration(= close 時刻)」を明記、(2) コード上は feature スキーマ定数に列→availability 種別(`close_of_bar` / `start_of_bar`)を宣言、(3) execution engine は「signal 行 t → fill は ts_t + 5m を開始時刻とするバーの open」として availability を消費する実装にする。列単位の物理 available_time 列は、availability が行内で一様でなくなったとき(例: aggTrades 由来 feature)に導入する |
| 3.8 | missing bars | **Safe**(features)/ **Potential risk**(execution・report) | features 層は ts-join で安全。**report.py の lag ベース 5分リターンは欠損非対応**(表示専用に限定し、backtest で流用禁止)。execution では「t+1 バー欠損時の fill」を未定義のまま実装すると危険 → §7 Q2 で規則を先に決める |
| 3.9 | rolling features 一般 | **Safe**(現状の2実装) | ただし今後の追加 feature が polars の既定(closed の向き、部分窓容認)に無自覚だと混入し得る。**対策**: contract に「rolling は closed='left' か現在バー確定値のみ・部分窓は null」を規約化し、Future Mutation Test を全 observable 列に一括適用して新列も自動的に検査対象にする |
| 3.10 | features の全再生成方式 | **Safe** | 決定的・冪等で、過去データが不変なら past 行は不変。Future Mutation Test の前提と相性が良い(将来データを追加/改変 → past 行の observable 値が不変であることをテスト可能) |

---

## 4. Proposed Architecture(Phase 0 終了時点)

### 4.1 方針

- ROADMAP §7 の `research/` 新ツリーは**そのまま採用しない**(既存破壊になる)。
  既存 `src/mce` パッケージに backtest layer を**追加**し、ROADMAP の論理構成
  (data / features / backtest / audit / experiments)をサブモジュールへ写像する。
- 既存の ingest 系(okx / ingest / normalize / store)は**無変更**。
- features.py のみ「observable と label の分離」のために分割する(実質は移動)。

### 4.2 Directory / module 構成

```text
src/mce/
├── config.py                # 既存。DATA_DIR に labels/ を追加
├── okx.py ingest.py         # 既存・無変更(data layer)
├── normalize.py store.py    # 既存・無変更
├── report.py                # 既存・無変更(表示専用と明記)
│
├── features.py              # observable features のみ生成(fwd_* を除去)
├── labels.py                # fwd_return_* 生成 → data/labels/(評価専用)
├── manifest.py              # dataset manifest(hash・行数・期間・欠損数)生成
│
├── backtest/
│   ├── __init__.py
│   ├── data.py              # 唯一の backtest 用 loader(guard 付き・split 強制)
│   ├── splits.py            # research/validation/final_oos の凍結境界
│   ├── execution.py         # signal → position → fill(open[t+1])→ trades
│   ├── costs.py             # fee/spread/slippage 成分、シナリオ、break-even
│   ├── metrics.py           # return/Sharpe/MaxDD/turnover/… 純関数
│   ├── engine.py            # 上記を束ねる run_backtest()
│   └── baselines.py         # always_flat / buy_and_hold / naive momentum / random(seed)
│
└── experiments.py           # run artifact(JSON)の書き出し・読み込み

data/ (gitignore 対象、ローカルのみ)
├── raw/  normalized/        # 既存
├── features/                # observable のみ(fwd_* が存在しない)
├── labels/                  # fwd_return_*(評価・条件検索専用)
└── manifests/               # *.json(小さい。git 管理へ昇格を検討 → §7 Q5)

experiments/runs/            # run artifact JSON(git 管理、結果の台帳)
docs/data_contract.md        # Data Contract(時刻規約・命名規約・availability)
tests/                       # 既存 + test_labels / test_leakage_guard /
                             #   test_execution / test_costs / test_metrics /
                             #   test_determinism / test_splits
```

### 4.3 各 module の responsibility / input / output / dependency

| module | responsibility | input | output | dependency |
|---|---|---|---|---|
| `features.py`(改) | observable feature のみを決定的に全再生成。「observable = close[t] までに確定した値」の規約を負う | normalized ohlcv(+funding) | `data/features/*.parquet`(fwd_* 非含有) | config, normalize 済データ |
| `labels.py`(新) | 未来参照列(fwd_return_*)の生成を一手に隔離 | normalized ohlcv | `data/labels/*.parquet` | config |
| `manifest.py`(新) | データ資産の指紋(sha256・行数・ts範囲・欠損数・生成元 commit)を記録 | 各 Parquet | `data/manifests/*.json` | config |
| `backtest/data.py`(新) | **backtest からデータへの唯一の入口**。(1) features のみ読み、`fwd_` 接頭辞列があれば例外、(2) split 名を必須引数にし境界外の行を渡さない、(3) `final_oos` は通常 API で読めない | features Parquet + splits | split 済み observable DataFrame | splits, config |
| `backtest/splits.py`(新) | research/validation/final_oos の日付境界の単一定義(凍結対象) | なし(定数+検証関数) | split 境界 | — |
| `backtest/execution.py`(新) | signal(行 t で確定)→ position 遷移 → **open[t+1] fill**。holding period・forced exit・欠損バー時規則 | signal series + ohlcv(open 列) | trades / position series(fill 時刻・価格付き) | — (純関数群) |
| `backtest/costs.py`(新) | fee/spread/slippage を bps 成分として分離適用。シナリオ(zero/base/stress)。break-even cost 逆算 | trades + cost config | net PnL 系列、コスト内訳 | — |
| `backtest/metrics.py`(新) | 指標の純関数集。年率化定数(5m: 288×365)もここに集約 | net return series + trades | metrics dict | — |
| `backtest/engine.py`(新) | strategy(callable: features→signal)を受け、data→signal→execution→costs→metrics を決定的に一気通貫 | strategy, split 名, cost config, seed | BacktestResult | data, execution, costs, metrics |
| `backtest/baselines.py`(新) | 固定 baseline 戦略。random は seed 必須 | features | signal series | engine 契約のみ |
| `experiments.py`(新) | run ごとの機械可読 artifact(config・manifest hash・commit hash・seed・metrics・結果)保存 | BacktestResult + 環境情報 | `experiments/runs/EXP-*.json` | manifest |

**依存方向の規律**: `backtest/*` は ingest 系に依存しない(Parquet 経由のみ)。
`features.py` / `labels.py` は backtest に依存しない。労働は一方向:
`data → features/labels → backtest → experiments`。

### 4.4 available_time の実装レベル(提案)

Phase 0 では**列単位の宣言 + 規約 + テスト**で持つ(行単位の物理列は持たない):

1. contract 文書: 「バー ts = 開始時刻。observable 列の available_time = ts + 5m(close 時刻)。
   funding_rate 列のみ as-of で ts 以前確定値(さらに保守的)」
2. コード: features スキーマ定数に `AVAILABILITY = {"return_5m": "close_of_bar", ...}` を宣言し、
   backtest/data.py が未宣言列を拒否する
3. execution: 「行 t の signal は open[t+1] でしか執行できない」ことで availability を消費

行単位 `available_time` 物理列は、公開遅延が行ごとに異なるデータ(OI 実測遅延、aggTrades)
を observable に昇格させる時点で導入する(Phase 5/7)。今入れると全行に定数を複製するだけで
コストに見合わない。

### 4.5 Exchange difference の扱い(用語の運用)

| 用語 | 定義 | 本プロジェクトでの該当 |
|---|---|---|
| exact replication | 同一取引所・同一足・同一期間・同一手順の再現 | **該当なし**(行わない) |
| replication-inspired experiment | 原論文の仮説・手順を借り、データ条件を変えて検証 | Phase 1A(原論文: BTC-USDT **1時間足** → 本実験: OKX 5分足) |
| method transfer | 手法(cost-aware abstention 等)のみ移植し、性能主張は引き継がない | Phase 1A の位置づけの中核 |
| cross-exchange validation | 原論文と別取引所で同種構造の有無を検証 | Phase 1B(Quarter-Hour: 原論文 Binance perp → 本実験 OKX perp) |

- 実験 artifact に `replication_class` フィールドを持たせ、上記4値のいずれかを必ず記録する。
- **Binance ingestion は今回追加しない**。既存の Binance funding 代理データ(source="binance"、
  キャリー統計専用)は現状のまま維持。将来 Binance 追加が正当化されるのは
  (a) Phase 7 で aggTrades が必要になった場合(Binance Vision が最有力)、
  (b) funding/OI の深い履歴が cost model 上不可欠になった場合のみ。理由: OKX 公開 API の
  遡及制限(funding 約3ヶ月・OI 約5日)は Binance Vision の全履歴ダンプでしか補えない。
  ただしその場合も「OKX 執行の検証」という主軸は変えず、Binance データは feature/統計用途に
  限定する(findings の換算注意をそのまま適用)。

### 4.6 ROADMAP と既存設計の矛盾・注意点(そのまま実装しない箇所)

1. ROADMAP §7 の `research/` ディレクトリ新設 → 既存 `src/mce` への写像で代替(§4.1)。
2. ROADMAP §8 artifact 例の `symbol: BTCUSDT` は Binance 表記 → 本リポジトリの provenance
   3点組(source="okx", symbol="BTC-USDT-SWAP", market_type="perp_linear")で記録する。
3. ROADMAP §8 のコスト例(fee 5bps + spread 2 + slippage 3)は例示値 → cost config は
   取引所実測に基づき別途決める(findings の運用基準は OKX taker 片道 5bps)。
   ROADMAP の数値を既定値としてコピーしない。
4. ROADMAP Phase 0 の Cost に "funding(該当時)" → OKX funding 履歴の遡及約3ヶ月という
   API 制約により、33ヶ月バックテストの funding 込み PnL は**正確には計算できない**
   (Binance 代理は水準差あり)。Phase 0 では funding を PnL 外に置き感応度分析扱いとする
   ことを推奨(§7 Q4)。これは ROADMAP 4.4「market impactは感応度分析でもよい」と同じ精神。
5. ROADMAP の Phase 1B は Binance 原論文 → OKX 実施のため cross-exchange validation と
   明記する(exact replication と呼ばない)。

---

## 5. Phase 0 Implementation Plan

実装順。各 task は独立にレビュー・テスト可能な単位。

### P0-01 — Data Contract 文書化 + observable / label 物理分離

- **purpose**: leakage 源の構造的除去。時刻規約(バー ts=開始時刻、available_time=close)の明文化。以降の全 task の前提
- **files**: `docs/data_contract.md`(新)、`src/mce/features.py`(fwd_* 除去・AVAILABILITY 宣言)、`src/mce/labels.py`(新)、`src/mce/config.py`(labels パス追加)、`README.md`(スキーマ表更新)、`tests/test_labels.py`(新)、`tests/test_features.py`(fwd 系テストを labels 側へ移動)
- **dependencies**: なし
- **tests**: Forward Leakage Test(features 出力に `fwd_` 列が存在しない/labels 出力の値は従来と一致)、Future Mutation Test(未来バーを改変して features 再生成 → 過去行の全 observable 列が不変)
- **completion criteria**: `python -m mce.features` と `python -m mce.labels` が別ファイルを生成。既存 19 テスト+新テスト成功。README の条件検索 SQL が features ⋈ labels の明示 join に更新されている

### P0-02 — Dataset manifest

- **purpose**: 実験 artifact から「どのデータで評価したか」をハッシュで固定できるようにする
- **files**: `src/mce/manifest.py`(新)、`tests/test_manifest.py`(新)、`.gitignore`(manifests の扱い→ §7 Q5)
- **dependencies**: P0-01(features/labels 分離後のファイル構成に対して作る)
- **tests**: 同一 Parquet → 同一 manifest(決定性)、行数・ts範囲・欠損数の正しさ(合成データ)
- **completion criteria**: `python -m mce.manifest` が data 配下全資産の manifest JSON を生成・更新する

### P0-03 — Split 定義と enforcing loader

- **purpose**: research / validation / final_oos の境界をコードで凍結し、loader レベルで強制する
- **files**: `src/mce/backtest/splits.py`(新)、`src/mce/backtest/data.py`(新)、`tests/test_splits.py`(新)
- **dependencies**: P0-01
- **tests**: 境界の正しさ(境界バーの帰属含む)、`load(split="research")` に validation/final_oos 行が混入しない、final_oos が通常 API で読めない、`fwd_` 列を含む入力で loader が例外
- **completion criteria**: backtest 側からデータへ到達する経路が data.py のみで、split 指定なしでは読めない

### P0-04 — Execution engine

- **purpose**: signal at close[t] → fill at open[t+1] の決定的執行。long/short/flat、holding period、forced exit
- **files**: `src/mce/backtest/execution.py`(新)、`tests/test_execution.py`(新)
- **dependencies**: P0-03(入力規約)。ohlcv open 列のみ使用
- **tests**: Execution Delay Test(fill を close[t] に変えると結果が変わる/意図的に +1 bar 遅延させると結果が変わる)、同一バー signal→fill の禁止、欠損バー時の fill 規則(Q2 の決定に従う)、position 遷移表(flat→long→short 等)の網羅
- **completion criteria**: 手作りの小さな価格系列に対し、全 trade の (signal_ts, fill_ts, fill_price) が手計算と一致

### P0-05 — Cost model

- **purpose**: fee / spread / slippage の成分分離、シナリオ(zero / base / stress)、break-even cost
- **files**: `src/mce/backtest/costs.py`(新)、`tests/test_costs.py`(新)
- **dependencies**: P0-04(trades 形式)
- **tests**: Cost Monotonicity Test(コスト増で net が悪化以外にならない)、cost=0 で gross=net、break-even cost の逆算が往復コスト定義と整合
- **completion criteria**: 同一 trades に対し 3 シナリオの net PnL と break-even cost が出力される

### P0-06 — Metrics

- **purpose**: 指標の純関数化。初期必須: total return / annualized return / Sharpe / MaxDD / turnover / trade count / hit rate / exposure / break-even cost。安価なので同梱: Sortino / profit factor。**対象外(Phase 5)**: DSR / PBO / SPA / Reality Check / block bootstrap
- **files**: `src/mce/backtest/metrics.py`(新)、`tests/test_metrics.py`(新)
- **dependencies**: P0-05(net return series)
- **tests**: 既知系列(定数リターン、単調下落等)に対する各指標の手計算一致、空 trade 時の定義(NaN でなく明示値)
- **completion criteria**: metrics(net_returns, trades) が全指標を返し、年率化定数が 1 箇所に定義されている

### P0-07 — Baselines + Experiment artifact + Determinism

- **purpose**: 固定 baseline(always_flat / buy_and_hold / naive momentum / random(seed))を engine で走らせ、run artifact を保存する
- **files**: `src/mce/backtest/engine.py`(新)、`src/mce/backtest/baselines.py`(新)、`src/mce/experiments.py`(新)、`tests/test_determinism.py`(新)
- **dependencies**: P0-02〜P0-06 全て
- **tests**: Determinism Test(同一データ・設定・seed で artifact の metrics が bit 一致)、always_flat の全指標がゼロ系、buy_and_hold の total return が価格比と一致、artifact に manifest hash / commit hash / seed / replication_class が含まれる
- **completion criteria**: `experiments/runs/EXP-0001.json` 形式の artifact が生成され、再実行で同一結果

### P0-08 — 統合 CLI と Phase 0 exit criteria の機械化

- **purpose**: ROADMAP Phase 0 Exit Criteria を 1 コマンドで検証可能にする
- **files**: `src/mce/backtest/__main__.py` または engine への CLI 追加、`docs/phase0/exit_criteria.md`(チェックリストと実行ログ)
- **dependencies**: P0-07
- **tests**: 既存全テストがそのまま exit criteria の大半をカバーしていることの対応表を作る
- **completion criteria**: (1) future bar 改変で過去 signal 不変、(2) cost 0/正で PnL が期待通り変化、(3) execution 1 bar 遅延で結果変化、(4) baseline 再現、(5) seed 固定で同一結果、(6) 全 unit test 成功 — の 6 項目が全て自動テストで green

---

## 6. ADR Candidates

記録しておくべき設計判断(Architecture Decision Record):

1. **Why next-bar execution?**(signal at close[t] / fill at open[t+1]。same-bar close fill の禁止理由、バー ts=開始時刻の規約とセットで)
2. **Why Parquet + DuckDB + Polars?**(DB サーバー・分散処理を導入しない理由。PoC 規模の根拠)
3. **Why labels are physically separated from observable features?**(論理分離でなく別ファイルにする理由: SQL 直書き探索でも事故らない構造)
4. **Why available_time is declared per-column, not stored per-row?**(§4.4 の段階導入方針)
5. **Why Final OOS is sealed at the loader level?**(searcher からの構造的遮断。Phase 1 freeze との関係)
6. **Why OKX first / Binance data is proxy-only?**(執行検証は OKX に固定。Binance funding は統計専用。exact replication を主張しない用語規律 §4.5)
7. **Why funding is excluded from core PnL in Phase 0?**(API 遡及制限による履歴の非対称性。感応度分析での扱い)
8. **Why no free-form strategy Python?**(将来の DSL/AST に向けて、Phase 0 でも strategy は「features→signal の制約付き callable」に限定)
9. **Why full regeneration of features instead of incremental?**(冪等・決定性優先。データ量が許す限り継続)
10. **Why costs are modeled in bps of notional with scenario analysis?**(L2 が無い段階での正直な近似。market impact は感応度)
11. **Why the findings ledger stays in Markdown alongside machine-readable artifacts?**(人間向け結論と機械可読 run 記録の二層化)

---

## 7. Questions / Ambiguities(recommended default 付き)

repository と ROADMAP を読んでも決まらない、実装前判断が必要な点:

| # | 問い | recommended default |
|---|---|---|
| Q1 | **Split 境界の具体日付**。データは 2023-11-19 開始・現在も蓄積中。final_oos を「過去の一区間」にするか「ある日付以降の未来全部」にするか | 「日付以降の未来全部」型を推奨: research = 2023-11-19〜2025-06-30、validation = 2025-07-01〜2025-12-31、final_oos = 2026-01-01 以降(将来蓄積分も自動的に封印域へ入る)。境界は P0-03 実装時に確定しコミットで凍結 |
| Q2 | **fill 予定バー(t+1)が欠損している場合の執行規則** | 「次に存在するバーの open で執行。ただし signal から fill までの経過が閾値(既定 30 分)を超えたら注文キャンセル」を推奨。キャンセル数は artifact に記録。「即キャンセル」より現実的で、「無制限に待つ」より安全 |
| Q3 | **position sizing と複利** | 固定 notional 1 単位・非複利(算術リターン合算)を推奨。sizing 研究は明示的に後続 Phase へ。Sharpe 等の比較が素直になる |
| Q4 | **funding を Phase 0 の PnL に入れるか** | 入れない(コスト成分としては config に枠だけ用意し 0 とする)。理由: OKX funding 遡及約3ヶ月で長期履歴が欠け、Binance 代理は水準差あり。保有期間の長い戦略を評価する際は「funding 感応度」を別途併記 |
| Q5 | **manifest / experiments artifact を git 管理するか**(現状 data/ は全 ignore) | `data/manifests/*.json` は gitignore の否定パターンで**管理する**、`experiments/runs/*.json` も管理する、を推奨。どちらも小さく、再現性の根幹。生データは従来どおり非管理 |
| Q6 | **パッケージ名 `mce` を維持するか**(Market Condition Explorer の略のまま research platform 化する) | 維持を推奨。リネームは全 import に触る割に情報量ゼロ。名が気になるなら Phase 1 freeze 後に一括で |
| Q7 | **年率化定数**(5分足の年間バー数) | 288 × 365 = 105,120 を推奨(暗号資産は 24/365)。metrics.py に単一定数として定義し ADR に記録 |
| Q8 | **random baseline の乱数系** | `numpy` を新規依存に入れず Python 標準 `random.Random(seed)` を推奨(polars 到着順に依存しない形で)。将来 numpy が必要になったら Determinism Test がそのまま守ってくれる |
| Q9 | **既存 findings の検証 SQL(features 1 ファイル前提)の互換性** | 後方互換ビュー等は作らない。README と findings の「凍結仕様」欄の SQL は features ⋈ labels join 形に書き換え、旧形式は P0-01 で廃止(凍結仮説の**判定値**は不変なので台帳の結論は影響を受けない) |

---

## 8. Recommended First Coding Task

**P0-01(Data Contract 文書化 + observable / label 物理分離)を単独で、最初の実装依頼とする。**

内容(再掲・確定版):

- `docs/data_contract.md` 新規: 時刻規約(バー ts=開始時刻、observable の available_time=close 時刻、`feature_time <= signal_time < execution_time`)、命名規約(label は `fwd_` 接頭辞・`data/labels/` のみ)、rolling 規約(closed="left" or 当バー確定値、部分窓は null)
- `src/mce/labels.py` 新規: fwd_return_* 生成を features.py から移設、`data/labels/` へ出力
- `src/mce/features.py` 変更: fwd_* 除去、AVAILABILITY 宣言追加
- `src/mce/config.py` 変更: LABELS_DIR 追加
- `README.md` 変更: スキーマ表と条件検索 SQL を features ⋈ labels 形へ
- テスト: Forward Leakage Test / Future Mutation Test + 既存 fwd 系テストの移設

選定理由:

1. 今回の監査で唯一 **Unsafe** 判定になった構造(§3.1)を最初に潰す。以降の全 task
   (loader guard、execution、engine)がこの分離を前提にする
2. 変更が `features.py` 1 ファイルの分割+文書+テストに収まり、diff が小さく
   レビュー可能。既存 ingest 系・normalized データには一切触れない
3. 完了判定が機械的(新旧 labels 値の一致テスト、Future Mutation Test green)で、
   データ再生成もユーザーのローカルで `python -m mce.features && python -m mce.labels`
   を一度走らせるだけ

このタスクの完了後、P0-02(manifest)→ P0-03(split + loader)→ P0-04(execution)…と
§5 の順に、1 依頼 1〜2 task の粒度で進める。

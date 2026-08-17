# Market Condition Explorer PoC

暗号資産(まずは BTC のみ)の短期市場データをローカルに蓄積し、
「ある市場条件が過去に何回発生し、その後どうなったか」を SQL / Python で
検証できるようにするための個人研究環境。売買シグナル生成は目的ではない。

## 構成

```
Exchange Public API (OKX v5)
    ↓  src/mce/okx.py      … 認証不要の public エンドポイントのみ
    ↓  src/mce/ingest.py   … 取得 CLI(冪等・差分再開つき)
data/raw/                  … APIレスポンス原形 (JSONL gzip, 追記のみ)
    ↓  src/mce/normalize.py … 共通スキーマへ変換(補間はしない)
data/normalized/           … Parquet(粒度ごとに別テーブル)
    ↓  DuckDB
src/mce/report.py          … 集計・欠損検出レポート
data/features/             … (将来) return_5m, volume_ratio_20 など

OKX WebSocket (public + business)
    ↓  src/mce/collect_microstructure.py … trades / BBO / 400段板 / instruments
data/raw/okx/ws/           … 接続到着順のimmutable gzip JSONL
data/raw/okx/rest/         … contract / tick / lotの初期・日次snapshot
data/raw/host/clock_quality/ … Linux adjtimexの起動時・60秒周期sample
    ↓  src/mce/normalize_microstructure.py
data/normalized/okx/microstructure/v3/ … schema/到着UTC日/hour partitionのParquet shard
```

データソース比較と選定理由は [docs/data_sources.md](docs/data_sources.md) を参照。

## セットアップ

Python 3.11+ / [uv](https://docs.astral.sh/uv/)。

```sh
uv sync
```

## 使い方

```sh
# 直近30日の5分足OHLCVを取得(再実行すると差分のみ取得される)
uv run python -m mce.ingest ohlcv --days 30

# Funding Rate(APIの制約で直近約3ヶ月まで)/ Open Interest(直近約5日まで)
uv run python -m mce.ingest funding --days 90
uv run python -m mce.ingest oi --days 30

# サマリ表示(件数・期間・欠損数・5分リターン統計)
uv run python -m mce.report

# observable features 生成(normalized から全再生成・冪等)
uv run python -m mce.features

# 評価専用ラベル fwd_return_* 生成(data/labels/ へ分離出力)
uv run python -m mce.labels

# dataset manifest 生成(sha256・行数・期間・欠損数。git 管理)
uv run python -m mce.manifest

# baseline backtest(signal at close[t] → fill at open[t+1]。artifact を experiments/runs/ へ保存)
uv run python -m mce.backtest --strategy buy_and_hold --split research --cost base_taker

# Phase 1A: cost-aware abstention 実験(walk-forward。プロトコルは docs/phase1/phase1a_protocol.md)
uv run python -m mce.research.abstention --cost maker_low
uv run python -m mce.research.abstention --cost base_taker

# Phase 7 Tier 0: Binance Vision 一括ダンプの取得→正規化→observable→品質レポート
#   (ラベルは作らない。ts >= 2026-01-01 は封印継承で落とす)
uv run python -m mce.binance_vision --start 2020-01 --end 2025-12
uv run python -m mce.normalize_binance
uv run python -m mce.features_tier0
uv run python -m mce.tier0_quality --json experiments/phase7/tier0_quality_v1.json

# Phase 3 bakeoff の cross-arm 集計(凍結 artifact を読むだけ・再評価しない)
uv run python -m mce.phase3_summary --json experiments/phase3/bakeoff_summary.json

# ローカルデータ在庫(manifest・OHLCV系・microstructure shard/raw の有無と期間)
uv run python -m mce.data_inventory --json data/analysis/data_inventory.json

# 約定・BBO・400段板を60秒だけ疎通確認（省略時はSIGINT/SIGTERMまで継続）
uv run python -m mce.collect_microstructure --duration 60

# closed rawの品質確認とimmutable Parquet化
uv run python -m mce.microstructure_quality data/raw/okx/ws
uv run python -m mce.normalize_microstructure \
  data/raw/okx/ws/public/YYYY/MM/DD/*.jsonl.gz \
  data/raw/okx/ws/business/YYYY/MM/DD/*.jsonl.gz \
  data/raw/okx/rest/instruments/YYYY/MM/DD/*.jsonl.gz

# テスト
uv run pytest
```

## backtest layer (Phase 0 — Deterministic Judge)

[ROADMAP.md](ROADMAP.md) Phase 0 の決定論的評価器。契約は
[docs/data_contract.md](docs/data_contract.md)、Exit Criteria との対応は
[docs/phase0/exit_criteria.md](docs/phase0/exit_criteria.md) を参照。

```
mce/backtest/
  splits.py     research / validation / final_oos の凍結境界(final_oos は封印)
  data.py       guard 付き loader(fwd_ 列拒否・availability 検査・split 強制)
  execution.py  signal at close[t] → fill at open[t+1](欠損バー時は遅延上限つき)
  costs.py      fee/spread/slippage の bps 成分・シナリオ・break-even cost
  metrics.py    Sharpe / Sortino / MaxDD / turnover / hit rate / exposure など
  engine.py     features → strategy → execution → cost → metrics の一気通貫
  baselines.py  always_flat / buy_and_hold / naive_momentum / random(seed 必須)
mce/experiments.py  run artifact(JSON・追記専用)を experiments/runs/ へ保存
```

## DSL layer (Phase 2 — Semantic Schema + DSL + AST)

searcher(Random / Genetic / LLM)は Python を書けない。生成できるのは JSON の
AST のみで、whitelist compiler が凍結済み Judge の strategy へ決定的に変換する。
仕様は [docs/phase2/dsl_spec.md](docs/phase2/dsl_spec.md)(Phase 3 開始前に凍結予定)。

```
mce/dsl/
  ops.py        feature/transform/bool 演算(contract 準拠: ts一致join・完全窓・負lagなし)
  nodes.py      AST 正規化・sha256 hash(duplicate control 第1層)・(de)serialization
  validator.py  制約検査(depth≤5 / features≤4 / params≤6 / holding≤48 / whitelist)
  compiler.py   AST → StrategySpec + ExecutionConfig(検証必須・部分木memoize)
  schema.py     Event×Context×Quality×Direction×Action の仮説語彙と検証
```

DuckDB から直接クエリする場合:

```sql
SELECT date_trunc('day', ts) AS d, avg(volume)
FROM read_parquet('data/normalized/ohlcv/okx_BTC-USDT-SWAP_5m.parquet')
GROUP BY d ORDER BY d;
```

## normalized スキーマ

粒度の異なるデータは無理に1テーブルへまとめず、テーブル(=Parquetファイル)を分ける。
全テーブル共通で出所列 `symbol` / `source` / `market_type` を持ち、`ts` は UTC
(ms精度・tz付き)。表示時のみ JST へ変換する。

| テーブル | 粒度 | 列 |
|---|---|---|
| `ohlcv` | 5m | ts, open, high, low, close, volume (BTC建て), volume_quote (USDT建て), symbol, source, market_type |
| `funding_rate` | 8h | ts, funding_rate, symbol, source, market_type |
| `open_interest` | 5m | ts, oi (BTC建て), oi_usd, symbol, source, market_type |

Phase 7 Tier 0(Binance USDT-M perp。別 venue なので `data/normalized/binance/` へ分離):

| テーブル | 粒度 | 列 |
|---|---|---|
| `klines_5m` | 5m | ts, open, high, low, close, volume, volume_quote, trades, taker_buy_volume, taker_buy_quote |
| `metrics_5m` | 5m snapshot | ts, open_interest, open_interest_value, top_trader_account_ls_ratio, top_trader_position_ls_ratio, global_account_ls_ratio, taker_ls_vol_ratio |
| `premium_index_5m` | 5m | ts, premium_open, premium_high, premium_low, premium_close, premium_samples |

契約・timestamp semantics・availability 宣言は
[docs/phase7/tier0_ingest_v1.md](docs/phase7/tier0_ingest_v1.md)。

## 設計上のルール

- **UTC 基準**: 内部時刻はすべて UTC。UNIX time は API 境界でのみ ms で扱い、即 Datetime 化する
- **冪等**: normalized へのマージは (source, symbol, ts) で重複排除。同じ取得を何度実行しても二重登録されない
- **差分再開**: 既存 Parquet の最終 ts 以降だけを取得する
- **raw 保持**: API レスポンスは原形のまま gzip JSONL で残す(正規化のバグはやり直せる)
- **欠損は補間しない**: report が 5 分グリッドとの突き合わせで欠損を検出・表示する

## features / labels スキーマ

**observable と label は物理的に分離されている**([docs/data_contract.md](docs/data_contract.md) 参照)。
features には「バー t の close 時点までに観測可能な値」のみが入り、先読み列
(`fwd_` 接頭辞)は labels にのみ存在する。backtest loader は fwd_ 列を拒否する。

### `data/features/okx_BTC-USDT-SWAP_5m.parquet`(observable)

OHLCV 全列に加えて:

| 列 | 意味 |
|---|---|
| `return_5m`, `return_1h` | 過去リターン(基準バーが欠損なら null) |
| `volume_ratio_20` | 出来高 / 直近20本平均(窓に20本揃わなければ null) |
| `drift_20d` | 20日リターン(基準バーが欠損なら null)。レジーム分類用 |
| `realized_vol_20d` | 直近20日([ts−20d, ts))の5分リターン標準偏差(有効本数が窓の90%未満なら null) |
| `minute_mod_15`, `minute_mod_60`, `hour_utc`, `weekday_utc` | clock 位相(バー開始時刻 UTC。weekday は 0=月曜) |
| `funding_rate` | その時点で確定している直近 Funding(as-of join, 9時間超は null) |
| `oi`, `oi_usd` | 同時刻の Open Interest(なければ null) |

### `data/labels/okx_BTC-USDT-SWAP_5m.parquet`(評価専用)

| 列 | 意味 |
|---|---|
| `fwd_return_5m`, `fwd_return_1h`, `fwd_return_4h` | **先読みリターン**(条件検索の「その後どうなった」用。strategy feature としての利用禁止) |
| `fwd_open_return_1h` | **執行整合ラベル**: open[t+1] entry → open[t+13] exit のリターン(Phase 1A の学習・評価用) |

リターン計算は行シフトではなく ts の完全一致 join なので、欠損バーを
またいで誤った期間のリターンが混入することはない。

## Historical Condition Search(SQL 1本で可能)

「この市場状態は過去に何回あり、その後どうなった?」は DuckDB で直接引ける。
条件(observable)は features、結果(fwd_*)は labels を **明示 join** して参照する:

```sql
SELECT count(*)                            AS n,
       avg(l.fwd_return_1h)                AS mean_1h,
       median(l.fwd_return_1h)             AS median_1h,
       avg((l.fwd_return_1h > 0)::INT)     AS win_rate_1h,
       quantile_cont(l.fwd_return_1h, 0.1) AS p10_1h,
       quantile_cont(l.fwd_return_1h, 0.9) AS p90_1h
FROM read_parquet('data/features/okx_BTC-USDT-SWAP_5m.parquet') f
JOIN read_parquet('data/labels/okx_BTC-USDT-SWAP_5m.parquet') l USING (ts)
WHERE f.volume_ratio_20 >= 2.0
  AND f.return_1h >= 0.005
  AND f.funding_rate >= 0.00001
  AND l.fwd_return_1h IS NOT NULL;
```

## 今後(未実装)

- 条件検索の CLI / 関数化(上記 SQL のテンプレート化)
- 特徴量の追加(`rolling_volatility`, `high_breakout_1h`, `open_interest_change` など)
- Funding / OI の定期取得による長期蓄積(API の遡及制限が浅いため)

OHLCV方向探索の検証済み結論と、prospectiveなOFI・板枯れ・吸収v1の事前仕様は
[docs/findings/README.md](docs/findings/README.md) を参照。

## 現在の研究軸(2026-08-16)

Phase 3 Alpha Search Bakeoff は Random / Genetic / LLM の3 arm すべてで
validation survivor 0/30 で完了した([総括](docs/findings/2026-08-16-phase3-bakeoff-summary-v1.md))。
これは「OHLCVにalphaが無い」ことでも「information setが唯一の原因」であることでもなく、
**次に検証する仮説として information-set expansion の期待情報価値が最も高い**という
優先順位の変更である。次の設計は
[Phase 7 — Information-Space Expansion](docs/phase7/information_space_expansion_v1.md)、
保留にした探索アルゴリズム研究は [research backlog](docs/research_backlog.md) にある。

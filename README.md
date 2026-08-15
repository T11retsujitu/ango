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

# features 生成(normalized から全再生成・冪等)
uv run python -m mce.features

# テスト
uv run pytest
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

## 設計上のルール

- **UTC 基準**: 内部時刻はすべて UTC。UNIX time は API 境界でのみ ms で扱い、即 Datetime 化する
- **冪等**: normalized へのマージは (source, symbol, ts) で重複排除。同じ取得を何度実行しても二重登録されない
- **差分再開**: 既存 Parquet の最終 ts 以降だけを取得する
- **raw 保持**: API レスポンスは原形のまま gzip JSONL で残す(正規化のバグはやり直せる)
- **欠損は補間しない**: report が 5 分グリッドとの突き合わせで欠損を検出・表示する

## features スキーマ

`data/features/okx_BTC-USDT-SWAP_5m.parquet`。OHLCV 全列に加えて:

| 列 | 意味 |
|---|---|
| `return_5m`, `return_1h` | 過去リターン(基準バーが欠損なら null) |
| `volume_ratio_20` | 出来高 / 直近20本平均(窓に20本揃わなければ null) |
| `fwd_return_5m`, `fwd_return_1h`, `fwd_return_4h` | **先読みリターン**(条件検索の「その後どうなった」用) |
| `funding_rate` | その時点で確定している直近 Funding(as-of join, 9時間超は null) |
| `oi`, `oi_usd` | 同時刻の Open Interest(なければ null) |

リターン計算は行シフトではなく ts の完全一致 join なので、欠損バーを
またいで誤った期間のリターンが混入することはない。

## Historical Condition Search(SQL 1本で可能)

「この市場状態は過去に何回あり、その後どうなった?」は DuckDB で直接引ける:

```sql
SELECT count(*)                          AS n,
       avg(fwd_return_1h)                AS mean_1h,
       median(fwd_return_1h)             AS median_1h,
       avg((fwd_return_1h > 0)::INT)     AS win_rate_1h,
       quantile_cont(fwd_return_1h, 0.1) AS p10_1h,
       quantile_cont(fwd_return_1h, 0.9) AS p90_1h
FROM read_parquet('data/features/okx_BTC-USDT-SWAP_5m.parquet')
WHERE volume_ratio_20 >= 2.0
  AND return_1h >= 0.005
  AND funding_rate >= 0.00001
  AND fwd_return_1h IS NOT NULL;
```

## 今後(未実装)

- 条件検索の CLI / 関数化(上記 SQL のテンプレート化)
- 特徴量の追加(`rolling_volatility`, `high_breakout_1h`, `open_interest_change` など)
- Funding / OI の定期取得による長期蓄積(API の遡及制限が浅いため)

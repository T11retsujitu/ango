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

## 今後(未実装)

最重要は Historical Condition Search:
`volume_ratio_20 >= 2.0 AND return_1h >= 0.015 AND funding_rate >= 0.0001`
のような条件の該当件数と、その 5分後 / 1時間後 / 4時間後のリターン分布
(平均・中央値・勝率・分位点)を返す機能。features 層に特徴量 Parquet を
生成し、DuckDB の SQL 一発で答えられる形を想定している。

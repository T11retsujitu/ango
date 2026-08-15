"""取得済みデータのサマリを DuckDB で集計して表示する。

    python -m mce.report

内部時刻は UTC。表示のみ JST を併記する。欠損は 5 分グリッドとの
突き合わせで「検出」する(補間はしない)。
"""

from datetime import timezone
from zoneinfo import ZoneInfo

import duckdb

from mce import config

JST = ZoneInfo("Asia/Tokyo")


def _fmt(dt) -> str:
    utc = dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)
    return f"{utc.isoformat()} (JST {utc.astimezone(JST).strftime('%Y-%m-%d %H:%M')})"


def report_ohlcv(con: duckdb.DuckDBPyConnection) -> None:
    path = config.ohlcv_parquet()
    if not path.exists():
        print("ohlcv: データなし(まず `python -m mce.ingest ohlcv` を実行)")
        return
    # timestamptz のまま Python へ取り出すと pytz が要るため、UTC の naive timestamp に落とす
    con.execute(
        f"CREATE OR REPLACE VIEW ohlcv AS SELECT * REPLACE (ts::TIMESTAMP AS ts) FROM read_parquet('{path}')"
    )

    n, ts_min, ts_max = con.execute("SELECT count(*), min(ts), max(ts) FROM ohlcv").fetchone()
    expected = con.execute(
        "SELECT 1 + datediff('minute', min(ts), max(ts)) / 5 FROM ohlcv"
    ).fetchone()[0]
    missing = int(expected) - n

    print("== OHLCV (5分足) ==")
    print(f"  レコード数 : {n}")
    print(f"  開始       : {_fmt(ts_min)}")
    print(f"  終了       : {_fmt(ts_max)}")
    print(f"  期待本数   : {int(expected)}")
    print(f"  欠損数     : {missing}")

    if missing > 0:
        gaps = con.execute(
            """
            SELECT ts AS gap_after, next_ts, datediff('minute', ts, next_ts) AS gap_min
            FROM (SELECT ts, lead(ts) OVER (ORDER BY ts) AS next_ts FROM ohlcv)
            WHERE next_ts > ts + INTERVAL 5 MINUTE
            ORDER BY gap_min DESC LIMIT 5
            """
        ).fetchall()
        for g in gaps:
            print(f"  欠損区間   : {_fmt(g[0])} の直後 〜 {_fmt(g[1])} ({g[2]} 分)")

    mean_r, med_r, mean_v = con.execute(
        """
        SELECT avg(r), median(r), avg(volume)
        FROM (
            SELECT close / lag(close) OVER (ORDER BY ts) - 1 AS r, volume
            FROM ohlcv
        )
        WHERE r IS NOT NULL
        """
    ).fetchone()
    print(f"  5分リターン平均   : {mean_r:+.6%}")
    print(f"  5分リターン中央値 : {med_r:+.6%}")
    print(f"  5分足出来高平均   : {mean_v:.3f} BTC")


def _report_simple(con: duckdb.DuckDBPyConnection, name: str, path, value_col: str) -> None:
    if not path.exists():
        print(f"{name}: データなし")
        return
    n, ts_min, ts_max, v_avg = con.execute(
        f"SELECT count(*), min(ts)::TIMESTAMP, max(ts)::TIMESTAMP, avg({value_col}) FROM read_parquet('{path}')"
    ).fetchone()
    print(f"== {name} ==")
    print(f"  レコード数 : {n}")
    print(f"  開始       : {_fmt(ts_min)}")
    print(f"  終了       : {_fmt(ts_max)}")
    print(f"  {value_col} 平均 : {v_avg:.8g}")


def main() -> None:
    con = duckdb.connect()
    con.execute("SET TimeZone = 'UTC'")
    report_ohlcv(con)
    print()
    _report_simple(con, "Funding Rate (8時間毎)", config.funding_parquet(), "funding_rate")
    print()
    _report_simple(con, "Open Interest (5分毎)", config.open_interest_parquet(), "oi")


if __name__ == "__main__":
    main()

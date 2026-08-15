import polars as pl

from mce.normalize import normalize_candles
from mce.store import merge_parquet, ts_range_ms


def make_df(ts_list):
    rows = [[str(ts), "1", "2", "0.5", "1.5", "10", "0.1", "150", "1"] for ts in ts_list]
    return normalize_candles(rows, "BTC-USDT-SWAP")


KEYS = ["source", "symbol", "ts"]


def test_merge_is_idempotent(tmp_path):
    path = tmp_path / "t.parquet"
    df = make_df([1000, 2000, 3000])
    assert merge_parquet(path, df, KEYS) == 3
    assert merge_parquet(path, df, KEYS) == 0  # 再実行しても増えない
    assert pl.read_parquet(path).height == 3


def test_merge_dedups_overlap(tmp_path):
    path = tmp_path / "t.parquet"
    merge_parquet(path, make_df([1000, 2000]), KEYS)
    added = merge_parquet(path, make_df([2000, 3000]), KEYS)
    assert added == 1
    out = pl.read_parquet(path)
    assert out.height == 3
    assert out["ts"].is_sorted()


def test_ts_range_ms(tmp_path):
    path = tmp_path / "t.parquet"
    assert ts_range_ms(path) == (None, None)
    merge_parquet(path, make_df([1000, 60_000]), KEYS)
    assert ts_range_ms(path) == (1000, 60_000)

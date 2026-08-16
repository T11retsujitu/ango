"""backtest loader の guard(leakage / availability / sealed split)のテスト。"""

from datetime import datetime, timezone

import polars as pl
import pytest

from mce.backtest import data as btdata
from mce.features import build_features
from mce.normalize import normalize_candles

UTC = timezone.utc
BAR_MS = 5 * 60_000


def _features_parquet(tmp_path, base: datetime, n_bars: int, extra_cols: dict | None = None):
    """base 起点で n_bars 連続の合成 features Parquet を作る。"""
    t0 = int(base.timestamp() * 1000)
    rows = [
        [str(t0 + i * BAR_MS), "100", "100", "100", "100", "0", "1", "0", "1"]
        for i in range(n_bars)
    ]
    df = build_features(normalize_candles(rows, "BTC-USDT-SWAP"))
    if extra_cols:
        df = df.with_columns([pl.lit(v).alias(k) for k, v in extra_cols.items()])
    path = tmp_path / "features.parquet"
    df.write_parquet(path)
    return path


def test_load_features_filters_to_split(tmp_path):
    # validation 境界(2025-07-01)を跨ぐ 4 バー
    base = datetime(2025, 6, 30, 23, 50, tzinfo=UTC)
    path = _features_parquet(tmp_path, base, 4)
    research = btdata.load_features("research", path=path)
    validation = btdata.load_features("validation", path=path)
    assert research.height == 2  # 23:50, 23:55
    assert validation.height == 2  # 00:00, 00:05
    assert research["ts"].max() < validation["ts"].min()


def test_bars_before_research_are_excluded(tmp_path):
    path = _features_parquet(tmp_path, datetime(2023, 11, 18, 23, 50, tzinfo=UTC), 4)
    df = btdata.load_features("research", path=path)
    assert df.height == 2  # 2023-11-19 00:00, 00:05 のみ


def test_final_oos_is_sealed(tmp_path):
    path = _features_parquet(tmp_path, datetime(2026, 1, 1, tzinfo=UTC), 2)
    with pytest.raises(btdata.SealedAccessError):
        btdata.load_features("final_oos", path=path)
    with pytest.raises(btdata.SealedAccessError):
        btdata.load_sealed_final_oos("please", path=path)
    df = btdata.load_sealed_final_oos(btdata.SEALED_ACK, path=path)
    assert df.height == 2


def test_fwd_column_raises_leakage_error(tmp_path):
    path = _features_parquet(
        tmp_path, datetime(2024, 1, 1, tzinfo=UTC), 2, extra_cols={"fwd_return_1h": 0.0}
    )
    with pytest.raises(btdata.LeakageError):
        btdata.load_features("research", path=path)


def test_undeclared_column_raises(tmp_path):
    path = _features_parquet(
        tmp_path, datetime(2024, 1, 1, tzinfo=UTC), 2, extra_cols={"mystery_signal": 1.0}
    )
    with pytest.raises(btdata.UndeclaredFeatureError):
        btdata.load_features("research", path=path)


def test_unknown_split_raises(tmp_path):
    path = _features_parquet(tmp_path, datetime(2024, 1, 1, tzinfo=UTC), 2)
    with pytest.raises(ValueError):
        btdata.load_features("test", path=path)

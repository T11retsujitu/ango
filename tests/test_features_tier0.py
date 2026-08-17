"""Phase 7 Tier 0 observable features(契約適合・join 規約・ラベル非混入)。"""

from datetime import datetime, timedelta, timezone

import polars as pl
import pytest

from mce import features, features_tier0

UTC = timezone.utc
START = datetime(2024, 1, 1, tzinfo=UTC)


def _klines(n: int = 30, volume: float = 10.0, taker: float = 4.0, trades: int = 8) -> pl.DataFrame:
    ts = [START + timedelta(minutes=5 * i) for i in range(n)]
    close = [100.0 + i for i in range(n)]
    return pl.DataFrame(
        {
            "ts": ts,
            "open": close,
            "high": [c + 1 for c in close],
            "low": [c - 1 for c in close],
            "close": close,
            "volume": [volume] * n,
            "volume_quote": [volume * c for c in close],
            "trades": [trades] * n,
            "taker_buy_volume": [taker] * n,
            "taker_buy_quote": [taker * c for c in close],
        }
    ).with_columns(
        pl.col("ts").dt.cast_time_unit("ms"),
        pl.lit("BTCUSDT").alias("symbol"),
        pl.lit("binance").alias("source"),
        pl.lit("perp_linear").alias("market_type"),
    )


def _metrics(timestamps) -> pl.DataFrame:
    n = len(timestamps)
    return pl.DataFrame(
        {
            "ts": list(timestamps),
            "open_interest": [1000.0] * n,
            "open_interest_value": [100000.0] * n,
            "top_trader_account_ls_ratio": [1.1] * n,
            "top_trader_position_ls_ratio": [1.2] * n,
            "global_account_ls_ratio": [1.3] * n,
            "taker_ls_vol_ratio": [0.9] * n,
        }
    ).with_columns(pl.col("ts").dt.cast_time_unit("ms").dt.replace_time_zone("UTC"))


def test_flow_columns_are_ratios_of_the_bar():
    df = features_tier0.build_tier0_features(_klines())
    row = df.row(0, named=True)
    assert row["taker_buy_ratio"] == pytest.approx(0.4)
    assert row["taker_buy_quote_ratio"] == pytest.approx(0.4)
    assert row["trade_count"] == 8
    assert row["avg_trade_size"] == pytest.approx(10.0 / 8)


def test_zero_volume_bar_gives_null_ratio_not_infinity():
    klines = _klines(n=3).with_columns(
        pl.when(pl.col("ts") == START).then(0.0).otherwise(pl.col("volume")).alias("volume"),
        pl.when(pl.col("ts") == START).then(0).otherwise(pl.col("trades")).alias("trades"),
    )
    df = features_tier0.build_tier0_features(klines).sort("ts")
    assert df["taker_buy_ratio"][0] is None
    assert df["avg_trade_size"][0] is None
    assert df["taker_buy_ratio"][1] is not None


def test_metrics_join_is_exact_ts_not_forward_filled():
    klines = _klines(n=4)
    # 1本目と3本目のスナップショットだけ存在する
    partial = _metrics([klines["ts"][0], klines["ts"][2]])
    df = features_tier0.build_tier0_features(klines, metrics=partial).sort("ts")
    assert df["open_interest"].to_list() == [1000.0, None, 1000.0, None]


def test_missing_optional_tables_produce_null_columns():
    df = features_tier0.build_tier0_features(_klines(n=3))
    for column in ("open_interest", "premium_close", "taker_ls_vol_ratio"):
        assert df[column].null_count() == df.height


def test_output_has_no_forward_looking_columns_and_declares_availability():
    df = features_tier0.build_tier0_features(_klines())
    assert not [c for c in df.columns if c.startswith("fwd_")]
    features_tier0.assert_observable(df)  # 例外が出ないこと
    declared = {**features.AVAILABILITY, **features_tier0.TIER0_AVAILABILITY}
    for c in df.columns:
        assert c in features.META_COLUMNS or c in declared


def test_undeclared_column_is_rejected():
    df = features_tier0.build_tier0_features(_klines()).with_columns(
        pl.lit(1.0).alias("mystery_column")
    )
    with pytest.raises(AssertionError):
        features_tier0.assert_observable(df)


def test_okx_only_columns_are_dropped_not_faked():
    df = features_tier0.build_tier0_features(_klines())
    for column in ("funding_rate", "oi", "oi_usd"):
        assert column not in df.columns


def test_baseline_columns_match_okx_definition():
    """baseline は OKX と同じ mce.features のコードで作る(venue 差を作らない)。"""
    df = features_tier0.build_tier0_features(_klines())
    for column in ("return_5m", "return_1h", "volume_ratio_20", "minute_mod_15", "hour_utc"):
        assert column in df.columns
    assert features.AVAILABILITY["return_5m"] == "close_of_bar"


def test_nonpositive_metrics_values_become_null():
    """OI=0 の穴が実データに存在する。observable では欠測として扱う(捏造しない)。"""
    klines = _klines(n=3)
    metrics = _metrics(list(klines["ts"])).with_columns(
        pl.when(pl.col("ts") == START).then(0.0).otherwise(pl.col("open_interest")).alias("open_interest"),
        pl.when(pl.col("ts") == START).then(0.0).otherwise(pl.col("taker_ls_vol_ratio")).alias("taker_ls_vol_ratio"),
    )
    df = features_tier0.build_tier0_features(klines, metrics=metrics).sort("ts")
    assert df["open_interest"][0] is None
    assert df["taker_ls_vol_ratio"][0] is None
    assert df["open_interest"][1] == 1000.0  # 正常行はそのまま

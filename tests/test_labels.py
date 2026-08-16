"""labels(fwd_return_*)の値と、observable/label 分離契約のテスト。

- Forward Leakage Test: features 出力に fwd_ 列が存在しない・全列 availability 宣言済み
- Future Mutation Test: 未来バーを書き換えても過去行の observable 値が変化しない
"""

import polars as pl

from conftest import by_minute, make_ohlcv
from mce.features import AVAILABILITY, META_COLUMNS, build_features
from mce.labels import build_labels


def test_fwd_values_and_gap_safety():
    # 15分のバーが欠損している
    df = build_labels(make_ohlcv([(0, 100, 1), (5, 110, 1), (10, 121, 1), (20, 133.1, 1)]))
    # 5分バーの5分後(10分)は有効、10分バーの5分後(15分)は欠損 → null
    assert abs(by_minute(df, 5)["fwd_return_5m"] - 0.10) < 1e-12
    assert by_minute(df, 10)["fwd_return_5m"] is None


def test_fwd_return_1h_and_4h():
    df = build_labels(make_ohlcv([(0, 100, 1), (60, 105, 1), (240, 120, 1)]))
    assert abs(by_minute(df, 0)["fwd_return_1h"] - 0.05) < 1e-12
    assert abs(by_minute(df, 0)["fwd_return_4h"] - 0.20) < 1e-12
    assert by_minute(df, 60)["fwd_return_1h"] is None  # 120分のバーが無い


def test_features_contain_no_labels_and_all_availability_declared():
    df = build_features(make_ohlcv([(0, 100, 1), (5, 110, 1)]))
    for c in df.columns:
        assert not c.startswith("fwd_"), f"features に label 列 {c} が混入"
        assert c in META_COLUMNS or c in AVAILABILITY, f"列 {c} の availability が未宣言"


def test_labels_columns_all_have_fwd_prefix():
    df = build_labels(make_ohlcv([(0, 100, 1), (5, 110, 1)]))
    value_cols = [c for c in df.columns if c not in META_COLUMNS]
    assert value_cols  # ラベルが1列以上ある
    assert all(c.startswith("fwd_") for c in value_cols)


def test_future_mutation_does_not_change_past_observables():
    """未来(60分より後)のバーの価格・出来高を書き換えても、60分以前の行の
    observable features は 1 bit も変化しない(labels は変化する)。"""
    past = [(5 * i, 100.0 + i, 1.0 + 0.1 * i) for i in range(13)]  # 0..60分
    future_a = [(5 * i, 100.0 + i, 1.0) for i in range(13, 30)]  # 65..145分
    future_b = [(5 * i, 500.0 - i, 9.0) for i in range(13, 30)]  # 同じ ts で値だけ改変
    future_b += [(200, 777.0, 5.0)]  # さらに未来へバーを追加

    feat_a = build_features(make_ohlcv(past + future_a))
    feat_b = build_features(make_ohlcv(past + future_b))

    cutoff = pl.col("ts").dt.epoch("ms") <= 60 * 60_000
    past_a = feat_a.filter(cutoff).sort("ts")
    past_b = feat_b.filter(cutoff).sort("ts")
    assert past_a.equals(past_b)

    # 対照: labels は未来改変の影響を受ける(テストの検出力の確認)
    lab_a = build_labels(make_ohlcv(past + future_a))
    lab_b = build_labels(make_ohlcv(past + future_b))
    assert by_minute(lab_a, 60)["fwd_return_5m"] != by_minute(lab_b, 60)["fwd_return_5m"]


def test_clock_features():
    df = build_features(make_ohlcv([(0, 100, 1), (25, 100, 1), (65, 100, 1)]))
    r = by_minute(df, 25)
    assert r["minute_mod_15"] == 10
    assert r["minute_mod_60"] == 25
    assert r["hour_utc"] == 0
    assert r["weekday_utc"] == 3  # 1970-01-01 は木曜(0=月曜)
    assert by_minute(df, 65)["hour_utc"] == 1
    assert by_minute(df, 65)["minute_mod_15"] == 5

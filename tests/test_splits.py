from datetime import datetime, timezone

import pytest

from mce.backtest import splits

UTC = timezone.utc


def test_boundary_assignment():
    assert splits.assign(datetime(2023, 11, 18, 23, 55, tzinfo=UTC)) is None  # research 以前
    assert splits.assign(datetime(2023, 11, 19, tzinfo=UTC)) == "research"
    assert splits.assign(datetime(2025, 6, 30, 23, 55, tzinfo=UTC)) == "research"
    assert splits.assign(datetime(2025, 7, 1, tzinfo=UTC)) == "validation"  # 境界は次区間
    assert splits.assign(datetime(2025, 12, 31, 23, 55, tzinfo=UTC)) == "validation"
    assert splits.assign(datetime(2026, 1, 1, tzinfo=UTC)) == "final_oos"
    assert splits.assign(datetime(2099, 1, 1, tzinfo=UTC)) == "final_oos"  # 上限なし


def test_split_bounds_unknown_name():
    with pytest.raises(ValueError):
        splits.split_bounds("test")


def test_walk_forward_folds_layout():
    folds = splits.walk_forward_folds(train_days=90, test_days=30)
    assert folds
    for f in folds:
        assert f.train_end == f.test_start
        assert (f.train_end - f.train_start).days == 90
        assert (f.test_end - f.test_start).days == 30
        assert f.test_end <= splits.FINAL_OOS_START  # final_oos に食い込まない
    # step = test_days なので test 区間は連続・非重複
    for a, b in zip(folds, folds[1:]):
        assert b.test_start == a.test_end


def test_walk_forward_folds_reject_final_oos():
    with pytest.raises(ValueError):
        splits.walk_forward_folds(30, 30, end=datetime(2026, 6, 1, tzinfo=UTC))

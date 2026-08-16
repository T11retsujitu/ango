"""screening エンジンの正しさ(fold・purge・帰無の作り方・最適化経路の同値性)。"""

from datetime import datetime, timedelta, timezone

import numpy as np
import pytest

from mce import tier0_prereg as P
from mce import tier0_screening as S

UTC = timezone.utc


def test_folds_are_expanding_with_frozen_geometry():
    folds = S.make_folds(datetime(2021, 1, 1, tzinfo=UTC), datetime(2025, 1, 1, tzinfo=UTC))
    assert len(folds) == 14  # 初期学習6ヶ月 + 3ヶ月ブロック
    assert folds[0].test_start == datetime(2021, 7, 1, tzinfo=UTC)
    assert folds[-1].test_end == datetime(2025, 1, 1, tzinfo=UTC)
    for fold in folds:
        assert fold.train_start == datetime(2021, 1, 1, tzinfo=UTC)  # expanding
        assert fold.train_end == fold.test_start
    starts = [f.test_start for f in folds]
    assert starts == sorted(starts) and len(set(starts)) == len(starts)


def test_short_dev_window_still_spans_two_calendar_years():
    folds = S.make_folds(P.DEV_START_T0B2, P.DEV_END)
    years = {f.test_start.year for f in folds}
    assert years == {2023, 2024}


def _synthetic_cell(n_slots: int = 4000, p_a: int = 4, p_x: int = 3, seed: int = 0) -> S.CellData:
    rng = np.random.default_rng(seed)
    a = rng.normal(size=(n_slots, p_a))
    x = a[:, :p_x] * 0.7 + rng.normal(size=(n_slots, p_x)) * 0.3  # A と相関を持たせる
    y = a @ rng.normal(size=p_a) + rng.normal(size=n_slots) * 3
    valid = np.ones(n_slots, dtype=bool)
    valid[:50] = False  # warmup 相当の無効行
    grid_ts = np.arange(n_slots) * S.BAR_MS
    train = [np.where(valid[: n_slots // 2])[0], np.where(valid[: 3 * n_slots // 4])[0]]
    test = [
        np.arange(n_slots // 2, 3 * n_slots // 4),
        np.arange(3 * n_slots // 4, n_slots),
    ]
    return S.CellData(grid_ts, a, x, y, valid, train, test, n_slots)


def test_fast_path_equals_naive_path_observed():
    cell = _synthetic_cell()
    naive = S.evaluate(cell)
    fast = S.evaluate_fast(cell, S.prepare_folds(cell))
    assert naive["dr2"] == pytest.approx(fast["dr2"], rel=1e-10, abs=1e-15)
    assert naive["r2_a"] == pytest.approx(fast["r2_a"], rel=1e-10, abs=1e-15)


def test_fast_path_equals_naive_path_under_placebo():
    """行が落ちる placebo でも、最適化経路と素朴経路は一致しなければならない。"""
    cell = _synthetic_cell()
    projections = S.fold_projections(cell)
    provider = S.a_projection_provider(projections, 3)
    naive = S.evaluate(cell, x_provider=provider)
    fast = S.evaluate_fast(cell, S.prepare_folds(cell), x_provider=provider)
    assert naive["dr2"] == pytest.approx(fast["dr2"], rel=1e-9, abs=1e-15)


def test_a_projection_placebo_preserves_correlation_with_a():
    """Bp は corr(X, A) を保存し、素朴シフト Bt は壊す(事前登録 §12.0)。"""
    cell = _synthetic_cell()
    projections = S.fold_projections(cell)
    x_bp = S.a_projection_provider(projections, 5)(0, cell.fold_train[0])
    x_bt = S.naive_shift_provider(cell, 5)(0, cell.fold_train[0])
    rows = np.arange(2000, 3000)

    def mean_abs_corr(x):
        return np.mean(
            [abs(np.corrcoef(cell.a[rows, j], x[rows, 0])[0, 1]) for j in range(cell.a.shape[1])]
        )

    observed = mean_abs_corr(cell.x)
    assert mean_abs_corr(x_bp) > 0.5 * observed  # 保存されている
    assert mean_abs_corr(x_bt) < 0.3 * observed  # 壊れている


def test_placebo_shift_set_respects_the_minimum_and_is_exhaustive():
    shifts = S.placebo_shifts(731, None)
    assert shifts[0] == P.PLACEBO["min_shift_days"] == 30
    assert shifts[-1] == 731 - 30
    assert len(shifts) == P.placebo_shift_count(731) == 672
    sampled = S.placebo_shifts(731, 200)
    assert len(sampled) == 200 and sampled[0] == 30 and len(set(sampled)) == 200


def test_ridge_path_matches_direct_solution():
    rng = np.random.default_rng(1)
    z = rng.normal(size=(500, 6))
    y = rng.normal(size=500)
    gram, rhs = z.T @ z, z.T @ y
    betas = S._ridge_path(gram, rhs)
    for i, alpha in enumerate(S.ALPHAS):
        direct = np.linalg.solve(gram + alpha * np.eye(6), rhs)
        assert np.allclose(betas[i], direct)


def test_holm_is_monotone_and_uses_the_full_family():
    entries = [{"p": p} for p in (0.001, 0.02, 0.4, 1.0)]
    S.holm(entries)
    adjusted = [e["p_holm"] for e in entries]
    assert adjusted == sorted(adjusted)
    assert adjusted[0] == pytest.approx(0.004)  # 0.001 * 4
    assert entries[0]["holm_significant"] and not entries[2]["holm_significant"]


def test_benjamini_hochberg_flags_the_expected_prefix():
    entries = [{"p": p} for p in (0.001, 0.02, 0.9, 1.0)]
    S.benjamini_hochberg(entries, q=0.10)
    assert entries[0]["bh_significant"] and not entries[2]["bh_significant"]


def test_causal_percentile_rank_never_looks_forward():
    values = np.arange(20000, dtype=float)  # 単調増加 -> 過去は必ず自分より小さい
    rank = S._causal_percentile_rank(values, window=P.ZSCORE_MIN_VALID_BARS + 100)
    tail = rank[~np.isnan(rank)]
    assert len(tail) > 0
    assert np.allclose(tail, 1.0)  # 単調増加なら常に「過去の全てより上」

    decreasing = -values
    rank_down = S._causal_percentile_rank(decreasing, window=P.ZSCORE_MIN_VALID_BARS + 100)
    tail_down = rank_down[~np.isnan(rank_down)]
    assert np.allclose(tail_down, 0.0)

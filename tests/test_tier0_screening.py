"""screening エンジンの正しさ(fold・purge・帰無の作り方・最適化経路の同値性)。"""

from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pytest

from mce import tier0_prereg as P
from mce import tier0_screening as S

UTC = timezone.utc


def test_folds_are_expanding_with_frozen_geometry():
    folds = S.make_folds(
        datetime(2021, 1, 1, tzinfo=UTC), datetime(2025, 1, 1, tzinfo=UTC)
    )
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


def _synthetic_cell(
    n_slots: int = 4000, p_a: int = 4, p_x: int = 3, seed: int = 0
) -> S.CellData:
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
            [
                abs(np.corrcoef(cell.a[rows, j], x[rows, 0])[0, 1])
                for j in range(cell.a.shape[1])
            ]
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


def test_placebo_distribution_is_independent_of_worker_count():
    """並列化は実行の都合であって、帰無分布を変えてはならない。"""
    cell = _synthetic_cell()
    caches = S.prepare_folds(cell)
    projections = S.fold_projections(cell)
    shifts = [3, 5, 7, 11]
    serial = S.placebo_distribution(cell, caches, projections, "bp", shifts, workers=1)
    parallel = S.placebo_distribution(
        cell, caches, projections, "bp", shifts, workers=3
    )
    assert np.array_equal(serial, parallel)  # 順序も値も一致


def test_placebo_distribution_supports_both_constructions():
    cell = _synthetic_cell()
    caches = S.prepare_folds(cell)
    projections = S.fold_projections(cell)
    bp = S.placebo_distribution(cell, caches, projections, "bp", [4, 9], workers=1)
    bt = S.placebo_distribution(cell, caches, projections, "bt", [4, 9], workers=1)
    assert len(bp) == len(bt) == 2
    assert not np.allclose(bp, bt)  # Bp と Bt は別物(Bt は corr(X,A) を壊す)


def test_stage2_promotion_threshold_can_reach_the_holm_floor():
    """第2段階の解像度が Holm 最小閾値に届くこと(事前登録 §12.3 の事前確認)。"""
    holm_floor = (
        0.05 / P.family_size() if hasattr(P, "family_size") else 0.05 / len(P.family())
    )
    for window_days in (1461, 731):  # dev A/B1/C 相当 と dev B2 相当
        k = P.placebo_shift_count(window_days)
        assert 1 / (1 + k) <= holm_floor
    assert S.STAGE2_MAX_EXCEED == 5


def test_extra_lag_provider_looks_backward_and_never_wraps():
    """公開遅延耐性(§17-6)の遅延は過去方向。窓頭は巡回させず NaN。"""
    cell = _synthetic_cell()
    provider = S.extra_lag_provider(cell, 3)
    rows = np.array([0, 1, 2, 3, 10, 500])
    got = provider.rows(0, rows)
    assert np.isnan(got[:3]).all()  # 窓頭は使えない
    assert np.allclose(got[3:], cell.x[rows[3:] - 3])
    assert np.allclose(provider(0, rows)[rows], got, equal_nan=True)  # 2経路が一致


def test_stability_flags_follow_the_frozen_thresholds():
    good = {
        "dr2": 1e-3,
        "dic": 1e-3,
        "fold_dr2": [1e-3] * 4,
        "dr2_by_year": {"2023": 1e-3, "2024": 2e-3},
        "sign": {"sign_agreement": 1.0},
        "leave_one_block_out_dr2": [1e-3] * 4,
        "dr2_without_most_influential_day": 5e-4,
    }
    assert S._stability_flags(good)["all_passed"]

    one_year = dict(good, dr2_by_year={"2023": 1e-3, "2024": -1e-3})
    flags = S._stability_flags(one_year)
    assert not flags["all_passed"] and not flags["passed"]["positive_calendar_years"]

    flipped = dict(good, dic=-1e-3)
    assert not S._stability_flags(flipped)["passed"]["dic_sign_matches_dr2"]


def test_dev_disposition_cannot_reach_go_without_confirmation():
    """dev だけで GO は出せない(§17-4 は confirmation 窓を要する)。"""
    entry = {
        "set": "T0-B1",
        "status": "tested",
        "holm_significant": True,
        "publication_delay": {"gate_passed": True},
        "stability": {"all_passed": True},
    }
    assert S._dev_disposition(entry) == "dev_pass_pending_confirmation"
    assert S._dev_disposition(dict(entry, holm_significant=False)).startswith("no_go")
    assert S._dev_disposition(dict(entry, status="insufficient_sample")).startswith(
        "no_go"
    )
    assert (
        S._dev_disposition(dict(entry, publication_delay={"gate_passed": False}))
        == "no_go_failed_set_specific_gate"
    )
    # T0-A は sham を上回ることが条件(§17-5)
    a_entry = dict(entry, set="T0-A", sham_s0={"observed_beats_sham": False})
    assert S._dev_disposition(a_entry) == "no_go_failed_set_specific_gate"


def test_bootstrap_ci_brackets_the_point_estimate():
    cell = _synthetic_cell()
    result = S.evaluate_fast(cell, S.prepare_folds(cell))
    ci = S._bootstrap_ci(result, cell.grid_ts, "dev")
    assert ci["ci_low"] <= result["dr2"] <= ci["ci_high"]
    assert ci["bootstrap_days"] > 1


def test_causal_percentile_rank_never_looks_forward():
    values = np.arange(20000, dtype=float)  # 単調増加 -> 過去は必ず自分より小さい
    rank = S._causal_percentile_rank(values, window=P.ZSCORE_MIN_VALID_BARS + 100)
    tail = rank[~np.isnan(rank)]
    assert len(tail) > 0
    assert np.allclose(tail, 1.0)  # 単調増加なら常に「過去の全てより上」

    decreasing = -values
    rank_down = S._causal_percentile_rank(
        decreasing, window=P.ZSCORE_MIN_VALID_BARS + 100
    )
    tail_down = rank_down[~np.isnan(rank_down)]
    assert np.allclose(tail_down, 0.0)


def test_resume_reuses_checkpoint_cells_verbatim(tmp_path):
    """再開は中断しなかった場合と同じ結果でなければならない(cell は互いに独立)。"""
    path = tmp_path / "cp.jsonl"
    rows = [
        {"set": "T0-A", "horizon_bars": 1, "target": "Y1", "dr2": 1.0},
        {"set": "T0-A", "horizon_bars": 1, "target": "Y2", "dr2": 2.0},
    ]
    path.write_text(
        "\n".join(__import__("json").dumps(r) for r in rows) + "\n", encoding="utf-8"
    )
    done = S.load_checkpoint(path)
    assert done[("T0-A", 1, "Y1")]["dr2"] == 1.0
    assert done[("T0-A", 1, "Y2")]["dr2"] == 2.0
    assert len(done) == 2


def test_resume_discards_a_torn_final_line():
    """強制終了で切れた末尾行は捨てる(その cell は再計算される)。"""
    import json
    import tempfile

    with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as handle:
        handle.write(
            json.dumps({"set": "T0-A", "horizon_bars": 1, "target": "Y1"}) + "\n"
        )
        handle.write('{"set": "T0-A", "horizon_ba')  # 書き込み途中で死んだ行
        name = handle.name
    done = S.load_checkpoint(Path(name))
    assert len(done) == 1 and ("T0-A", 1, "Y1") in done


def test_load_checkpoint_without_a_file_is_empty():
    assert S.load_checkpoint(Path("/nonexistent/cp.jsonl")) == {}


def test_confirmation_folds_are_four_quarters_trained_from_the_dev_start():
    """事前登録 §9.2: confirmation は 2025Q1〜Q4 の4ブロック・学習は dev 開始から。"""
    for info_set in P.INFORMATION_SETS:
        folds = S.make_folds(
            info_set.dev_start, P.CONFIRMATION_END, P.CONFIRMATION_START
        )
        assert len(folds) == 4, info_set.id
        assert folds[0].test_start == P.CONFIRMATION_START
        assert folds[-1].test_end == P.CONFIRMATION_END
        # 学習は confirmation 窓の内側ではなく dev 開始から伸びる
        for fold in folds:
            assert fold.train_start == info_set.dev_start
        assert {f.test_start.year for f in folds} == {2025}


def test_dev_fold_geometry_is_unchanged_by_the_confirmation_support():
    """dev の artifact は確定済みなので、fold 構成が変わってはならない。"""
    folds = S.make_folds(
        datetime(2021, 1, 1, tzinfo=UTC), datetime(2025, 1, 1, tzinfo=UTC)
    )
    assert len(folds) == 14
    assert folds[0].test_start == datetime(2021, 7, 1, tzinfo=UTC)
    assert len(S.make_folds(P.DEV_START_T0B2, P.DEV_END)) == 6


def test_confirmation_placebo_shift_set_matches_the_frozen_resolution():
    """§12.3 の表: conf の |S| = 306、最小 p = 3.26e-3。"""
    days = (P.CONFIRMATION_END - P.CONFIRMATION_START).days
    assert P.placebo_shift_count(days) == 306
    assert len(S.placebo_shifts(days, None)) == 306
    assert abs(1 / (1 + 306) - 3.26e-3) < 1e-5


def _conf_entry(**over):
    base = {
        "set": "T0-B1",
        "status": "tested",
        "dr2": 2.0e-3,
        "publication_delay": {"gate_passed": True},
        "sham_s0": {"observed_beats_sham": True},
    }
    return base | over


def test_confirmation_requires_sign_match_and_half_the_dev_magnitude():
    dev = {"dr2": 3.0e-3}
    assert S._confirmation_disposition(_conf_entry(dr2=2.0e-3), dev) == "go"
    # 符号反転は即 NO-GO
    assert (
        S._confirmation_disposition(_conf_entry(dr2=-1.0e-3), dev)
        == "no_go_sign_flipped_vs_dev"
    )
    # dev の半分未満は conditional hold(§17 CONDITIONAL HOLD)
    assert (
        S._confirmation_disposition(_conf_entry(dr2=1.0e-3), dev)
        == "conditional_hold_magnitude_below_half_of_dev"
    )
    # ちょうど半分は通る
    assert S._confirmation_disposition(_conf_entry(dr2=1.5e-3), dev) == "go"


def test_confirmation_keeps_the_set_specific_gates():
    dev = {"dr2": 3.0e-3}
    assert (
        S._confirmation_disposition(
            _conf_entry(publication_delay={"gate_passed": False}), dev
        )
        == "no_go_failed_set_specific_gate"
    )
    assert (
        S._confirmation_disposition(
            _conf_entry(set="T0-A", sham_s0={"observed_beats_sham": False}), dev
        )
        == "no_go_failed_set_specific_gate"
    )


def test_cells_not_promoted_in_dev_are_reported_not_dropped():
    """§18-4: 走らせなかった test も行として残り、理由が付く。"""
    entry = {"set": "T0-C", "status": "not_promoted_from_dev"}
    assert S._confirmation_disposition(entry, {}) == "no_go_not_promoted_from_dev"

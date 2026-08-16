"""Phase 1B clock-phase スクリーニングのテスト(合成データ)。

- 植え込んだ quarter-hour 方向効果を真の境界でのみ検出(shifted phase は不検出)
- 効果なしデータでは no_directional_structure
- permutation の seed 決定性 / report 全体の決定性
- Welch t の手計算一致
"""

import math
from datetime import datetime, timedelta, timezone

import numpy as np
import polars as pl
import pytest

from mce.features import build_features
from mce.labels import build_labels
from mce.normalize import normalize_candles
from mce.research import clock_phase

UTC = timezone.utc
BASE = datetime(2024, 5, 1, tzinfo=UTC)  # research 区間内
BAR_MS = 5 * 60_000
N_DAYS = 20


def _ohlcv(effect_bps: float) -> pl.DataFrame:
    """minute%15==5 のバーのリターンを +effect_bps にする
    (= phase 0 の行の fwd_return_5m が正になる)。ノイズは決定的な擬似乱数。"""
    t0 = int(BASE.timestamp() * 1000)
    rows, px = [], 60_000.0
    for i in range(288 * N_DAYS):
        minute = (i * 5) % 60
        noise = 2e-4 * math.sin(i * 12.9898) + 1e-4 * math.sin(i * 78.233)
        r = noise + (effect_bps * 1e-4 if minute % 15 == 5 else 0.0)
        px *= 1 + r
        rows.append([str(t0 + i * BAR_MS), str(px), str(px), str(px), str(px), "0", "5", "0", "1"])
    return normalize_candles(rows, "BTC-USDT-SWAP")


def _paths(tmp_path, effect_bps):
    ohlcv = _ohlcv(effect_bps)
    fp, lp = tmp_path / "features.parquet", tmp_path / "labels.parquet"
    build_features(ohlcv).write_parquet(fp)
    build_labels(ohlcv).write_parquet(lp)
    return fp, lp


def _cfg(fp, lp, n_perm=200):
    return clock_phase.ClockPhaseConfig(
        n_permutations=n_perm,
        start=BASE,
        end=BASE + timedelta(days=N_DAYS),
        features_path=fp,
        labels_path=lp,
    )


def test_planted_effect_detected_at_true_boundary(tmp_path):
    fp, lp = _paths(tmp_path, effect_bps=8.0)
    report = clock_phase.run(_cfg(fp, lp))
    fam = report["families"]["minute_mod_15"]
    by_phase = {r["phase"]: r for r in fam["phases"]}
    assert by_phase[0]["D1_pass"] is True  # 真の境界で検出
    assert by_phase[0]["fwd5m_mean_bps"] > 4.0
    # 効果量の argmax が真の境界にあり、verdict が clock_anchored になる
    assert fam["judgment"]["top_phase"] == 0
    assert fam["judgment"]["verdict"] == "clock_anchored_directional"


def test_shifted_effect_raises_placebo_alarm(tmp_path):
    """効果を phase 10 側(minute%15==0 のバーのリターン)に植えると、
    fwd_return_5m の効果は phase 10 の行に現れ、argmax 判定が placebo 警報を出す。"""
    t0 = int(BASE.timestamp() * 1000)
    rows, px = [], 60_000.0
    for i in range(288 * N_DAYS):
        minute = (i * 5) % 60
        noise = 2e-4 * math.sin(i * 12.9898) + 1e-4 * math.sin(i * 78.233)
        r = noise + (8e-4 if minute % 15 == 0 else 0.0)  # 境界バー自体のリターンが正
        px *= 1 + r
        rows.append([str(t0 + i * BAR_MS), str(px), str(px), str(px), str(px), "0", "5", "0", "1"])
    ohlcv = normalize_candles(rows, "BTC-USDT-SWAP")
    fp, lp = tmp_path / "f.parquet", tmp_path / "l.parquet"
    build_features(ohlcv).write_parquet(fp)
    build_labels(ohlcv).write_parquet(lp)
    report = clock_phase.run(_cfg(fp, lp))
    fam = report["families"]["minute_mod_15"]
    assert fam["judgment"]["top_phase"] == 10  # fwd で見るので1つ手前の行に現れる
    assert fam["judgment"]["verdict"] == "directional_at_shifted_phase"


def test_null_data_finds_no_structure(tmp_path):
    fp, lp = _paths(tmp_path, effect_bps=0.0)
    report = clock_phase.run(_cfg(fp, lp))
    for family in ("minute_mod_15", "minute_mod_60"):
        assert report["families"][family]["judgment"]["verdict"] == "no_directional_structure"


def test_report_deterministic(tmp_path):
    fp, lp = _paths(tmp_path, effect_bps=3.0)
    r1 = clock_phase.run(_cfg(fp, lp, n_perm=100))
    r2 = clock_phase.run(_cfg(fp, lp, n_perm=100))
    r1.pop("source_commit"), r2.pop("source_commit")
    assert r1 == r2


def test_perm_null_seed_determinism():
    y = np.array([math.sin(i * 0.7) for i in range(3000)])
    labels = np.array([i % 3 for i in range(3000)])
    a = clock_phase._perm_null_max(y, labels, 50, seed=1)
    b = clock_phase._perm_null_max(y, labels, 50, seed=1)
    c = clock_phase._perm_null_max(y, labels, 50, seed=2)
    assert np.array_equal(a, b)
    assert not np.array_equal(a, c)


def test_welch_t_matches_hand_calc():
    a = np.array([1.0, 2.0, 3.0, 4.0])
    b = np.array([2.0, 2.0, 2.0, 2.0, 2.0])
    t = clock_phase._welch_t(a, b)
    expected = (a.mean() - 2.0) / math.sqrt(a.var(ddof=1) / 4 + 0.0)
    assert t == pytest.approx(expected)


def test_descriptive_families_present(tmp_path):
    fp, lp = _paths(tmp_path, effect_bps=0.0)
    report = clock_phase.run(_cfg(fp, lp, n_perm=50))
    assert report["families"]["hour_utc"]["judgment"] is None
    assert len(report["families"]["hour_utc"]["phases"]) == 24
    assert len(report["families"]["weekday_utc"]["phases"]) == 7
    assert "m15_x_vol_regime" in report["families"]
    assert report["replication_class"] == "cross_exchange_validation"

"""engine + baselines + artifact のテスト(Determinism Test 含む)。"""

import json

import polars as pl
import pytest

from conftest import make_ohlcv_oc
from mce.backtest import baselines
from mce.backtest.costs import SCENARIOS
from mce.backtest.engine import run_backtest
from mce.experiments import build_artifact, next_experiment_id, save_artifact
from mce.features import build_features


def _features(n=30):
    bars = [(5 * i, 100.0 + i, 100.0 + i) for i in range(n)]
    return build_features(make_ohlcv_oc(bars))


def test_always_flat_and_buy_and_hold():
    feats = _features()
    flat = run_backtest(feats, baselines.always_flat(), SCENARIOS["base_taker"])
    assert flat.metrics["total_return"] == 0.0
    assert flat.metrics["turnover_total"] == 0.0

    bh = run_backtest(feats, baselines.buy_and_hold(), SCENARIOS["zero"])
    # entry @open[1]=101、以後 open-to-open の算術合算 = (129-101)/各open の合計
    expected = sum((101 + i + 1) / (101 + i) - 1 for i in range(28))
    assert bh.metrics["total_return"] == pytest.approx(expected)


def test_naive_momentum_uses_observable_only():
    feats = _features()
    res = run_backtest(feats, baselines.naive_momentum(), SCENARIOS["zero"])
    # 上昇一辺倒なので return_1h が確定した後は long(warmup 中は flat)
    assert res.metrics["exposure"] > 0
    assert res.bar_df["position"].max() == 1


def test_determinism_same_seed_same_result():
    feats = _features(50)
    m1 = run_backtest(feats, baselines.random_signal(seed=42), SCENARIOS["base_taker"]).metrics
    m2 = run_backtest(feats, baselines.random_signal(seed=42), SCENARIOS["base_taker"]).metrics
    assert m1 == m2  # bit 一致


def test_determinism_different_seed_differs():
    feats = _features(50)
    m1 = run_backtest(feats, baselines.random_signal(seed=1), SCENARIOS["zero"]).metrics
    m2 = run_backtest(feats, baselines.random_signal(seed=2), SCENARIOS["zero"]).metrics
    assert m1 != m2


def test_cost_aware_target():
    edge = pl.Series([15.0, -15.0, 5.0, -5.0, 0.0])
    t = baselines.cost_aware_target(edge, roundtrip_cost_bps=10.0)
    assert t.to_list() == [1, -1, 0, 0, 0]  # コスト以下のエッジでは abstain


def test_cost_aware_target_abstains_on_nan_and_null():
    # polars は NaN を任意の数より大きいと比較するため、明示的に abstain へ落とす
    edge = pl.Series([float("nan"), None, 15.0], dtype=pl.Float64)
    t = baselines.cost_aware_target(edge, roundtrip_cost_bps=10.0)
    assert t.to_list() == [0, 0, 1]


def test_artifact_roundtrip(tmp_path):
    feats = _features()
    res = run_backtest(feats, baselines.random_signal(seed=7), SCENARIOS["stress"])
    art = build_artifact(res, split="research", manifest_hashes={"features": "ab" * 32})
    path = save_artifact(art, runs_dir=tmp_path)
    rec = json.loads(path.read_text(encoding="utf-8"))
    assert rec["experiment_id"] == "EXP-0001"
    assert rec["data"]["source"] == "okx"
    assert rec["signal"] == {"feature_cutoff": "close_t", "execution": "open_t_plus_1"}
    assert rec["cost"]["scenario"] == "stress"
    assert rec["strategy"]["seed"] == 7
    assert rec["replication_class"] == "original"
    assert rec["metrics"]["bars"] == feats.height
    # 追記専用: 同 ID の上書きは拒否
    with pytest.raises(FileExistsError):
        save_artifact(art, runs_dir=tmp_path, experiment_id="EXP-0001")
    assert next_experiment_id(tmp_path) == "EXP-0002"


def test_artifact_rejects_unknown_replication_class(tmp_path):
    feats = _features()
    res = run_backtest(feats, baselines.always_flat(), SCENARIOS["zero"])
    with pytest.raises(ValueError):
        build_artifact(res, split="research", replication_class="exact")


def test_artifact_determinism_modulo_metadata(tmp_path):
    feats = _features(50)
    arts = []
    for _ in range(2):
        res = run_backtest(feats, baselines.random_signal(seed=3), SCENARIOS["base_taker"])
        arts.append(build_artifact(res, split="research"))
    assert arts[0] == arts[1]  # created_at/experiment_id 付与前は完全一致

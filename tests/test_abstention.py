"""Phase 1A abstention パイプラインのテスト(合成データ)。

- ラベル fwd_open_return_1h の執行整合性
- embargo(学習行がラベル horizon ぶん test 窓の手前で切れる)
- turnover: abstention <= model_sign
- 決定性(同一設定で report が一致)
- random_abstention の seed 決定性
"""

import math
from datetime import datetime, timedelta, timezone

import polars as pl
import pytest

from conftest import by_minute
from mce.features import build_features
from mce.labels import build_labels
from mce.normalize import normalize_candles
from mce.research import abstention

UTC = timezone.utc
BASE = datetime(2024, 3, 1, tzinfo=UTC)  # research 区間内
BAR_MS = 5 * 60_000
N_DAYS = 7


def _synthetic_ohlcv() -> pl.DataFrame:
    """ゆっくり振動する持続的リターン(過去リターンが未来を予測する構造を植え込む)。"""
    t0 = int(BASE.timestamp() * 1000)
    rows = []
    px = 50_000.0
    for i in range(288 * N_DAYS):
        r = 0.001 * math.sin(i / 80) + 0.0001 * math.sin(i * 1.7)
        px *= 1 + r
        rows.append([str(t0 + i * BAR_MS), str(px), str(px), str(px), str(px), "0", "5", "0", "1"])
    return normalize_candles(rows, "BTC-USDT-SWAP")


@pytest.fixture(scope="module")
def paths(tmp_path_factory):
    d = tmp_path_factory.mktemp("phase1a")
    ohlcv = _synthetic_ohlcv()
    fp, lp = d / "features.parquet", d / "labels.parquet"
    build_features(ohlcv).write_parquet(fp)
    build_labels(ohlcv).write_parquet(lp)
    return fp, lp


def _cfg(paths, cost="maker_low", seed=20260816):
    fp, lp = paths
    return abstention.AbstentionConfig(
        cost_scenario=cost,
        train_days=3,
        test_days=1,
        start=BASE,
        end=BASE + timedelta(days=N_DAYS),
        model_features=("return_5m", "return_1h", "volume_ratio_20"),  # 20d系は合成データ期間では全null
        features_path=fp,
        labels_path=lp,
        seed=seed,
    )


def test_open_label_is_execution_aligned():
    # open が close と異なる系列でラベル定義を直接検証
    rows = [
        [str(m * 60_000), str(o), str(o), str(o), str(c), "0", "1", "0", "1"]
        for m, o, c in [(5 * i, 100.0 + i, 200.0 + i) for i in range(15)]
    ]
    labels = build_labels(normalize_candles(rows, "X"))
    r = by_minute(labels, 0)
    # entry = open[t+1] = 101, exit = open[t+13] = 113(close の 200 系は使わない)
    assert abs(r["fwd_open_return_1h"] - (113.0 / 101.0 - 1)) < 1e-12
    assert by_minute(labels, 10)["fwd_open_return_1h"] is None  # 出口バーが無い


def test_train_frame_respects_embargo(paths):
    cfg = _cfg(paths)
    from mce.backtest import data as btdata, splits

    features = btdata.load_features("research", path=cfg.features_path)
    labels = pl.read_parquet(cfg.labels_path).select("ts", abstention.LABEL)
    joined = features.join(labels, on="ts", how="left")
    folds = splits.walk_forward_folds(cfg.train_days, cfg.test_days, start=cfg.start, end=cfg.end)
    train = abstention._train_frame(joined, folds[0], cfg.model_features)
    limit = folds[0].train_end - timedelta(minutes=abstention.EMBARGO_MINUTES)
    assert train.height > 100
    assert train["ts"].max() < limit  # ラベルが test 窓を覗く行が学習に入らない


def test_pipeline_runs_and_abstention_reduces_turnover(paths):
    report = abstention.run(_cfg(paths))
    rows = [r for r in report["folds"] if "arms" in r]
    assert len(rows) >= 3
    for r in rows:
        assert r["arms"]["abstention"]["turnover_total"] <= r["arms"]["model_sign"]["turnover_total"]
    j = report["judgment"]
    assert j["J1_pass"] in (True, False)
    assert j["verdict"] in ("abstention_supported", "abstention_rejected", "insufficient_trades")
    assert report["replication_class"] == "method_transfer"
    # 植え込んだ持続構造をモデルが拾い、maker コストではエッジが閾値を超えて取引が発生する
    assert sum(r["arms"]["abstention"]["trade_count"] for r in rows) > 0


def test_report_is_deterministic(paths):
    r1 = abstention.run(_cfg(paths))
    r2 = abstention.run(_cfg(paths))
    r1.pop("source_commit"), r2.pop("source_commit")
    assert r1 == r2


def test_random_abstention_seed_determinism():
    target = ([1] * 5 + [0] * 3 + [-1] * 4) * 10
    a = abstention._random_abstention(target, 0.5, seed=7)
    b = abstention._random_abstention(target, 0.5, seed=7)
    c = abstention._random_abstention(target, 0.5, seed=8)
    assert a == b
    assert a != c
    # 区間単位で残す(部分的に切り取らない)
    assert all(x in (0, 1, -1) for x in a)


def test_higher_cost_threshold_trades_less(paths):
    maker = abstention.run(_cfg(paths, cost="maker_low"))
    taker = abstention.run(_cfg(paths, cost="base_taker"))
    trades = lambda rep: sum(
        r["arms"]["abstention"]["trade_count"] for r in rep["folds"] if "arms" in r
    )
    assert trades(taker) <= trades(maker)  # 閾値10bps は 2bps より厳しい

"""funding mark proxy の **materiality 分析**の適合テスト。

**ネットワークを使わない。** REST の原文は引数で与える。
**strategy PnL を計算しない。** ここで検査するのは入力誤差の集計だけである。
"""

import hashlib
import json
from decimal import Decimal
from pathlib import Path

import pytest

from mce import funding_mark_materiality as fmm
from mce.funding_mark_materiality import (
    VOLATILITY_BAND_LABELS,
    analyse,
    funding_sign,
    summarise,
    volatility_band,
)

REPO = Path(__file__).resolve().parents[1]
T0 = 1577836800000  # 2020-01-01T00:00:00Z
H8 = 8 * 3600 * 1000


def bar(open_text: str = "100.00000000", high: str = "100.50000000",
        low: str = "99.50000000", close: str = "100.20000000",
        samples: int = 300) -> dict:
    return {"mark_open_text": open_text, "mark_high_text": high,
            "mark_low_text": low, "mark_close_text": close,
            "mark_samples": samples}


def events(*times: int) -> list[dict]:
    return [{"funding_time_ms": t} for t in times]


# --------------------------------------------------------------------------
# 誤差の定義
# --------------------------------------------------------------------------


def test_error_quantities_follow_the_frozen_definitions():
    """4つの誤差量が事前に固定した式のとおりであること。"""
    bars = {T0: bar(open_text="101.0")}
    result = analyse(events(T0), {T0: "100.0"}, {T0: "0.0001"}, bars)
    m = result["metrics"]
    # price_error = 101 - 100 = 1
    assert Decimal(m["price_error"]["p50"]) == Decimal("1")
    # bps_error = (101/100 - 1) * 10000 = 100 bps
    assert Decimal(m["bps_error"]["p50"]) == Decimal("100")
    # cashflow = 1 * 0.0001
    assert Decimal(m["cashflow_diff_per_btc"]["p50"]) == Decimal("0.0001")
    # notional_return = 0.0001 * 0.01
    assert Decimal(m["notional_return_diff"]["p50"]) == Decimal("0.000001")


def test_negative_funding_rate_flips_the_cashflow_sign():
    bars = {T0: bar(open_text="101.0")}
    result = analyse(events(T0), {T0: "100.0"}, {T0: "-0.0001"}, bars)
    assert Decimal(result["metrics"]["cashflow_diff_per_btc"]["p50"]) < 0
    assert result["by_funding_sign"]["negative"]["events"] == 1


def test_no_float_in_the_decimal_path():
    """float では消える差も誤差として現れる。"""
    proxy = "100.000000000000000001"
    rest = "100.0"
    assert float(proxy) == float(rest), "前提: float では区別できない"
    result = analyse(events(T0), {T0: rest}, {T0: "0.0001"}, {T0: bar(open_text=proxy)})
    assert result["mismatches"] == 1
    assert Decimal(result["metrics"]["abs_price_error"]["max"]) > 0


# --------------------------------------------------------------------------
# 層別は事前に固定した境界で切る
# --------------------------------------------------------------------------


@pytest.mark.parametrize("high,low,expected", [
    ("100.05", "100.00", "[0,10)bps"),      # 5 bps
    ("100.15", "100.00", "[10,25)bps"),     # 15 bps
    ("100.30", "100.00", "[25,50)bps"),     # 30 bps
    ("100.70", "100.00", "[50,100)bps"),    # 70 bps
    ("102.00", "100.00", "[100,inf)bps"),   # 200 bps
])
def test_volatility_bands_use_fixed_cut_points(high, low, expected):
    assert volatility_band(bar(open_text="100.00", high=high, low=low)) == expected


def test_volatility_band_is_undefined_when_the_range_is_absent():
    """帯を推測しない。"""
    assert volatility_band({"mark_open_text": "100.0"}) == "undefined"


def test_funding_sign_buckets():
    assert funding_sign(Decimal("0.0001")) == "positive"
    assert funding_sign(Decimal("-0.0001")) == "negative"
    assert funding_sign(Decimal("0")) == "zero"


def test_breakdowns_cover_year_volatility_and_sign():
    bars = {T0: bar(open_text="101.0"), T0 + H8: bar(open_text="100.0")}
    result = analyse(
        events(T0, T0 + H8), {T0: "100.0", T0 + H8: "100.0"},
        {T0: "0.0001", T0 + H8: "-0.0002"}, bars,
    )
    assert result["by_year"]["2020"]["events"] == 2
    assert set(result["by_funding_sign"]) == {"positive", "negative"}
    assert sum(b["events"] for b in result["by_volatility_band"].values()) == 2
    assert "literature_in_sample" in result["by_layer"]


# --------------------------------------------------------------------------
# 集計の誠実さ
# --------------------------------------------------------------------------


def test_summary_exposes_both_tails_and_the_sums():
    values = [Decimal("-3"), Decimal("0"), Decimal("5")]
    s = summarise(values)
    for key in ("min", "p01", "p05", "p50", "p90", "p95", "p99", "max",
                "sum", "sum_abs", "mean", "n"):
        assert key in s, key
    assert Decimal(s["min"]) == Decimal("-3")
    assert Decimal(s["max"]) == Decimal("5")
    assert Decimal(s["sum"]) == Decimal("2")
    assert Decimal(s["sum_abs"]) == Decimal("8")


def test_signed_cancellation_is_visible_and_not_used_to_claim_exactness():
    """符号付きの相殺が見えるようにする。相殺を「一致」に読み替えない。"""
    bars = {T0: bar(open_text="101.0"), T0 + H8: bar(open_text="99.0")}
    result = analyse(
        events(T0, T0 + H8), {T0: "100.0", T0 + H8: "100.0"},
        {T0: "0.0001", T0 + H8: "0.0001"}, bars,
    )
    assert Decimal(result["signed_net_error"]["price_usdt"]) == Decimal("0")
    assert Decimal(result["cumulative_absolute_error"]["price_usdt"]) == Decimal("2")
    assert result["mismatches"] == 2, "相殺したのに一致扱いになっている"
    assert result["exact_matches"] == 0


def test_unusable_primary_is_skipped_not_counted_as_zero_error():
    """利用不能なバーの誤差を 0 として集計に混ぜない。"""
    result = analyse(events(T0), {T0: "100.0"}, {T0: "0.0001"}, {})
    assert result["compared_events"] == 0
    assert len(result["skipped"]) == 1
    assert "not zero" in result["skipped"][0]["reason"]
    assert result["metrics"]["abs_price_error"]["n"] == 0
    assert result["metrics"]["abs_price_error"]["max"] is None


def test_stale_bar_is_skipped():
    bars = {T0: bar(samples=0)}
    result = analyse(events(T0), {T0: "100.0"}, {T0: "0.0001"}, bars)
    assert result["compared_events"] == 0
    assert result["skipped"][0]["mark_path_status"] == "stale_unverified"


def test_events_without_a_rest_mark_are_not_compared():
    """REST に mark が無い決済は誤差の母集団に入れない。"""
    result = analyse(events(T0, T0 + H8), {T0: "100.0"},
                     {T0: "0.0001", T0 + H8: "0.0001"},
                     {T0: bar(), T0 + H8: bar()})
    assert result["compared_events"] == 1


def test_every_mismatch_keeps_full_provenance():
    bars = {T0: bar(open_text="101.0"), T0 + H8: bar(open_text="100.0")}
    result = analyse(
        events(T0, T0 + H8), {T0: "100.0", T0 + H8: "100.0"},
        {T0: "0.0001", T0 + H8: "0.0001"}, bars,
    )
    assert len(result["all_mismatch_provenance"]) == result["mismatches"] == 1
    p = result["all_mismatch_provenance"][0]
    for key in ("funding_time_utc", "mark_bar_open_ms", "proxy_mark_open_text",
                "rest_mark_price_text", "funding_rate_text", "price_error",
                "bps_error", "cashflow_diff_per_btc", "notional_return_diff",
                "mark_path_status", "mark_samples", "volatility_band"):
        assert key in p, key


def test_module_does_not_import_the_strategy_layer():
    """strategy を計算する経路がモジュールに**存在しない**。

    語の出現ではなく **import と呼び出し**を見る(docstring には「PnL ではない」と
    書いてあるので、語の検査では自分の否定文に引っかかる)。
    """
    import ast
    import inspect
    tree = ast.parse(inspect.getsource(fmm))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
        elif isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
    for forbidden in ("mce.backtest.two_leg", "mce.backtest.rho",
                      "mce.backtest.engine", "mce.backtest.metrics",
                      "mce.backtest.costs", "mce.phase8_prereg"):
        assert forbidden not in imported, forbidden
    called = {
        n.func.id for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
    }
    for forbidden in ("rho", "arm_r_signal", "generate_arm_r_signals", "simulate"):
        assert forbidden not in called, forbidden
    assert "NOT a strategy PnL" in inspect.getsource(fmm), "宣言が消えている"


def test_frozen_artifacts_are_untouched():
    freeze = json.loads(
        (REPO / "experiments/phase8/carry_freeze_v1_8_5.json").read_text(encoding="utf-8")
    )
    for key in ("prereg_module", "engine_module", "mark_path_module", "signal_module",
                "splits_module", "rate_adapter", "prereg_doc"):
        entry = freeze[key]
        digest = hashlib.sha256((REPO / entry["path"]).read_bytes()).hexdigest()
        assert digest == entry["sha256"], entry["path"]

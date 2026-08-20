"""funding 決済時 markPrice の再構成**検証**の適合テスト。

**ネットワークを使わない。** REST の原文は引数で与える。
**取引系列を作らない。** rho / シグナル / return / PnL を計算しない。
"""

import hashlib
import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import polars as pl
import pytest

from mce import config, funding_mark_probe as fmp
from mce.backtest import mark_path
from mce.backtest.splits import FINAL_OOS_START
from mce.funding_mark_probe import (
    BAR_MS,
    DIAGNOSTIC_CANDIDATES,
    PRIMARY_FIELD,
    PRIMARY_USABLE_STATUSES,
    VERDICT_DETERMINISTIC,
    VERDICT_INCOMPLETE,
    VERDICT_PROXY_ONLY,
    availability_census,
    floor_5m,
    verify_exact_conformance,
)

UTC = timezone.utc
REPO = Path(__file__).resolve().parents[1]
T0 = 1577836800000  # 2020-01-01T00:00:00Z(5分グリッド上)
H8 = 8 * 3600 * 1000


def bar(open_ms: int, mark_open: str = "100.00000000",
        mark_close: str = "101.00000000", samples: int = 300) -> tuple[int, dict]:
    return open_ms, {"mark_open_text": mark_open, "mark_close_text": mark_close,
                     "mark_samples": samples}


def events(*funding_times: int) -> list[dict]:
    return [{"funding_time_ms": t} for t in funding_times]


# --------------------------------------------------------------------------
# バーの割り当て
# --------------------------------------------------------------------------


@pytest.mark.parametrize("offset_ms", [0, 1, 4, 16, 28, 47, 999, BAR_MS - 1])
def test_funding_time_maps_into_the_bar_that_contains_it(offset_ms):
    """実測の +0〜47ms ジッタが**同じバー**に落ちる。"""
    assert floor_5m(T0 + offset_ms) == T0


def test_bar_boundary_belongs_to_the_new_bar_and_one_ms_before_to_the_previous():
    assert floor_5m(T0 + BAR_MS) == T0 + BAR_MS, "境界時刻が前バーに落ちている"
    assert floor_5m(T0 + BAR_MS - 1) == T0, "境界直前が新バーに落ちている"
    assert floor_5m(T0 - 1) == T0 - BAR_MS


def test_measured_jitter_range_maps_to_one_bar():
    """実測された funding のジッタ幅(全 6,576 決済で 0〜47ms)は1本のバーに収まる。"""
    assert len({floor_5m(T0 + ms) for ms in range(0, 48)}) == 1


# --------------------------------------------------------------------------
# primary は mark_open 固定。diagnostic は混入しない
# --------------------------------------------------------------------------


def test_primary_is_mark_open_and_usable_statuses_are_frozen():
    assert PRIMARY_FIELD == "mark_open"
    assert PRIMARY_USABLE_STATUSES == (mark_path.OBSERVED, mark_path.VERIFIED_REPAIR)


def test_primary_uses_mark_open_not_mark_close():
    """同一バーの mark_close が REST と一致していても、判定は mark_open で行う。"""
    bars = dict([bar(T0, mark_open="100.00000000", mark_close="123.45000000")])
    result = verify_exact_conformance(events(T0), {T0: "123.45"}, bars)
    assert result["exact_decimal_matches"] == 0, "mark_close を採用している"
    assert result["verdict"] == VERDICT_PROXY_ONLY
    assert result["mismatch_detail"][0]["candidate_mark_open_text"] == "100.00000000"


def test_diagnostic_candidates_are_declared_but_never_promoted():
    bars = dict([bar(T0 - BAR_MS, mark_close="777.00000000"), bar(T0)])
    result = verify_exact_conformance(events(T0), {T0: "777.00000000"}, bars)
    assert result["candidate"]["diagnostic_only"] == list(DIAGNOSTIC_CANDIDATES)
    assert result["candidate"]["diagnostic_promoted_to_primary"] is False
    assert result["candidate"]["nearest_candidate_selection"] is False
    # 前バーの mark_close が一致していても matched にならない
    assert result["exact_decimal_matches"] == 0


def test_no_nearest_candidate_selection_happens():
    """候補のうち誤差が小さいものを事後に選ぶ経路が無い。"""
    bars = dict([bar(T0, mark_open="100.00000000", mark_close="100.00000001")])
    result = verify_exact_conformance(events(T0), {T0: "100.00000001"}, bars)
    assert result["exact_decimal_matches"] == 0


# --------------------------------------------------------------------------
# Decimal 完全一致
# --------------------------------------------------------------------------


def test_trailing_zeros_do_not_break_decimal_equality():
    """`89766.10000000` と `89766.1` は Decimal の数値比較では等しい。"""
    bars = dict([bar(T0, mark_open="89766.10000000")])
    result = verify_exact_conformance(events(T0), {T0: "89766.1"}, bars)
    assert result["exact_decimal_matches"] == 1
    assert result["verdict"] == VERDICT_DETERMINISTIC


def test_a_difference_beyond_float_resolution_is_still_a_mismatch():
    """float に落とすと消える差でも**不一致として検出する**。"""
    a = "90327.183130430000000001"
    b = "90327.18313043"
    assert float(a) == float(b), "前提: float では区別できない"
    assert Decimal(a) != Decimal(b)
    bars = dict([bar(T0, mark_open=a)])
    result = verify_exact_conformance(events(T0), {T0: b}, bars)
    assert result["exact_decimal_matches"] == 0
    assert result["verdict"] == VERDICT_PROXY_ONLY


def test_all_exact_yields_deterministic_reconstruction():
    bars = dict([bar(T0, mark_open="100.5"), bar(T0 + H8, mark_open="200.25")])
    result = verify_exact_conformance(
        events(T0, T0 + H8), {T0: "100.50000000", T0 + H8: "200.25000000"}, bars
    )
    assert result["one_to_one_mark_bar_matches"] == 2
    assert result["exact_decimal_matches"] == 2
    assert result["verdict"] == VERDICT_DETERMINISTIC


def test_one_mismatch_downgrades_to_proxy_only():
    bars = dict([bar(T0, mark_open="100.5"), bar(T0 + H8, mark_open="200.25")])
    result = verify_exact_conformance(
        events(T0, T0 + H8), {T0: "100.5", T0 + H8: "200.26"}, bars
    )
    assert result["verdict"] == VERDICT_PROXY_ONLY
    assert result["mismatches"] == 1
    assert len(result["mismatch_detail"]) == 1, "不一致明細が全件残っていない"


def test_percentiles_expose_both_tails_of_a_signed_quantity():
    """符号付きの量で**負の裾を隠さない**(上側だけ出すと片側に見える)。"""
    bars = dict([bar(T0, mark_open="99.0"), bar(T0 + H8, mark_open="101.0")])
    result = verify_exact_conformance(
        events(T0, T0 + H8), {T0: "100.0", T0 + H8: "100.0"}, bars
    )
    signed = result["signed_bps_difference"]
    assert Decimal(signed["min"]) < 0, "負の裾が出ていない"
    assert Decimal(signed["max"]) > 0
    for key in ("min", "p01", "p05", "p50", "p95", "p99", "max", "n"):
        assert key in signed, key
    # 絶対値の側は当然すべて非負
    assert Decimal(result["absolute_bps_difference"]["min"]) >= 0


def test_small_error_is_not_called_deterministic():
    """近似誤差が小さいだけでは deterministic にしない。"""
    bars = dict([bar(T0, mark_open="100.00000001")])
    result = verify_exact_conformance(events(T0), {T0: "100.00000000"}, bars)
    assert result["verdict"] == VERDICT_PROXY_ONLY
    assert Decimal(result["absolute_price_difference"]["max"]) > 0


# --------------------------------------------------------------------------
# 対応不能・曖昧
# --------------------------------------------------------------------------


def test_missing_mark_bar_is_incomplete_not_filled():
    result = verify_exact_conformance(events(T0), {T0: "100.5"}, {})
    assert result["verdict"] == VERDICT_INCOMPLETE
    assert result["unmappable"] == 1
    assert result["unmappable_detail"][0]["reason"] == "mark_bar_absent"
    assert result["one_to_one_mark_bar_matches"] == 0


def test_incomplete_takes_precedence_over_a_mismatch():
    bars = dict([bar(T0, mark_open="999.0")])
    result = verify_exact_conformance(
        events(T0, T0 + H8), {T0: "100.0", T0 + H8: "100.0"}, bars
    )
    assert result["mismatches"] == 1 and result["unmappable"] == 1
    assert result["verdict"] == VERDICT_INCOMPLETE


def test_many_funding_events_sharing_one_bar_are_refused():
    """多対一を拒否する(1本のバーに2つの決済が寄った)。"""
    bars = dict([bar(T0, mark_open="100.0")])
    result = verify_exact_conformance(
        events(T0, T0 + 60_000), {T0: "100.0", T0 + 60_000: "100.0"}, bars
    )
    assert result["ambiguous"] == 2
    assert result["verdict"] == VERDICT_INCOMPLETE
    assert result["one_to_one_mark_bar_matches"] == 0
    assert all(d["reason"] == "multiple_funding_events_share_one_mark_bar"
               for d in result["ambiguous_detail"])


def test_zero_comparisons_is_not_reported_as_deterministic():
    """比較対象が 0 件のとき「全件一致」を空虚に真としない。

    **「検証できなかった」が「決定的に再構成できた」に化ける**のを防ぐ。
    """
    result = verify_exact_conformance(events(T0), {}, dict([bar(T0)]))
    assert result["one_to_one_mark_bar_matches"] == 0
    assert result["verdict"] == fmp.VERDICT_NOT_VERIFIABLE
    assert result["verdict"] != VERDICT_DETERMINISTIC


def test_shared_bar_is_detected_even_when_only_one_event_has_a_rest_price():
    """多対一の検出を REST の有無で絞らない(片方だけ REST がある衝突も曖昧)。"""
    bars = dict([bar(T0, mark_open="100.0")])
    result = verify_exact_conformance(
        events(T0, T0 + 60_000), {T0: "100.0"}, bars
    )
    assert result["ambiguous"] == 1, "REST が片方に無い衝突を見逃している"
    assert result["one_to_one_mark_bar_matches"] == 0
    assert result["verdict"] == VERDICT_INCOMPLETE


def test_one_funding_event_maps_to_exactly_one_bar():
    """一対多が構成上ありえないこと(floor は単射的に1本を返す)。"""
    bars = dict([bar(T0), bar(T0 + BAR_MS)])
    result = verify_exact_conformance(events(T0), {T0: "100.00000000"}, bars)
    assert result["one_to_one_mark_bar_matches"] == 1
    assert result["timestamp_correspondence"]["rule"] == "floor_5m(funding_time)"


# --------------------------------------------------------------------------
# 状態の扱い(不明を補完しない)
# --------------------------------------------------------------------------


def test_stale_bar_is_not_usable_and_is_not_filled():
    """`mark_samples == 0` は `stale_unverified`。**primary に使わない。**"""
    bars = dict([bar(T0, mark_open="100.0", samples=0)])
    result = verify_exact_conformance(events(T0), {T0: "100.0"}, bars)
    assert result["unmappable"] == 1
    assert result["unmappable_detail"][0]["reason"].endswith(mark_path.STALE_UNVERIFIED)
    assert result["exact_decimal_matches"] == 0
    assert result["verdict"] == VERDICT_INCOMPLETE


def test_absent_bar_without_a_probe_is_route_unverified_not_unobservable():
    """遮断/未判定を「存在しない」に化けさせない(§32)。"""
    census = availability_census(events(T0), {}, {})
    assert census["mark_path_status_counts"] == {mark_path.ROUTE_UNVERIFIED: 1}
    assert census["availability_kinds"] == {"mark_bar_absent": 1}
    assert mark_path.SOURCE_UNOBSERVABLE not in census["mark_path_status_counts"]


def test_status_comes_from_the_frozen_module():
    bars = dict([bar(T0, samples=300)])
    assert fmp.bar_status(T0, bars) == mark_path.OBSERVED
    assert fmp.bar_status(T0, dict([bar(T0, samples=0)])) == mark_path.STALE_UNVERIFIED
    assert fmp.bar_status(T0 + BAR_MS, bars) == mark_path.ROUTE_UNVERIFIED


# --------------------------------------------------------------------------
# 検証2 — 可用性の区別
# --------------------------------------------------------------------------


def test_census_separates_unverified_conformance_from_missing_observation():
    """「REST に無い」と「mark バーが無い」を混ぜない。"""
    bars = dict([bar(T0), bar(T0 + H8)])
    census = availability_census(events(T0, T0 + H8, T0 + 2 * H8), {T0: "100.0"}, bars)
    assert census["availability_kinds"] == {
        "exact_conformance_verified": 1,
        "conformance_unverified_bar_present": 1,
        "mark_bar_absent": 1,
    }
    assert census["events_with_rest_mark_price"] == 1
    assert census["events_without_rest_mark_price"] == 2


def test_census_generates_and_stores_no_values():
    census = availability_census(events(T0), {}, {})
    assert census["values_generated"] is False
    assert census["values_stored"] is False
    assert census["interpolation"] == "none"


def test_census_records_every_unusable_event_in_detail():
    bars = dict([bar(T0, samples=0)])
    census = availability_census(events(T0, T0 + H8), {}, bars)
    detail = census["p1_p2_overlap_detail"]
    assert len(detail) == 2, "許容外の決済が明細に全件残っていない"
    regions = {d["region"] for d in detail}
    assert regions == {"P1_missing_vision_bars", "P2_stale_bars"}


# --------------------------------------------------------------------------
# 不変性
# --------------------------------------------------------------------------


def test_frozen_artifacts_are_untouched():
    freeze = json.loads(
        (REPO / "experiments/phase8/carry_freeze_v1_8_5.json").read_text(encoding="utf-8")
    )
    for key in ("mark_path_module", "prereg_module", "splits_module", "signal_module",
                "engine_module", "rate_adapter"):
        entry = freeze[key]
        digest = hashlib.sha256((REPO / entry["path"]).read_bytes()).hexdigest()
        assert digest == entry["sha256"], entry["path"]


def test_funding_events_are_filtered_by_the_seal_explicitly():
    """封印の適用が「たまたま範囲外の行が無い」ことに依存していない。"""
    import inspect
    source = inspect.getsource(fmp.load_funding_events)
    assert "cutoff_ms" in source and "FINAL_OOS_START" in source


def test_mark_gap_probe_path_is_repo_anchored_not_cwd_relative():
    """cwd 依存だと、別の場所から走らせたとき probe が無言で無効化される。"""
    assert fmp.MARK_GAP_PROBE_PATH.is_absolute()
    assert fmp.MARK_GAP_PROBE_PATH.name == "mark_gap_probe_v1.json"


def test_probe_module_does_not_write_any_parquet():
    """canonical parquet を書く経路がモジュールに無い。"""
    import inspect
    source = inspect.getsource(fmp)
    for forbidden in ("write_parquet", "merge_parquet", "to_parquet"):
        assert forbidden not in source, forbidden


@pytest.mark.skipif(
    not config.binance_funding_rate_parquet().exists(), reason="funding 未取得"
)
def test_canonical_funding_parquet_is_unchanged_by_the_probe():
    manifest = json.loads(
        (REPO / "data/manifests/binance_funding_rate_funding_rate_BTCUSDT.json")
        .read_text(encoding="utf-8")
    )
    path = config.binance_funding_rate_parquet()
    assert hashlib.sha256(path.read_bytes()).hexdigest() == manifest["sha256"]
    df = pl.read_parquet(path)
    assert "mark_price" not in df.columns
    assert df["ts"].max() < FINAL_OOS_START


@pytest.mark.skipif(
    not config.binance_mark_price_parquet().exists(), reason="mark price 未取得"
)
def test_canonical_mark_parquet_is_unchanged_by_the_probe():
    manifest = json.loads(
        (REPO / "data/manifests/binance_mark_price_mark_price_BTCUSDT_5m.json")
        .read_text(encoding="utf-8")
    )
    df = pl.read_parquet(config.binance_mark_price_parquet())
    assert df.height == manifest["rows"]
    assert df.columns == manifest["columns"]

"""funding 決済 mark の source resolver の適合テスト(v1.8.6 草案 §4 / §5 / §8)。

**I/O もネットワークも使わない。** 純粋関数だけを検査する。
**取引系列を作らない。** rho / シグナル / return / PnL を計算しない。
"""

import hashlib
import json
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from mce.backtest import mark_path
from mce.backtest.splits import FINAL_OOS_START
from mce.funding_mark_resolver import (
    FIDELITY_EXACT_REST,
    FIDELITY_KLINE_PROXY,
    FIDELITY_UNAVAILABLE,
    FUNDING_MARK_FIDELITIES,
    FUNDING_MARK_PROXY_IS_EXACT,
    LAYER_FIDELITY_EXACT,
    LAYER_FIDELITY_PARTIAL,
    LAYER_FIDELITY_UNKNOWN,
    PROXY_BAR_MISSING,
    PROXY_BLOCKED_REASONS,
    PROXY_NOT_ATTEMPTED,
    PROXY_OPEN_NON_POSITIVE,
    PROXY_OPEN_UNPARSEABLE,
    PROXY_ROUTE_UNVERIFIED,
    PROXY_SOURCE_UNOBSERVABLE,
    PROXY_STALE_UNVERIFIED,
    REST_ABSENT,
    REST_INVALID_REASONS,
    REST_NON_POSITIVE,
    REST_UNPARSEABLE,
    REST_VALID,
    SENSITIVITY_FIDELITY,
    SENSITIVITY_NOT_ATTEMPTED,
    SENSITIVITY_BAR_MISSING,
    SENSITIVITY_BLOCKED_REASONS,
    SENSITIVITY_CLOSE_NON_POSITIVE,
    SENSITIVITY_ROUTE_UNVERIFIED,
    SENSITIVITY_SOURCE,
    SENSITIVITY_STALE_UNVERIFIED,
    SOURCE_KLINE_OPEN,
    SOURCE_REST,
    FundingMarkInputs,
    FundingMarkResolverError,
    MarkBarInput,
    layer_fidelity,
    layer_fidelity_for,
    floor_5m,
    resolve,
    resolve_layer_name,
    resolve_all,
)

UTC = timezone.utc
REPO = Path(__file__).resolve().parents[1]
TS = datetime(2024, 6, 1, 8, tzinfo=UTC)          # layer 1
TS_L2 = datetime(2025, 8, 1, 8, tzinfo=UTC)       # layer 2
BAR_TS = datetime(2024, 6, 1, 8, tzinfo=UTC)
PREV_TS = BAR_TS - timedelta(minutes=5)


def bar(open_text="100.00000000", close_text="101.00000000",
        status=mark_path.OBSERVED, ts=None, settlement=TS) -> MarkBarInput:
    """`floor_5m(settlement)` に載ったバー(resolver は位置を検査する)。"""
    return MarkBarInput(ts=ts or floor_5m(settlement), mark_path_status=status,
                        mark_open_text=open_text, mark_close_text=close_text)


def prev(close_text="99.00000000", status=mark_path.OBSERVED, settlement=TS) -> MarkBarInput:
    return MarkBarInput(ts=floor_5m(settlement) - timedelta(minutes=5),
                        mark_path_status=status,
                        mark_open_text="98.00000000", mark_close_text=close_text)


def inputs(rest="50000.12345678", ts=TS, **kw) -> FundingMarkInputs:
    base = dict(
        ts=ts,
        rest_funding_time=ts if rest is not None else None,
        rest_mark_price_text=rest,
        mark_bar=bar(settlement=ts),
        previous_mark_bar=prev(settlement=ts),
        funding_rate_text="0.00010000",
    )
    base.update(kw)
    return FundingMarkInputs(**base)


# --------------------------------------------------------------------------
# 有効な REST が常に最優先
# --------------------------------------------------------------------------


def test_valid_rest_always_wins():
    r = resolve(inputs(rest="50000.12345678"))
    assert r.primary_value == Decimal("50000.12345678")
    assert r.primary_source == SOURCE_REST
    assert r.primary_fidelity == FIDELITY_EXACT_REST
    assert r.primary_reason == REST_VALID
    assert r.resolution_permitted is True


def test_valid_rest_wins_even_when_the_proxy_would_be_available_and_closer():
    """proxy が「近い」ことは REST を上書きする理由にならない。"""
    r = resolve(inputs(rest="50000.00000000", mark_bar=bar(open_text="50000.00000001")))
    assert r.primary_value == Decimal("50000.00000000")
    assert r.primary_source == SOURCE_REST


def test_valid_rest_wins_even_when_the_mark_path_is_unusable():
    """mark 経路が壊れていても、REST が有効なら解決できる。"""
    r = resolve(inputs(mark_bar=bar(status=mark_path.STALE_UNVERIFIED)))
    assert r.primary_fidelity == FIDELITY_EXACT_REST
    assert r.resolution_permitted is True
    assert r.proxy_reason == PROXY_NOT_ATTEMPTED


def test_valid_rest_leaves_sensitivity_untouched():
    """REST がある決済は sensitivity でも REST のまま(§8)。"""
    r = resolve(inputs())
    assert r.sensitivity_value is None
    assert r.sensitivity_source is None
    assert r.sensitivity_fidelity is None
    assert r.sensitivity_reason == SENSITIVITY_NOT_ATTEMPTED


# --------------------------------------------------------------------------
# REST が無効なときだけ proxy
# --------------------------------------------------------------------------


@pytest.mark.parametrize("rest_text,expected_reason", [
    (None, REST_ABSENT),
    ("", REST_ABSENT),
    ("   ", REST_ABSENT),
    ("abc", REST_UNPARSEABLE),
    ("NaN", REST_UNPARSEABLE),
    ("Infinity", REST_UNPARSEABLE),
    ("0", REST_NON_POSITIVE),
    ("-1.5", REST_NON_POSITIVE),
])
def test_invalid_rest_falls_through_to_the_proxy_with_its_reason(rest_text, expected_reason):
    """**「空」だけでなく parse 不能・非正でも proxy 候補へ進む**(§4 step 2)。"""
    r = resolve(inputs(rest=rest_text, rest_funding_time=TS))
    assert r.rest_validity == expected_reason
    assert r.primary_reason == expected_reason, "REST が無効だった理由を失っている"
    assert r.primary_value == Decimal("100.00000000")
    assert r.primary_source == SOURCE_KLINE_OPEN
    assert r.primary_fidelity == FIDELITY_KLINE_PROXY
    assert r.resolution_permitted is True


def test_proxy_is_never_labelled_exact():
    r = resolve(inputs(rest=None))
    assert r.primary_fidelity != FIDELITY_EXACT_REST
    assert r.primary_fidelity == FIDELITY_KLINE_PROXY
    assert FUNDING_MARK_PROXY_IS_EXACT is False
    assert r.to_dict()["proxy_is_exact"] is False


def test_rest_invalid_reason_survives_even_when_the_proxy_also_fails():
    """proxy も駄目でも、REST が無効だった理由は残る。"""
    r = resolve(inputs(rest="-1", mark_bar=None))
    assert r.rest_validity == REST_NON_POSITIVE
    assert r.primary_reason == REST_NON_POSITIVE
    assert r.proxy_reason == PROXY_BAR_MISSING
    assert r.primary_fidelity == FIDELITY_UNAVAILABLE


# --------------------------------------------------------------------------
# stale / unverified なバーを proxy に使わない
# --------------------------------------------------------------------------


@pytest.mark.parametrize("status,expected", [
    (mark_path.ROUTE_UNVERIFIED, PROXY_ROUTE_UNVERIFIED),
    (mark_path.STALE_UNVERIFIED, PROXY_STALE_UNVERIFIED),
    (mark_path.SOURCE_UNOBSERVABLE, PROXY_SOURCE_UNOBSERVABLE),
])
def test_unacceptable_mark_path_blocks_the_proxy(status, expected):
    """**値があっても使わない。** 横引きも補間もしない。"""
    r = resolve(inputs(rest=None, mark_bar=bar(open_text="100.0", status=status)))
    assert r.primary_value is None
    assert r.primary_source is None
    assert r.primary_fidelity == FIDELITY_UNAVAILABLE
    assert r.proxy_reason == expected
    assert r.resolution_permitted is False


@pytest.mark.parametrize("status", [mark_path.OBSERVED, mark_path.VERIFIED_REPAIR])
def test_acceptable_statuses_allow_the_proxy(status):
    r = resolve(inputs(rest=None, mark_bar=bar(status=status)))
    assert r.primary_fidelity == FIDELITY_KLINE_PROXY
    assert r.resolution_permitted is True


@pytest.mark.parametrize("open_text,expected", [
    (None, PROXY_BAR_MISSING),
    ("", PROXY_BAR_MISSING),
    ("xyz", PROXY_OPEN_UNPARSEABLE),
    ("0", PROXY_OPEN_NON_POSITIVE),
    ("-3", PROXY_OPEN_NON_POSITIVE),
])
def test_proxy_value_problems_are_classified(open_text, expected):
    r = resolve(inputs(rest=None, mark_bar=bar(open_text=open_text)))
    assert r.proxy_reason == expected
    assert r.primary_fidelity == FIDELITY_UNAVAILABLE
    assert r.primary_value is None


def test_missing_mark_bar_is_unavailable_not_filled():
    r = resolve(inputs(rest=None, mark_bar=None))
    assert r.primary_value is None
    assert r.proxy_reason == PROXY_BAR_MISSING
    assert r.resolution_permitted is False


# --------------------------------------------------------------------------
# sensitivity は primary に影響しない
# --------------------------------------------------------------------------


def test_sensitivity_uses_the_previous_bar_close_only_when_rest_is_invalid():
    r = resolve(inputs(rest=None))
    assert r.sensitivity_value == Decimal("99.00000000")
    assert r.sensitivity_source == SENSITIVITY_SOURCE
    assert r.sensitivity_fidelity == SENSITIVITY_FIDELITY


def test_sensitivity_never_changes_the_primary_value():
    """直前 close を動かしても primary は 1 ビットも変わらない。"""
    a = resolve(inputs(rest=None, previous_mark_bar=prev(close_text="99.0")))
    b = resolve(inputs(rest=None, previous_mark_bar=prev(close_text="123456.0")))
    assert a.primary_value == b.primary_value == Decimal("100.00000000")
    assert a.primary_source == b.primary_source
    assert a.primary_fidelity == b.primary_fidelity
    assert a.sensitivity_value != b.sensitivity_value


def test_sensitivity_does_not_rescue_an_unavailable_primary():
    """primary が使えないとき、sensitivity があっても解決を許可しない。"""
    r = resolve(inputs(rest=None, mark_bar=None))
    assert r.sensitivity_value == Decimal("99.00000000")
    assert r.primary_fidelity == FIDELITY_UNAVAILABLE
    assert r.resolution_permitted is False, "sensitivity で救済している"


def test_sensitivity_label_does_not_pollute_the_three_fidelities():
    """sensitivity は自分のラベルを持つ(§5.1)。"""
    r = resolve(inputs(rest=None))
    assert r.sensitivity_fidelity not in FUNDING_MARK_FIDELITIES
    assert r.sensitivity_fidelity == SENSITIVITY_FIDELITY


@pytest.mark.parametrize("status,expected", [
    (mark_path.ROUTE_UNVERIFIED, SENSITIVITY_ROUTE_UNVERIFIED),
    (mark_path.STALE_UNVERIFIED, SENSITIVITY_STALE_UNVERIFIED),
])
def test_sensitivity_also_refuses_unacceptable_previous_bars(status, expected):
    r = resolve(inputs(rest=None, previous_mark_bar=prev(status=status)))
    assert r.sensitivity_value is None
    assert r.sensitivity_source is None
    assert r.sensitivity_reason == expected


def test_no_or_rule_and_no_nearest_candidate_selection():
    """`open` が使えないとき、直前 `close` が primary へ昇格しない。"""
    r = resolve(inputs(rest=None, mark_bar=bar(open_text=None)))
    assert r.primary_value is None, "OR 規則で救済している"
    assert r.sensitivity_value == Decimal("99.00000000")
    assert r.primary_fidelity == FIDELITY_UNAVAILABLE


def test_primary_does_not_pick_whichever_is_closer_to_rest():
    """REST が無効なので比較対象すら無い。近い方を選ぶ経路が無いことを固定する。"""
    r = resolve(inputs(rest=None, mark_bar=bar(open_text="100.0"),
                       previous_mark_bar=prev(close_text="100.0")))
    assert r.primary_source == SOURCE_KLINE_OPEN
    assert r.primary_value == Decimal("100.0")


# --------------------------------------------------------------------------
# 分岐の排他性と決定性
# --------------------------------------------------------------------------


def test_branches_are_mutually_exclusive():
    """fidelity は必ず 3 値のどれか1つ。source との対応も1対1。"""
    cases = [
        inputs(),
        inputs(rest=None),
        inputs(rest=None, mark_bar=None),
        inputs(rest="0"),
        inputs(rest="bad", mark_bar=bar(status=mark_path.ROUTE_UNVERIFIED)),
    ]
    seen = set()
    for case in cases:
        r = resolve(case)
        assert r.primary_fidelity in FUNDING_MARK_FIDELITIES
        pair = (r.primary_fidelity, r.primary_source)
        seen.add(pair)
        if r.primary_fidelity == FIDELITY_EXACT_REST:
            assert r.primary_source == SOURCE_REST and r.primary_value is not None
        elif r.primary_fidelity == FIDELITY_KLINE_PROXY:
            assert r.primary_source == SOURCE_KLINE_OPEN and r.primary_value is not None
        else:
            assert r.primary_source is None and r.primary_value is None
    assert len(seen) == 3, seen


def test_resolution_permitted_matches_the_fidelity_exactly():
    for case in (inputs(), inputs(rest=None), inputs(rest=None, mark_bar=None)):
        r = resolve(case)
        assert r.resolution_permitted == (r.primary_fidelity != FIDELITY_UNAVAILABLE)


def test_reasons_come_from_the_frozen_vocabularies():
    for case in (inputs(), inputs(rest=None), inputs(rest="x"), inputs(rest="0"),
                 inputs(rest=None, mark_bar=None)):
        r = resolve(case)
        assert r.rest_validity in (REST_VALID, *REST_INVALID_REASONS)
        assert (r.proxy_reason in PROXY_BLOCKED_REASONS
                or r.proxy_reason in (PROXY_NOT_ATTEMPTED, "proxy_used_mark_open"))


def test_same_input_gives_byte_identical_output():
    source = inputs(rest=None)
    a = json.dumps(resolve(source).to_dict(), sort_keys=True, ensure_ascii=False)
    b = json.dumps(resolve(source).to_dict(), sort_keys=True, ensure_ascii=False)
    assert a == b
    assert hashlib.sha256(a.encode()).hexdigest() == hashlib.sha256(b.encode()).hexdigest()


def test_inputs_are_not_mutated():
    source = inputs(rest=None)
    before = deepcopy(source)
    resolve(source)
    assert source == before


def test_trace_is_deterministic_and_records_each_branch():
    r = resolve(inputs(rest="0"))
    assert r.trace == (
        f"seal_ok:{TS.isoformat()}",
        "rest:non_positive",
        "proxy:proxy_used_mark_open",
        "sensitivity:sensitivity_used_previous_mark_close",
        f"primary:{FIDELITY_KLINE_PROXY}",
    )
    assert resolve(inputs(rest="0")).trace == r.trace


# --------------------------------------------------------------------------
# 拒否すべき入力(黙って fallback しない)
# --------------------------------------------------------------------------


def test_unknown_mark_path_status_is_refused():
    with pytest.raises(FundingMarkResolverError, match="凍結されていない"):
        resolve(inputs(rest=None, mark_bar=bar(status="probably_fine")))


def test_unknown_previous_bar_status_is_refused():
    with pytest.raises(FundingMarkResolverError):
        resolve(inputs(rest=None, previous_mark_bar=prev(status="looks_ok")))


def test_sealed_settlement_is_refused():
    with pytest.raises(FundingMarkResolverError, match="封印域"):
        resolve(inputs(ts=FINAL_OOS_START))


def test_naive_timestamp_is_refused():
    with pytest.raises(FundingMarkResolverError, match="tz-naive"):
        resolve(inputs(ts=datetime(2024, 6, 1, 8)))


def test_empty_rest_without_a_reconciliation_row_still_falls_through_to_the_proxy():
    """空の REST は「照合行が無い」のが正常。**provenance ガードで塞がない。**"""
    r = resolve(inputs(rest="", rest_funding_time=None))
    assert r.rest_validity == REST_ABSENT
    assert r.primary_fidelity == FIDELITY_KLINE_PROXY
    assert r.resolution_permitted is True


def test_a_rest_row_for_a_different_settlement_is_refused():
    """別の決済の REST 行を `exact_rest` にしない。"""
    with pytest.raises(FundingMarkResolverError, match="別の決済"):
        resolve(inputs(rest="50000.0", rest_funding_time=TS + timedelta(hours=8)))
    # 実測されたサブ秒ジッタは許容範囲
    ok = resolve(inputs(rest="50000.0", rest_funding_time=TS + timedelta(milliseconds=47)))
    assert ok.primary_fidelity == FIDELITY_EXACT_REST


@pytest.mark.parametrize("bad_ts", [
    BAR_TS + timedelta(minutes=5),    # 次バー(未来)
    BAR_TS - timedelta(minutes=5),    # 前バー
])
def test_a_proxy_bar_at_the_wrong_position_is_refused(bad_ts):
    """`floor_5m(funding_time)` 以外のバーを黙って proxy にしない。"""
    wrong = MarkBarInput(ts=bad_ts, mark_path_status=mark_path.OBSERVED,
                         mark_open_text="100.0", mark_close_text="101.0")
    with pytest.raises(FundingMarkResolverError, match="floor_5m"):
        resolve(inputs(rest=None, mark_bar=wrong))


def test_a_sensitivity_bar_that_is_not_the_previous_one_is_refused():
    """同一バーや次バーの close を sensitivity にしない。"""
    same_bar = MarkBarInput(ts=BAR_TS, mark_path_status=mark_path.OBSERVED,
                            mark_close_text="99.0")
    with pytest.raises(FundingMarkResolverError, match="直前バー"):
        resolve(inputs(rest=None, previous_mark_bar=same_bar))


@pytest.mark.parametrize("text", ["1_0", "1 0", "0x10", "1,000", "1.2.3", "--5"])
def test_corrupted_numeric_text_is_unparseable_not_a_value(text):
    """`Decimal` の緩い構文をそのまま通さない(`1_0` が 10 になってしまう)。"""
    r = resolve(inputs(rest=text, rest_funding_time=TS))
    assert r.rest_validity == REST_UNPARSEABLE
    assert r.primary_source == SOURCE_KLINE_OPEN  # 規則どおり proxy へ
    broken_bar = resolve(inputs(rest=None, mark_bar=bar(open_text=text)))
    assert broken_bar.proxy_reason == PROXY_OPEN_UNPARSEABLE


def test_surrounding_whitespace_is_still_accepted():
    """外側の空白は値の破損ではない。"""
    r = resolve(inputs(rest="  50000.5  "))
    assert r.primary_value == Decimal("50000.5")


def test_sensitivity_reasons_do_not_borrow_the_primary_vocabulary():
    """sensitivity は `mark_close` を読むので、`mark_open_*` と記録しない。"""
    r = resolve(inputs(rest=None, previous_mark_bar=prev(close_text="-1")))
    assert r.sensitivity_reason == SENSITIVITY_CLOSE_NON_POSITIVE
    assert "mark_open" not in r.sensitivity_reason
    assert r.sensitivity_reason in SENSITIVITY_BLOCKED_REASONS
    # 同じ行で primary は正常に proxy を採用できている(矛盾した記録が出ない)
    assert r.primary_fidelity == FIDELITY_KLINE_PROXY
    assert r.proxy_reason == "proxy_used_mark_open"


def test_output_carries_the_refusal_flags_required_by_the_artifact_spec():
    d = resolve(inputs()).to_dict()
    assert d["proxy_is_exact"] is False
    assert d["or_rule_used"] is False
    assert d["nearest_candidate_selection"] is False
    assert d["interpolation"] == "none"
    assert d["fidelity_describes"] == "primary_selection_only"
    assert d["proxy_bar_rule"] == "floor_5m(funding_time)"


def test_rest_value_without_provenance_is_refused():
    """値だけあって出所が無い照合行は分類できない。**推測で通さない。**"""
    with pytest.raises(FundingMarkResolverError, match="provenance"):
        resolve(inputs(rest="50000.0", rest_funding_time=None))


# --------------------------------------------------------------------------
# layer の区分は**規則から導く**
# --------------------------------------------------------------------------


def test_layer_class_is_derived_not_hardcoded():
    l1 = [resolve(inputs(rest="50000.0", ts=TS)),
          resolve(inputs(rest=None, ts=TS))]          # exact + proxy
    l2 = [resolve(inputs(rest="50000.0", ts=TS_L2))]  # exact のみ
    table = layer_fidelity([*l1, *l2])
    assert table["literature_in_sample"]["class"] == LAYER_FIDELITY_PARTIAL
    assert table["contaminated_confirmation"]["class"] == LAYER_FIDELITY_EXACT


def test_layer3_is_unknown_until_observed_not_assumed_exact():
    """観測が無い layer を exact と推定しない。**表からも落とさない**(§6)。"""
    observed = [resolve(inputs(rest="50000.0", ts=TS_L2))]
    assert layer_fidelity_for("phase8_prospective_final", observed) == LAYER_FIDELITY_UNKNOWN
    assert layer_fidelity_for("layer3", observed) == LAYER_FIDELITY_UNKNOWN
    assert layer_fidelity_for("layer3", []) == LAYER_FIDELITY_UNKNOWN
    table = layer_fidelity(observed)
    assert "phase8_prospective_final" in table, "layer 3 が表から消えている"
    assert table["phase8_prospective_final"]["events"] == 0


def test_layer_short_names_from_the_amendment_resolve():
    """草案の `layer1` / `layer2` でも引ける。**黙って unknown を返さない**。"""
    rows = [resolve(inputs(rest="1.0", ts=TS)), resolve(inputs(rest=None, ts=TS)),
            resolve(inputs(rest="1.0", ts=TS_L2))]
    assert layer_fidelity_for("layer1", rows) == LAYER_FIDELITY_PARTIAL
    assert layer_fidelity_for("literature_in_sample", rows) == LAYER_FIDELITY_PARTIAL
    assert layer_fidelity_for("layer2", rows) == LAYER_FIDELITY_EXACT
    assert layer_fidelity(rows)["literature_in_sample"]["alias"] == "layer1"


def test_unknown_layer_name_is_refused_not_silently_unknown():
    """名前の間違いを「観測が無い」に化けさせない。"""
    rows = [resolve(inputs(rest="1.0", ts=TS))]
    with pytest.raises(FundingMarkResolverError, match="未知の layer 名"):
        layer_fidelity_for("layer_one", rows)
    with pytest.raises(FundingMarkResolverError):
        resolve_layer_name("literature")


def test_canonical_timeline_row_without_a_price_keeps_its_status():
    """§32 の欠測行を渡すと、**状態由来の理由が残る**(§6 の区別が消えない)。

    Vision に行が無いバーでも、canonical タイムラインは状態つきの行を持つ。
    その行を渡すのが呼び出し規約である。
    """
    missing_row = MarkBarInput(
        ts=BAR_TS, mark_path_status=mark_path.ROUTE_UNVERIFIED,
        mark_open_text=None, mark_close_text=None,
    )
    r = resolve(inputs(rest=None, mark_bar=missing_row))
    assert r.primary_fidelity == FIDELITY_UNAVAILABLE
    assert r.proxy_reason == PROXY_ROUTE_UNVERIFIED, "状態が mark_bar_missing に潰れている"
    assert r.mark_path_status == mark_path.ROUTE_UNVERIFIED
    assert r.mark_bar_ts == BAR_TS
    # source_unobservable と取り違えない
    assert r.proxy_reason != PROXY_SOURCE_UNOBSERVABLE


def test_row_present_but_price_absent_is_a_missing_bar_not_a_parse_failure():
    """行はあるが価格が無い場合と、parse 不能を混ぜない。"""
    r = resolve(inputs(rest=None, mark_bar=bar(open_text=None)))
    assert r.proxy_reason == PROXY_BAR_MISSING
    assert r.mark_path_status == mark_path.OBSERVED, "行の状態は残る"
    broken = resolve(inputs(rest=None, mark_bar=bar(open_text="not-a-number")))
    assert broken.proxy_reason == PROXY_OPEN_UNPARSEABLE


def test_layer_class_reports_counts_so_partial_is_never_ambiguous():
    """全件 unavailable の layer も、件数を見れば proxy の有無が分かる。"""
    rows = [resolve(inputs(rest=None, mark_bar=None, ts=TS))]
    table = layer_fidelity(rows)
    entry = table["literature_in_sample"]
    assert entry["class"] == LAYER_FIDELITY_PARTIAL
    assert entry["counts"][FIDELITY_UNAVAILABLE] == 1
    assert entry["counts"][FIDELITY_EXACT_REST] == 0
    assert entry["counts"][FIDELITY_KLINE_PROXY] == 0
    assert entry["shares"][FIDELITY_EXACT_REST] == "0/1"


def test_a_single_proxy_event_downgrades_a_layer_from_exact():
    exact_only = [resolve(inputs(rest="1.0", ts=TS)) for _ in range(5)]
    assert layer_fidelity(exact_only)["literature_in_sample"]["class"] == LAYER_FIDELITY_EXACT
    mixed = [*exact_only, resolve(inputs(rest=None, ts=TS))]
    assert layer_fidelity(mixed)["literature_in_sample"]["class"] == LAYER_FIDELITY_PARTIAL


def test_resolve_all_preserves_order_and_independence():
    batch = [inputs(rest="1.0"), inputs(rest=None), inputs(rest=None, mark_bar=None)]
    out = resolve_all(batch)
    assert [r.primary_fidelity for r in out] == [
        FIDELITY_EXACT_REST, FIDELITY_KLINE_PROXY, FIDELITY_UNAVAILABLE,
    ]
    assert all(resolve(i).to_dict() == o.to_dict() for i, o in zip(batch, out))


# --------------------------------------------------------------------------
# 不変性
# --------------------------------------------------------------------------


def test_frozen_modules_are_untouched():
    freeze = json.loads(
        (REPO / "experiments/phase8/carry_freeze_v1_8_5.json").read_text(encoding="utf-8")
    )
    for key in ("prereg_module", "engine_module", "mark_path_module",
                "signal_module", "splits_module", "prereg_doc"):
        entry = freeze[key]
        digest = hashlib.sha256((REPO / entry["path"]).read_bytes()).hexdigest()
        assert digest == entry["sha256"], entry["path"]


def test_resolver_does_not_import_the_strategy_or_prereg_layer():
    import ast
    import inspect

    from mce import funding_mark_resolver as fmr

    tree = ast.parse(inspect.getsource(fmr))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            # `from mce.backtest import mark_path` は module=mce.backtest / name=mark_path
            imported.update(f"{node.module}.{a.name}" for a in node.names)
            imported.add(node.module)
        elif isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
    for forbidden in ("mce.phase8_prereg", "mce.backtest.two_leg", "mce.backtest.rho",
                      "mce.backtest.engine", "polars"):
        assert forbidden not in imported, forbidden
    # 凍結された状態語彙は **使う**(再定義しない)
    assert "mce.backtest.mark_path" in imported


def test_status_vocabulary_is_not_redefined():
    """§32 の状態を resolver 側で作り直していない。"""
    from mce import funding_mark_resolver as fmr
    from mce import phase8_prereg as prereg

    assert set(fmr._STATUS_TO_BLOCKED_REASON) < set(prereg.MARK_PATH_STATUSES)
    # 許容状態も凍結側と一致していること(resolver で別定義していない)
    for status in prereg.MARK_PATH_ACCEPTABLE:
        assert status not in fmr._STATUS_TO_BLOCKED_REASON


# --------------------------------------------------------------------------
# 変異テストで生き残った箇所を固定する
# --------------------------------------------------------------------------


def test_to_dict_contract_is_pinned():
    """`to_dict()` のキー・型・provenance を固定する。

    ここを固定しないと、Decimal を float へ落としても provenance 列を丸ごと
    落としても、テストが緑のまま通ってしまう。
    """
    r = resolve(inputs(rest=None))
    d = r.to_dict()
    assert set(d) == {
        "ts", "layer", "primary_value", "primary_source", "primary_fidelity",
        "primary_reason", "rest_validity", "proxy_reason",
        "sensitivity_value", "sensitivity_source", "sensitivity_fidelity",
        "sensitivity_reason", "rest_funding_time", "mark_bar_ts",
        "previous_mark_bar_ts", "funding_rate_text", "mark_path_status",
        "previous_mark_path_status", "resolution_permitted",
        "sensitivity_applicable", "proxy_is_exact", "or_rule_used",
        "nearest_candidate_selection", "interpolation", "fidelity_describes",
        "proxy_bar_rule", "trace",
    }
    # 値は**文字列**で出す(float へ落とすと丸めが入る)
    assert isinstance(d["primary_value"], str)
    assert d["primary_value"] == "100.00000000", "原文の桁が保存されていない"
    assert isinstance(d["sensitivity_value"], str)
    # provenance が欠けていない
    assert d["mark_bar_ts"] == BAR_TS.isoformat()
    assert d["previous_mark_bar_ts"] == PREV_TS.isoformat()
    assert d["funding_rate_text"] == "0.00010000"
    assert d["mark_path_status"] == mark_path.OBSERVED
    assert d["previous_mark_path_status"] == mark_path.OBSERVED
    assert d["ts"] == TS.isoformat()
    assert isinstance(d["trace"], list) and d["trace"]


def test_sensitivity_never_falls_back_to_the_open_of_the_previous_bar():
    """sensitivity は**直前バーの `mark_close` だけ**。open へ落ちない。

    落ちる実装にすると §8 が禁じる OR 的規則になる。
    """
    previous = MarkBarInput(
        ts=PREV_TS, mark_path_status=mark_path.OBSERVED,
        mark_open_text="777.00000000", mark_close_text=None,
    )
    r = resolve(inputs(rest=None, previous_mark_bar=previous))
    assert r.sensitivity_value is None, "直前バーの open へ落ちている"
    assert r.sensitivity_reason == SENSITIVITY_BAR_MISSING
    broken = MarkBarInput(ts=PREV_TS, mark_path_status=mark_path.OBSERVED,
                          mark_open_text="777.0", mark_close_text="oops")
    assert resolve(inputs(rest=None, previous_mark_bar=broken)).sensitivity_value is None


def test_unknown_status_is_refused_even_when_rest_is_valid():
    """status の検査を「REST が無効なときだけ」に遅延させない。"""
    with pytest.raises(FundingMarkResolverError, match="凍結されていない"):
        resolve(inputs(rest="50000.0", mark_bar=bar(status="looks_fine")))
    with pytest.raises(FundingMarkResolverError):
        resolve(inputs(rest="50000.0", previous_mark_bar=prev(status="looks_fine")))


def test_sensitivity_applicability_is_explicit():
    """「適用外」と「適用対象だが使えない」を値の None で兼ねさせない。"""
    not_applicable = resolve(inputs(rest="50000.0"))
    assert not_applicable.sensitivity_applicable is False
    assert not_applicable.sensitivity_value is None

    applicable_but_blocked = resolve(
        inputs(rest=None, previous_mark_bar=prev(status=mark_path.ROUTE_UNVERIFIED))
    )
    assert applicable_but_blocked.sensitivity_applicable is True
    assert applicable_but_blocked.sensitivity_value is None
    assert applicable_but_blocked.sensitivity_reason in SENSITIVITY_BLOCKED_REASONS


def test_every_unacceptable_frozen_status_has_a_reason():
    """凍結語彙に許容外 status が増えても `KeyError` にならない。"""
    from mce import funding_mark_resolver as fmr
    from mce import phase8_prereg as prereg

    unacceptable = set(prereg.MARK_PATH_STATUSES) - set(prereg.MARK_PATH_ACCEPTABLE)
    assert unacceptable == set(fmr._STATUS_TO_BLOCKED_REASON), "対応表に漏れがある"
    assert unacceptable == set(fmr._STATUS_TO_SENSITIVITY_REASON)
    # 凍結表は書き換えられない
    with pytest.raises(TypeError):
        fmr._STATUS_TO_BLOCKED_REASON["x"] = "y"


def test_reason_values_all_come_from_the_published_vocabularies():
    """成功時の理由も語彙に入っている(テストが文字列リテラルを持たない)。"""
    from mce.funding_mark_resolver import PROXY_REASONS, SENSITIVITY_REASONS

    cases = [inputs(), inputs(rest=None), inputs(rest=None, mark_bar=None),
             inputs(rest="0"), inputs(rest="bad", rest_funding_time=TS)]
    for case in cases:
        r = resolve(case)
        assert r.proxy_reason in PROXY_REASONS, r.proxy_reason
        assert r.sensitivity_reason in SENSITIVITY_REASONS, r.sensitivity_reason


@pytest.mark.parametrize("bad", [100.5, Decimal("100.5"), 100, b"100.5"])
def test_non_string_prices_are_refused_by_rule_not_by_a_raw_type_error(bad):
    """parquet の Float64 等を渡されたら**分類できるエラー**で止まる。"""
    with pytest.raises(FundingMarkResolverError, match="原文の文字列"):
        resolve(inputs(rest=None, mark_bar=bar(open_text=bad)))
    with pytest.raises(FundingMarkResolverError):
        resolve(inputs(rest=bad, rest_funding_time=TS))

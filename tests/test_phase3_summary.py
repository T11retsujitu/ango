"""Phase 3 cross-arm 集計の単体テストと、凍結 artifact との突き合わせ。"""

import json
from pathlib import Path

import pytest

from mce import phase3_summary as ps

REPO = Path(__file__).resolve().parents[1]
PHASE3 = REPO / "experiments" / "phase3"


def _write_run(
    run_dir: Path,
    method: str,
    records: list[dict],
    counters: dict,
    features_sha: str = "abc",
) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "summary.json").write_text(
        json.dumps(
            {
                "protocol": "phase3_bakeoff_v1",
                "method": method,
                "config": {
                    "seed": 1,
                    "budget": len([r for r in records if r["status"] == "evaluated"]),
                    "primary_cost": "base_taker",
                    "secondary_cost": "maker_low",
                },
                "counters": counters,
                "survivors": [],
                "manifest_sha256": {"features": features_sha},
                "source_commit": "deadbeef",
            }
        ),
        encoding="utf-8",
    )
    (run_dir / "candidates.jsonl").write_text(
        "\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8"
    )


def _metrics(total_return: float, sharpe: float, trades: int) -> dict:
    return {
        "total_return": total_return,
        "sharpe": sharpe,
        "trade_count": trades,
        "turnover_total": float(trades * 2),
        "break_even_cost_bps": 0.0,
        "exposure": 0.5,
        "max_drawdown": 1.0,
        "cancelled_fills": 0,
    }


def _evaluated(i: int, ast: dict, net: float, trades: int) -> dict:
    return {
        "i": i,
        "status": "evaluated",
        "ast_hash": f"h{i}",
        "ast": ast,
        "research": {
            "base_taker": _metrics(net, net, trades),
            "maker_low": _metrics(net + 1.0, net + 1.0, trades),
        },
        "research_pass": False,
        "validation": None,
        "survivor": False,
    }


COUNTERS = {
    "candidate_count": 4,
    "unique_candidate_count": 2,
    "duplicate_count": 1,
    "rejected_candidate_count": 1,
    "runtime_failure_count": 0,
    "evaluated_count": 2,
    "research_pass_count": 0,
    "validation_count": 0,
    "survivor_count": 0,
}


@pytest.fixture
def fake_root(tmp_path: Path) -> Path:
    long_only = {"long_if": {"op": "greater"}, "short_if": None}
    both = {"long_if": {"op": "greater"}, "short_if": {"op": "less"}}
    records = [
        _evaluated(1, long_only, -0.5, 100),
        {"i": 2, "status": "rejected", "reason": "params", "ast": long_only},
        {"i": 3, "status": "duplicate", "ast_hash": "h1"},
        _evaluated(4, both, 1.0, 10),
    ]
    _write_run(tmp_path / "random_seed1", "random", records, COUNTERS)
    return tmp_path


def test_arm_stats_derives_search_quality(fake_root: Path):
    stats = ps.arm_stats(fake_root / "random_seed1")
    q = stats["search_quality"]
    assert q["valid_rate"] == 0.75  # rejected 1 / draw 4
    assert q["duplicate_rate"] == 0.25
    assert q["unique_per_draw"] == 0.5
    assert q["draws_per_evaluation"] == 2.0


def test_arm_stats_distribution_and_sides(fake_root: Path):
    d = ps.arm_stats(fake_root / "random_seed1")["research_distribution"]
    assert d["n_evaluated"] == 2
    assert d["primary_net_median"] == 0.25
    assert d["primary_net_positive"] == 1
    assert d["secondary_net_positive"] == 2  # maker_low は +1.0 されている
    # trade_count >= 30 の候補だけを見る active 系
    assert d["active_candidates"] == 1
    assert d["active_net_max"] == -0.5
    assert d["active_net_positive"] == 0
    assert d["sides"] == {"both": 1, "long": 1}


def test_collect_detects_manifest_mismatch(fake_root: Path):
    _write_run(
        fake_root / "genetic_seed2",
        "genetic",
        [_evaluated(1, {"long_if": None, "short_if": {"op": "less"}}, -1.0, 50)],
        COUNTERS,
        features_sha="other",
    )
    report = ps.collect(fake_root)
    assert report["consistency"]["same_features_manifest"] is False
    assert [a["method"] for a in report["arms"]] == ["random", "genetic"]  # ARM_ORDER 順


def test_llm_transcript_stats_absent_by_default(fake_root: Path):
    assert ps.llm_transcript_stats(fake_root / "random_seed1") is None


def test_markdown_table_has_total_row(fake_root: Path):
    table = ps.markdown_table(ps.collect(fake_root))
    assert "| **total** |" in table


# --- 凍結 artifact に対する回帰(数値が動いたら気づく) ---


@pytest.mark.skipif(not PHASE3.is_dir(), reason="phase3 artifacts が無い")
def test_frozen_phase3_counters():
    report = ps.collect(PHASE3)
    counters = {a["method"]: a["counters"] for a in report["arms"]}
    assert counters["random"]["candidate_count"] == 46
    assert counters["random"]["research_pass_count"] == 4
    assert counters["genetic"]["duplicate_count"] == 64
    assert counters["llm"]["candidate_count"] == 32
    assert counters["llm"]["rejected_candidate_count"] == 2
    # 主指標: 全 arm で validation survivor は 0
    assert all(c["survivor_count"] == 0 for c in counters.values())
    assert report["totals"]["evaluated_count"] == 100
    # 全 arm が同一 features manifest で評価されている(比較可能性の前提)
    assert report["consistency"]["same_features_manifest"] is True


@pytest.mark.skipif(not PHASE3.is_dir(), reason="phase3 artifacts が無い")
def test_frozen_llm_transcript_stats():
    stats = ps.llm_transcript_stats(PHASE3 / "llm_claude-opus-5")
    assert stats["api_calls"] == 6
    assert stats["proposals"] == 36
    assert stats["non_ok_calls"] == 0
    assert stats["models"] == ["claude-opus-5"]
    # 事後スキャン: 提案本文が OHLCV 集約前の実体へ言及した件数
    assert stats["mechanism_keyword_hits"] == 34

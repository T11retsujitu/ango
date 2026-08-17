"""docs の主張と一次 artifact の機械的照合(研究整合性ルール §11)。

findings に手で書いた数値が artifact とズレたら落ちる。survivor 0 という
negative result を「都合よく」書き換えられないことのコード化でもある。
"""

from pathlib import Path

import pytest

from mce import phase3_summary as ps

REPO = Path(__file__).resolve().parents[1]
PHASE3 = REPO / "experiments" / "phase3"
FINDINGS = REPO / "docs" / "findings"
SUMMARY_DOC = FINDINGS / "2026-08-16-phase3-bakeoff-summary-v1.md"
ARMC_DOC = FINDINGS / "2026-08-16-phase3-armC-llm-v1.md"

pytestmark = pytest.mark.skipif(not PHASE3.is_dir(), reason="phase3 artifacts が無い")


def _counter_rows(markdown: str) -> dict[str, list[int]]:
    """`| arm | draw | rejected | duplicate | evaluated | research_pass | survivor |`
    形式(7列・全て整数)の行だけを拾う。"""
    rows: dict[str, list[int]] = {}
    for line in markdown.splitlines():
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) != 7:
            continue
        name = cells[0].strip("*").strip()
        try:
            values = [int(c) for c in cells[1:]]
        except ValueError:
            continue
        rows[name] = values
    return rows


def test_summary_doc_counter_table_matches_artifacts():
    doc_rows = _counter_rows(SUMMARY_DOC.read_text(encoding="utf-8"))
    report = ps.collect(PHASE3)
    keys = (
        "candidate_count",
        "rejected_candidate_count",
        "duplicate_count",
        "evaluated_count",
        "research_pass_count",
        "survivor_count",
    )
    for arm in report["arms"]:
        assert arm["method"] in doc_rows, f"{arm['method']} の行が findings に無い"
        assert doc_rows[arm["method"]] == [arm["counters"][k] for k in keys]
    assert doc_rows["total"] == [report["totals"][k] for k in keys]


def test_docs_do_not_claim_a_survivor():
    report = ps.collect(PHASE3)
    assert report["totals"]["survivor_count"] == 0
    for doc in (SUMMARY_DOC, ARMC_DOC):
        text = doc.read_text(encoding="utf-8")
        assert "0 / 30" in text or "0/30" in text
        assert "Final OOS" in text  # 封印状態への言及を必須にする


def test_findings_index_links_resolve():
    index = FINDINGS / "README.md"
    text = index.read_text(encoding="utf-8")
    for name in (
        "2026-08-16-phase3-bakeoff-summary-v1.md",
        "2026-08-16-phase3-armC-llm-v1.md",
        "../phase7/information_space_expansion_v1.md",
        "../phase7/microstructure_v1_review.md",
        "../research_backlog.md",
    ):
        assert name in text, f"{name} が findings index から参照されていない"
        assert (index.parent / name).exists(), f"{name} が存在しない"


def test_armc_doc_matches_transcript_stats():
    stats = ps.llm_transcript_stats(PHASE3 / "llm_claude-opus-5")
    text = ARMC_DOC.read_text(encoding="utf-8")
    assert f"{stats['proposals']}提案" in text or f"{stats['proposals']} / 32" in text
    assert str(stats["mechanism_keyword_hits"]) in text

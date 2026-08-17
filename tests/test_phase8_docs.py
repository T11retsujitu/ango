"""Phase 8 の文書と機械可読 artifact の照合(研究整合性ルール §11)。

Phase 8.0 の採点は docs/phase8/replication_candidates_v1.json が正であり、
markdown はそこから書き写したものである。ズレたら落とす。

Phase 7 の negative result と「GO 2件は保留であって棄却ではない」という
区別を、後から都合よく書き換えられないことのコード化でもある。
"""

import json
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PHASE8 = REPO / "docs" / "phase8"
FINDINGS = REPO / "docs" / "findings"

CANDIDATES_JSON = PHASE8 / "replication_candidates_v1.json"
CANDIDATES_DOC = PHASE8 / "replication_candidates_v1.md"
SELECTION_DOC = PHASE8 / "phase8_selection_memo_v1.md"
REVIEW_DOC = PHASE8 / "literature_review_2026-08-17.md"
PROTOCOL_DOC = PHASE8 / "carry_replication_protocol_v1.md"
CLOSEOUT_DOC = FINDINGS / "2026-08-17-phase7-tier0-closeout-v1.md"

AXIS_ORDER = (
    "mechanism_clarity",
    "data_availability",
    "cost_execution_realism",
    "independent_confirmation",
    "reimplementability",
    "ango_asset_reuse",
    "solo_feasibility",
    "evidence_quality",
)


NEGATIONS = ("とは言っていない", "のではない", "していない", "ではない", "と書かない")


def _is_negated(tail: str) -> bool:
    """直後の文脈が否定形(「〜とは言っていない」等)かどうか。"""
    return any(marker in tail for marker in NEGATIONS)


def _load() -> dict:
    return json.loads(CANDIDATES_JSON.read_text(encoding="utf-8"))


def _doc_score_rows(markdown: str) -> dict[str, list[int]]:
    """`| 順位 | ID | 名前 | family | 8軸... | 合計 |` の 13 列行を拾う。

    数値セルは `**89**` のような強調を含みうるので数字だけ取り出す。
    """
    rows: dict[str, list[int]] = {}
    for line in markdown.splitlines():
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) != 13:
            continue
        cid = cells[1].strip("*").strip()
        if not re.fullmatch(r"P8-C\d+", cid):
            continue
        numbers = []
        for cell in [cells[0]] + cells[4:]:
            digits = re.sub(r"[^0-9]", "", cell)
            if digits == "":
                break
            numbers.append(int(digits))
        if len(numbers) != 10:  # rank + 8 axes + total
            continue
        rows[cid] = numbers
    return rows


def test_scoring_axes_sum_to_100():
    axes = _load()["scoring_axes"]
    assert set(axes) == set(AXIS_ORDER)
    assert sum(axes.values()) == 100


def test_each_candidate_score_sums_to_its_total():
    for cand in _load()["candidates"]:
        got = sum(cand["scores"][a] for a in AXIS_ORDER)
        assert got == cand["total_score"], f"{cand['id']}: {got} != {cand['total_score']}"


def test_no_axis_score_exceeds_its_cap():
    axes = _load()["scoring_axes"]
    for cand in _load()["candidates"]:
        for axis, cap in axes.items():
            value = cand["scores"][axis]
            assert 0 <= value <= cap, f"{cand['id']}.{axis} = {value} (上限 {cap})"


def test_ranks_are_consistent_with_totals():
    cands = _load()["candidates"]
    ranks = [c["rank"] for c in cands]
    assert ranks == list(range(1, len(cands) + 1)), "rank は 1..N の連番であること"
    totals = [c["total_score"] for c in cands]
    assert totals == sorted(totals, reverse=True), "rank 順は total_score 降順であること"


def test_markdown_table_matches_json():
    doc_rows = _doc_score_rows(CANDIDATES_DOC.read_text(encoding="utf-8"))
    cands = _load()["candidates"]
    assert len(doc_rows) == len(cands), "採点表の行数が JSON の候補数と違う"
    for cand in cands:
        assert cand["id"] in doc_rows, f"{cand['id']} の行が markdown に無い"
        expected = [cand["rank"]] + [cand["scores"][a] for a in AXIS_ORDER] + [cand["total_score"]]
        assert doc_rows[cand["id"]] == expected, f"{cand['id']} の採点が JSON と食い違う"


def test_unverified_source_is_penalised_to_the_floor():
    """一次資料の存在を確認できなかった候補が、証拠品質で満点近くを取っていないこと。

    捏造・未確認の主張が採点を通り抜けないことの機械的な担保。
    """
    for cand in _load()["candidates"]:
        statuses = [p.get("verification_status") for p in cand.get("anchor_papers", [])]
        if "UNVERIFIED" in statuses:
            assert cand["scores"]["evidence_quality"] <= 2, (
                f"{cand['id']}: UNVERIFIED な一次資料に依存しているのに "
                f"evidence_quality={cand['scores']['evidence_quality']}"
            )


def test_reported_returns_are_not_scored():
    rules = _load()["scoring_rules"]
    assert rules["no_points_for_reported_returns"] is True


def test_selected_candidate_is_rank_one_everywhere():
    top = min(_load()["candidates"], key=lambda c: c["rank"])
    assert top["id"] == "P8-C1"
    for doc in (SELECTION_DOC, PROTOCOL_DOC):
        text = doc.read_text(encoding="utf-8")
        assert "P8-C1" in text, f"{doc.name} が選定候補 ID を参照していない"


def test_phase8_docs_do_not_claim_results():
    """まだ計算していないものを計算したと書いていないこと。"""
    for doc in (REVIEW_DOC, CANDIDATES_DOC, SELECTION_DOC, PROTOCOL_DOC):
        text = doc.read_text(encoding="utf-8")
        assert "Final OOS" in text or "final_oos" in text, f"{doc.name} が封印状態に言及していない"
    review = REVIEW_DOC.read_text(encoding="utf-8")
    assert "開封していない" in review


def test_protocol_is_frozen_and_records_its_remaining_blocker():
    """2026-08-17 の決定ログにより v1.8 で凍結された。

    凍結前は「未凍結であること」を検査していた。状態遷移に伴い検査を反転させる
    (テストが状態を追随せずに通り続けることを防ぐため、両方は書かない)。
    """
    text = PROTOCOL_DOC.read_text(encoding="utf-8")
    assert "FROZEN" in text, "凍結状態が明示されていない"
    assert "未凍結" not in text, "凍結後なのに未凍結の記述が残っている"
    # 解決済みの blocker は解決として、未解決の H13 は未解決として書かれていること
    assert "H13" in text, "未解決の blocker H13 が記載されていない"
    assert "commissionRate" in text, "H13 の取得経路が記載されていない"
    # primary endpoint が方向正解率でないこと / delta-neutral が non-goal に書かれていること
    assert "方向正解率" in text
    assert "方向予測をしない" in text
    assert "delta-neutral" in text


def test_protocol_declares_required_sections():
    """事前登録の必須18項目が、見出しの言い回しに依らず本文に存在すること。

    各項目は「これのどれかが出てくれば充足」という別名の集合で判定する
    (改訂で見出しが変わっても、内容が消えたときだけ落ちるようにするため)。
    """
    text = PROTOCOL_DOC.read_text(encoding="utf-8")
    required: dict[str, tuple[str, ...]] = {
        "primary research question": ("Primary Research Question",),
        "non-goals": ("Non-goals",),
        "information set": ("情報集合",),
        "decision time / availability": ("decision time と availability",),
        "candidate horizons": ("候補 horizon",),
        "fixed baselines": ("baseline",),
        "explicit trade structure": ("取引構造",),
        "two-leg cost model": ("コストモデル",),
        "funding cash flows": ("funding 収支",),
        "hedge mismatch": ("hedge mismatch",),
        "turnover and capital": ("turnover と資金拘束",),
        "layer boundaries": ("split と外部知識汚染", "layer 1", "layer 3"),
        "promotion / selection rule": ("昇格規則", "horizon 選択規則"),
        "multiple testing": ("多重比較補正",),
        "effective N / overlap": ("effective N",),
        "primary endpoint": ("Primary endpoint",),
        "go / no-go": ("GO / NO-GO",),
        "negative result closure": ("negative result として閉じる条件",),
        "artifact spec": ("experiment artifact 仕様",),
        "implementation plan": ("実装予定ファイルとテスト",),
    }
    missing = [k for k, aliases in required.items() if not any(a in text for a in aliases)]
    assert not missing, f"事前登録の必須項目が欠けている: {missing}"


def test_protocol_defines_an_entry_rule_and_a_null_hypothesis():
    """凍結前監査が fatal として検出した2つの欠落を、再発しないよう固定する。

    - entry 規則が無いと、固定 horizon の連続グリッドは always_on へ縮退する
    - 帰無仮説が無いと、多重比較補正を要求しても p が作れない
    """
    text = PROTOCOL_DOC.read_text(encoding="utf-8")
    assert "entry" in text and "exit" in text
    assert "帰無" in text, "帰無仮説が定義されていない"
    assert "randomization" in text, "帰無分布の実現方法が定義されていない"
    assert "telescope" in text, "連続グリッドの縮退問題への言及が無い"


def test_protocol_sets_a_sample_floor_and_an_mde_gate():
    """検出力不足を「効果が無かった」と書かないための機械的な担保。"""
    text = PROTOCOL_DOC.read_text(encoding="utf-8")
    assert "MIN_TRADES" in text
    assert "insufficient_sample" in text
    assert "insufficient_power" in text
    assert "MDE" in text


def test_arm_r_trades_on_rho_not_on_raw_basis():
    """Arm R の entry/exit が A2 の ρ で書かれていること(Y45 / Y55)。

    ρ は κ(1−e^{−(f−s)}) − (r−r′) であって単純な相対 basis ではない。
    §4.2 で列を分離しても §6.3 へ伝播し損ねる、という実際に起きたバグを固定する。
    """
    text = PROTOCOL_DOC.read_text(encoding="utf-8")
    assert "rho > arb_bound_upper" in text, "Arm R の entry が ρ 基準で書かれていない"
    # A2 の年率化係数が明記されていること
    assert "1095" in text
    # v1.5 まで使われていた未定義トークンが Arm R の定義行へ戻っていないこと。
    # 改訂表(Y55)は「かつてこう書かれていた」と記録するため出現してよい。
    arm_r_rows = [
        line
        for line in text.splitlines()
        if line.startswith("|") and "Arm R(replication)" in line
    ]
    assert arm_r_rows, "Arm R の定義行が見つからない"
    for row in arm_r_rows:
        assert "theoretical_relation_no_cost" not in row, (
            "Arm R の定義行に未定義トークンが復活している"
        )
        assert "basis_rel" not in row, "Arm R の定義行が basis_rel を使っている"


def test_protocol_records_the_long_spot_only_variant():
    """A2 は2変種を検定しており、Arm R が対応するのはどちらかを明示すること(Y54)。"""
    text = PROTOCOL_DOC.read_text(encoding="utf-8")
    assert "long-spot-only" in text, "対応する A2 の変種が明示されていない"


def test_audit_record_declares_the_harness_race():
    """監査中に監査対象を書き換えた事実を記録していること。

    これを書かないと「63件が誤りだった」という誤読を招く。
    """
    text = (PHASE8 / "carry_protocol_audit_v1.md").read_text(encoding="utf-8")
    assert "偽陽性率 95%" in text or "95%" in text
    assert "凍結する" in text or "commit hash" in text


def test_protocol_does_not_amend_the_seal_without_human_approval():
    text = PROTOCOL_DOC.read_text(encoding="utf-8")
    assert "PHASE8_PROSPECTIVE_START" in text
    assert "splits" in text and "変更していない" in text


def test_closeout_keeps_the_go_pair_on_hold_not_rejected():
    """GO 2件を「棄却」と書き換えていないこと(指示された恒久的な区別)。

    「棄却したのではない」のような否定形は許すが、地の文で棄却したと書くことを禁じる。
    """
    text = CLOSEOUT_DOC.read_text(encoding="utf-8")
    assert "棄却ではなく" in text or "保留" in text
    for match in re.finditer(r"GO\s*2件(?:を|は)棄却", text):
        tail = text[match.end() : match.end() + 24]
        assert _is_negated(tail), "GO 2件を棄却したと地の文で書いている"
    assert "未開封" in text


def test_closeout_does_not_overclaim():
    """過大な主張を書いていないことの機械的な担保。"""
    text = CLOSEOUT_DOC.read_text(encoding="utf-8")
    for forbidden in (
        "microstructure 全体に情報が無い",
        "BTC に alpha が存在しない",
        "Tier 0 に incremental information が無い",
    ):
        # 否定形(「〜とは言っていない」)としての出現のみ許す
        for match in re.finditer(re.escape(forbidden), text):
            tail = text[match.end() : match.end() + 40]
            assert _is_negated(tail) or "」とは" in tail, (
                f"過大主張が地の文で書かれている: {forbidden}"
            )


def test_findings_index_links_to_phase8_documents():
    index = FINDINGS / "README.md"
    text = index.read_text(encoding="utf-8")
    for name in (
        "2026-08-17-phase7-tier0-closeout-v1.md",
        "../phase8/literature_review_2026-08-17.md",
        "../phase8/replication_candidates_v1.md",
        "../phase8/phase8_selection_memo_v1.md",
        "../phase8/carry_replication_protocol_v1.md",
    ):
        assert name in text, f"{name} が findings index から参照されていない"
        assert (index.parent / name).exists(), f"{name} が存在しない"


def test_external_knowledge_contamination_is_recorded():
    data = _load()["external_knowledge_contamination"]
    items = data["knowledge_acquired_during_this_review"]
    ids = {k["id"] for k in items}
    assert {"K1", "K3", "K7"} <= ids
    for item in items:
        assert item["source_status"] in {
            "VERIFIED-FULL",
            "VERIFIED-META",
            "PARTIAL",
            "UNVERIFIED",
            "IN-HOUSE",
        }


def test_in_house_contamination_is_recorded_and_reaches_into_the_seal():
    """ango 自身の過去の測定による汚染を、外部知識と同じ台帳で管理していること。

    自分の repository に既にある測定を見落とすのは、外部文献を読むより危険である
    (数値が精密で、既にコミット済みで、封印域に食い込みうるため)。
    """
    items = _load()["external_knowledge_contamination"]["knowledge_acquired_during_this_review"]
    in_house = [k for k in items if k["source_status"] == "IN-HOUSE"]
    assert in_house, "在庫由来の汚染が1件も記録されていない"
    for item in in_house:
        assert "source" in item, f"{item['id']}: 在庫の出所ファイルが記録されていない"
        assert (REPO / item["source"]).exists(), f"{item['id']}: 出所ファイルが存在しない"
    # 既存の封印を動かしていないこと
    sealed = _load()["external_knowledge_contamination"]["ango_sealed_split"]
    assert "2026-01-01" in sealed
    assert "NOT opened" in sealed


def test_seal_definition_is_untouched_by_phase8():
    """Phase 8 の設計は splits.py を変更していない。"""
    from mce.backtest import splits

    assert splits.FINAL_OOS_START.isoformat().startswith("2026-01-01")
    assert splits.RESEARCH_START.isoformat().startswith("2023-11-19")
    assert splits.VALIDATION_START.isoformat().startswith("2025-07-01")

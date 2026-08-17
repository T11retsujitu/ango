"""findings 台帳の表が artifact から機械生成され、行を落とさないこと。"""

import pytest

from mce import tier0_prereg as P
from mce import tier0_report as R


def _cell(set_id="T0-A", horizon=1, target="Y1", **extra) -> dict:
    base = {
        "set": set_id,
        "horizon_bars": horizon,
        "target": target,
        "status": "tested",
        "n": 300000,
        "n_eff": 300000.0,
        "utc_days": 1400,
        "folds": 14,
        "dr2": 1.2e-4,
        "mde": 3.4e-4,
        "p": 0.4,
        "p_holm": 1.0,
        "bh_significant": False,
        "disposition": "no_go_not_significant_after_holm",
        "placebo_k_stage1": 200,
        "placebo_exceed": 80,
        "placebo_mean": 1.0e-5,
        "placebo_bt": {"p": 0.3},
        "sham_s0": {"dr2": -1e-4, "observed_beats_sham": True},
        "fold_dr2": [1e-4] * 14,
        "dr2_by_year": {"2021": 1e-4, "2022": -1e-5},
        "excluded_months": ["2022-05"],
        "publication_delay": {
            "extra_lag_1_bars": {"dr2": 1e-4},
            "extra_lag_12_bars": {"dr2": -1e-5},
            "gate_passed": True,
        },
    }
    return base | extra


def _report(cells: list[dict]) -> dict:
    return {"family_size": len(cells), "cells": cells, "runtime_sec": 7200.0}


def test_every_family_row_is_rendered_including_untested():
    cells = [_cell() for _ in range(26)]
    cells.append(
        _cell(target="Y3", status="insufficient_sample", p=1.0, dr2=None, mde=None)
    )
    text = R.render(_report(cells))
    # 結果表の行数 = family の数(選別しない)
    table = text.split("### 1.")[1].split("### 2.")[0]
    rows = [line for line in table.splitlines() if line.startswith("| T0-")]
    assert len(rows) == 27
    assert "—" in rows[-1]  # 検定不能でも行は残り、値が空欄になるだけ


def test_summary_counts_match_the_cells():
    cells = [_cell() for _ in range(3)]
    cells[0]["holm_significant"] = True
    cells[1]["status"] = "insufficient_sample"
    cells[2]["placebo_k_stage2"] = 1402
    summary = R.summary(_report(cells))
    assert summary["reported_rows"] == 3
    assert summary["tested"] == 2
    assert summary["insufficient"] == 1
    assert summary["holm_significant"] == 1
    assert summary["stage2_promoted"] == 1
    assert summary["runtime_hours"] == 2.0


def test_render_covers_every_required_section():
    """§18-3 が列挙する報告項目に対応する節が全て出ること。"""
    text = R.render(_report([_cell()]))
    for heading in ("結果", "placebo", "fold 別", "安定性", "公開遅延", "除外した暦月"):
        assert heading in text


def test_delay_table_uses_the_frozen_lag_values():
    text = R.delay_table([_cell()])
    assert f"+{P.PUBLICATION_DELAY_ROBUSTNESS['gate_extra_lag_bars']}バー" in text
    assert f"+{P.PUBLICATION_DELAY_ROBUSTNESS['reported_extra_lag_bars']}バー" in text


def test_missing_values_never_crash_the_renderer():
    bare = {
        "set": "T0-C",
        "horizon_bars": 48,
        "target": "Y2",
        "status": "insufficient_sample",
    }
    text = R.render(_report([bare]))
    assert "T0-C" in text


def test_main_rejects_an_artifact_that_lost_rows(tmp_path, monkeypatch):
    import json

    monkeypatch.setattr(R, "ARTIFACT_DIR", tmp_path)
    path = tmp_path / "tier0_screening_dev_v1.json"
    report = _report([_cell()])
    report["family_size"] = 27  # 行を消した artifact
    path.write_text(json.dumps(report), encoding="utf-8")
    monkeypatch.setattr("sys.argv", ["tier0_report", "--stage", "dev"])
    with pytest.raises(SystemExit, match="family"):
        R.main()

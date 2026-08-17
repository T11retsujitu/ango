"""screening artifact から findings 台帳用の表を機械生成する(事前登録 §18-3)。

    uv run python -m mce.tier0_report --stage dev

報告する数値を手で書き写さないための module。**選別しない**のが要件なので、
`family()` の 27 test を必ず全行出す(検定不能・未実施も含む)。
"""

import argparse
import json
from pathlib import Path

from mce import tier0_prereg as P

ARTIFACT_DIR = Path("experiments") / "phase7"


def _fmt(value, digits: int = 3) -> str:
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, float):
        return f"{value:+.{digits}e}" if abs(value) < 0.1 else f"{value:+.4f}"
    return str(value)


def _key(entry: dict) -> str:
    return f"{entry['set']} h={entry['horizon_bars']:>2} {entry['target']}"


def results_table(cells: list[dict]) -> str:
    head = (
        "| test | n | n_eff | UTC日 | fold | ΔR² | MDE(95%p) | p | p(Holm) | BH | 判定 |\n"
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|:-:|---|\n"
    )
    rows = []
    for c in cells:
        rows.append(
            f"| {_key(c)} | {c.get('n', 0):,} | {_fmt(c.get('n_eff'), 1)} | "
            f"{c.get('utc_days', 0)} | {c.get('folds', 0)} | {_fmt(c.get('dr2'))} | "
            f"{_fmt(c.get('mde'))} | {_fmt(c.get('p'), 4)} | {_fmt(c.get('p_holm'), 4)} | "
            f"{'*' if c.get('bh_significant') else '·'} | {c.get('disposition', '—')} |"
        )
    return head + "\n".join(rows) + "\n"


def placebo_table(cells: list[dict]) -> str:
    head = (
        "| test | K1 | #≥obs | K2(全数) | #≥obs | Bp平均 | Bt p | sham S0 ΔR² | obs>sham |\n"
        "|---|---:|---:|---:|---:|---:|---:|---:|:-:|\n"
    )
    rows = []
    for c in cells:
        bt = c.get("placebo_bt") or {}
        sham = c.get("sham_s0") or {}
        rows.append(
            f"| {_key(c)} | {c.get('placebo_k_stage1', '—')} | {c.get('placebo_exceed', '—')} | "
            f"{c.get('placebo_k_stage2', '—')} | {c.get('placebo_exceed_stage2', '—')} | "
            f"{_fmt(c.get('placebo_mean'))} | {_fmt(bt.get('p'), 4)} | "
            f"{_fmt(sham.get('dr2'))} | {_fmt(sham.get('observed_beats_sham'))} |"
        )
    return head + "\n".join(rows) + "\n"


def stability_table(cells: list[dict]) -> str:
    head = (
        "| test | fold+ 割合 | 正の暦年 | 符号一致 | LOBO全正 | ΔIC符号一致 | "
        "最大寄与日除外後 | 全条件 |\n|---|---:|---:|---:|:-:|:-:|:-:|:-:|\n"
    )
    rows = []
    for c in cells:
        stability = c.get("stability")
        if not stability:
            rows.append(f"| {_key(c)} | — | — | — | — | — | — | — |")
            continue
        values, passed = stability["values"], stability["passed"]
        rows.append(
            f"| {_key(c)} | {values['positive_fold_fraction']:.2f} | "
            f"{values['positive_calendar_years']} | "
            f"{_fmt(values.get('sign_agreement'), 2)} | "
            f"{_fmt(passed['leave_one_block_out_all_positive'])} | "
            f"{_fmt(passed['dic_sign_matches_dr2'])} | "
            f"{_fmt(passed['drop_most_influential_day_still_positive'])} | "
            f"{_fmt(stability['all_passed'])} |"
        )
    return head + "\n".join(rows) + "\n"


def delay_table(cells: list[dict]) -> str:
    gate = P.PUBLICATION_DELAY_ROBUSTNESS["gate_extra_lag_bars"]
    reported = P.PUBLICATION_DELAY_ROBUSTNESS["reported_extra_lag_bars"]
    head = (
        f"| test | ΔR²(観測) | ΔR²(+{gate}バー) | ΔR²(+{reported}バー) | gate |\n"
        "|---|---:|---:|---:|:-:|\n"
    )
    rows = []
    for c in cells:
        delay = c.get("publication_delay") or {}
        one = delay.get(f"extra_lag_{gate}_bars") or {}
        twelve = delay.get(f"extra_lag_{reported}_bars") or {}
        rows.append(
            f"| {_key(c)} | {_fmt(c.get('dr2'))} | {_fmt(one.get('dr2'))} | "
            f"{_fmt(twelve.get('dr2'))} | {_fmt(delay.get('gate_passed'))} |"
        )
    return head + "\n".join(rows) + "\n"


def per_fold_table(cells: list[dict]) -> str:
    lines = ["| test | fold 別 ΔR² | 年別 ΔR² |", "|---|---|---|"]
    for c in cells:
        folds = c.get("fold_dr2") or []
        years = c.get("dr2_by_year") or {}
        fold_text = " ".join(f"{v:+.1e}" for v in folds) if folds else "—"
        year_text = " ".join(f"{k}:{v:+.1e}" for k, v in sorted(years.items())) or "—"
        lines.append(f"| {_key(c)} | {fold_text} | {year_text} |")
    return "\n".join(lines) + "\n"


def excluded_months_table(cells: list[dict]) -> str:
    lines = ["| test | 除外した暦月(被覆 95% 未満) |", "|---|---|"]
    for c in cells:
        months = c.get("excluded_months") or []
        lines.append(f"| {_key(c)} | {', '.join(months) if months else '(なし)'} |")
    return "\n".join(lines) + "\n"


def summary(report: dict) -> dict:
    cells = report["cells"]
    tested = [c for c in cells if c.get("status") == "tested"]
    # 「走らせなかった(dev で非昇格)」と「走らせたがサンプル不足」は別物。混ぜない。
    not_promoted = [c for c in cells if c.get("status") == "not_promoted_from_dev"]
    return {
        "family_size": report["family_size"],
        "reported_rows": len(cells),
        "tested": len(tested),
        "not_promoted_from_dev": len(not_promoted),
        "insufficient": len(cells) - len(tested) - len(not_promoted),
        "holm_significant": sum(1 for c in cells if c.get("holm_significant")),
        "bh_significant": sum(1 for c in cells if c.get("bh_significant")),
        "stage2_promoted": sum(1 for c in cells if c.get("placebo_k_stage2")),
        "positive_dr2": sum(1 for c in tested if (c.get("dr2") or 0) > 0),
        "max_dr2": max((c.get("dr2") or 0) for c in tested) if tested else None,
        "median_mde": sorted(c["mde"] for c in tested)[len(tested) // 2]
        if tested
        else None,
        "dispositions": {
            d: sum(1 for c in cells if c.get("disposition") == d)
            for d in sorted(
                {c.get("disposition") for c in cells if c.get("disposition")}
            )
        },
        "runtime_hours": round(report["runtime_sec"] / 3600, 2),
    }


def render(report: dict) -> str:
    cells = report["cells"]
    parts = [
        f"### 集計\n\n```json\n{json.dumps(summary(report), ensure_ascii=False, indent=2)}\n```\n",
        f"### 1. 全 {report['family_size']} test の結果(省略なし)\n\n{results_table(cells)}",
        f"### 2. placebo 分布と対照\n\n{placebo_table(cells)}",
        f"### 3. fold 別 / 年別 ΔR²\n\n{per_fold_table(cells)}",
        f"### 4. 安定性(§15 / STABILITY)\n\n{stability_table(cells)}",
        f"### 5. 公開遅延耐性(§17-6)\n\n{delay_table(cells)}",
        f"### 6. 除外した暦月(§7)\n\n{excluded_months_table(cells)}",
    ]
    return "\n".join(parts)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Tier 0 screening artifact -> markdown"
    )
    parser.add_argument("--stage", choices=("dev", "confirmation"), default="dev")
    args = parser.parse_args()
    path = ARTIFACT_DIR / f"tier0_screening_{args.stage}_v1.json"
    report = json.loads(path.read_text(encoding="utf-8"))
    if len(report["cells"]) != report["family_size"]:
        raise SystemExit(
            f"artifact の行数 {len(report['cells'])} が family {report['family_size']} と違う"
        )
    print(render(report))


if __name__ == "__main__":
    main()

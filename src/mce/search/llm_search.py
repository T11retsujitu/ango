"""Arm C — LLM Semantic Search(凍結: docs/phase3/bakeoff_protocol.md §10)。

    python -m mce.search.llm_search                      # 公式run(API 呼び出しあり)
    python -m mce.search.llm_search --replay <run_dir>   # 記録から決定的に再評価

LLM は仮説レコード + dsl_plan のみを出力し、plan.py が AST へ翻訳、共通 Evaluator が
評価する。feedback は research primary metrics のみ(validation は firewall)。
"""

import argparse
import json
from datetime import datetime
from pathlib import Path

from mce.search import llm
from mce.search.plan import PlanError, hypothesis_to_ast
from mce.search.runner import PRIMARY_COST, BudgetExhausted, Evaluator, SearchConfig

FROZEN_BUDGET = 30
PROPOSALS_PER_CALL = 6
MAX_CALLS = 12


def _feedback(record: dict | None, hyp: dict, outcome: str) -> dict:
    """LLM へ返す1件(research primary のみ。validation は絶対に含めない)。"""
    m = record["research"][PRIMARY_COST] if record else None
    trades = m["trade_count"] if m else 0
    net_bps = round(m["total_return"] / trades * 1e4, 2) if m and trades else None
    return {
        "hypothesis_id": hyp.get("hypothesis_id", "?"),
        "signal_family": hyp.get("dsl_plan", {}).get("signal_family", "?"),
        "trades": trades,
        "net_bps_per_trade": net_bps,
        "sharpe": round(m["sharpe"], 3) if m and m["sharpe"] is not None else None,
        "turnover": round(m["turnover_total"], 1) if m else 0,
        "outcome": outcome,
    }


def _outcome(record: dict | None) -> str:
    if record is None:
        return "rejected_by_validator"
    m = record["research"][PRIMARY_COST]
    if record["research_pass"]:
        return "passed_research_gate"
    if m["trade_count"] < 30:
        return "below_min_trades"
    return "net_negative"


def run_llm_search(
    cfg: SearchConfig,
    out_dir: Path,
    propose: llm.ProposeFn | None,
    replay: list[dict] | None = None,
    model: str = llm.DEFAULT_MODEL,
) -> dict:
    """propose=None かつ replay 指定なら例外。replay 指定時は API を呼ばない。"""
    ev = Evaluator(cfg, out_dir)
    transcript = llm.Transcript(out_dir / "llm_transcript.jsonl")
    bars_research = ev.research.height
    history: list[dict] = []
    families: set[str] = set()
    seen_plans: set[str] = set()
    calls = 0
    refusals = 0
    plan_errors = 0
    queue: list[dict] = list(replay or [])

    try:
        while ev.budget_left() > 0:
            if not queue:
                if replay is not None:
                    break  # 記録を使い切った
                if propose is None:
                    raise ValueError("propose も replay も与えられていない")
                if calls >= MAX_CALLS:
                    break
                user = llm.build_user_prompt(PROPOSALS_PER_CALL, history, bars_research)
                calls += 1
                entry = {
                    "call": calls,
                    "model": model,
                    "prompt_sha256": llm.prompt_hash(llm.SYSTEM_PROMPT, user),
                    "user_prompt": user,
                }
                try:
                    payload = propose(llm.SYSTEM_PROMPT, user)
                except llm.Refusal as e:
                    refusals += 1
                    transcript.record({**entry, "status": "refusal", "error": str(e)})
                    continue
                except llm.LlmError as e:
                    transcript.record({**entry, "status": "error", "error": str(e)})
                    break
                transcript.record({**entry, "status": "ok", "response": payload})
                queue = list(payload.get("hypotheses", []))
                if not queue:
                    break

            hyp = queue.pop(0)
            families.add(hyp.get("dsl_plan", {}).get("signal_family", "?"))
            plan_key = json.dumps(hyp.get("dsl_plan", {}), sort_keys=True)
            if plan_key in seen_plans:
                history.append(_feedback(None, hyp, "duplicate_plan"))
                continue
            seen_plans.add(plan_key)
            try:
                ast = hypothesis_to_ast(hyp)
            except (PlanError, ValueError) as e:
                plan_errors += 1
                ev.counters.candidate_count += 1
                ev.counters.rejected_candidate_count += 1
                ev.ledger.record({"i": ev.counters.candidate_count, "status": "rejected",
                                  "reason": f"plan: {e}", "hypothesis": hyp})
                history.append(_feedback(None, hyp, "rejected_by_validator"))
                continue
            record = ev.evaluate(ast)
            history.append(_feedback(record, hyp, _outcome(record)))
    except BudgetExhausted:
        pass

    return ev.summary(
        extra={
            "arm": "llm",
            "model": model,
            "mode": "replay" if replay is not None else "live",
            "deterministic": False,
            "replayable": True,
            "masking": llm.MASKING,
            "api_calls": calls,
            "refusals": refusals,
            "plan_errors": plan_errors,
            "proposals_per_call": PROPOSALS_PER_CALL,
            "semantic_families": sorted(families),
            "feedback_scope": "research_primary_only",
        }
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Arm C: LLM semantic search(凍結プロトコル)")
    parser.add_argument("--budget", type=int, default=FROZEN_BUDGET)
    parser.add_argument("--model", default=llm.DEFAULT_MODEL)
    parser.add_argument("--replay", type=Path, default=None, help="記録済み run ディレクトリから再評価")
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()

    if args.replay is not None:
        hypotheses = llm.replay_hypotheses(args.replay / "llm_transcript.jsonl")
        if not hypotheses:
            raise SystemExit(f"{args.replay} に記録された仮説がない")
        out_dir = args.out or (args.replay.parent / f"{args.replay.name}_replay")
        cfg = SearchConfig(method="llm", seed=0, budget=args.budget)
        report = run_llm_search(cfg, out_dir, propose=None, replay=hypotheses, model=args.model)
    else:
        out_dir = args.out or Path("experiments") / "phase3" / f"llm_{args.model}"
        client = llm.AnthropicClient(model=args.model)
        cfg = SearchConfig(method="llm", seed=0, budget=args.budget)
        report = run_llm_search(cfg, out_dir, propose=client.propose, model=args.model)

    record = {"created_at": datetime.now().astimezone().isoformat(), **report}
    summary_path = out_dir / "summary.json"
    summary_path.write_text(json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    meta = report["arm_meta"]
    print(f"method=llm model={meta['model']} mode={meta['mode']} budget={args.budget} "
          f"api_calls={meta['api_calls']} refusals={meta['refusals']}")
    print("counters:", json.dumps(report["counters"], ensure_ascii=False))
    print("semantic families:", ", ".join(meta["semantic_families"]) or "(none)")
    if report["survivors"]:
        for s in report["survivors"]:
            print(f"  survivor {s['ast_hash'][:12]}: research net {s['research']['total_return']:+.4f} "
                  f"/ validation net {s['validation']['total_return']:+.4f} (trades {s['validation']['trade_count']})")
    else:
        print("  survivors: なし")
    print(f"summary -> {summary_path}")


if __name__ == "__main__":
    main()

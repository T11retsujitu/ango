"""Arm B — Genetic Search(凍結: docs/phase3/bakeoff_protocol.md §8)。

    python -m mce.search.genetic

AST-level の mutation / crossover による steady-state GA。
- fitness は research primary metrics のみから計算(validation は記録専用・防火壁)
- 無効・重複の子は budget 非消費(共通 Evaluator が会計)
- 凍結値: seed 20260819 / budget 30 / population 6 / tournament 3 /
  crossover 0.4 / fitness = sharpe − 0.02×ノード数 − (2.0 if trades<30)
"""

import argparse
import copy
import json
import random
from datetime import datetime
from pathlib import Path

from mce.dsl.nodes import ROOT_CONDITION_KEYS, iter_nodes
from mce.search import grammar
from mce.search.runner import PRIMARY_COST, BudgetExhausted, Evaluator, SearchConfig

FROZEN_SEED = 20260819
FROZEN_BUDGET = 30
POPULATION = 6
TOURNAMENT = 3
P_CROSSOVER = 0.4
COMPLEXITY_PENALTY = 0.02
MIN_TRADES_PENALTY = 2.0
MAX_CHILD_ATTEMPTS = 20


def node_count(strategy: dict) -> int:
    n = 0
    for key in ROOT_CONDITION_KEYS:
        cond = strategy.get(key)
        if isinstance(cond, dict):
            n += sum(1 for _ in iter_nodes(cond))
    return n


def fitness(record: dict) -> float:
    m = record["research"][PRIMARY_COST]
    f = (m["sharpe"] if m["sharpe"] is not None else -99.0) - COMPLEXITY_PENALTY * node_count(record["ast"])
    if m["trade_count"] < 30:
        f -= MIN_TRADES_PENALTY
    return f


# ---- 遺伝操作(全て凍結 grammar のメニュー内で閉じる)----

def _threshold_kind(cmp_node: dict) -> str:
    x = cmp_node["x"]
    if x["op"] == "zscore":
        return "zscore"
    if x["op"] == "rolling_mean":
        return x["x"]["op"]
    return x["op"]


def _collect_sites(strategy: dict) -> dict:
    sites: dict = {"window": [], "threshold": [], "bars": [], "cmp": [], "bool_slot": []}
    for key in ROOT_CONDITION_KEYS:
        cond = strategy.get(key)
        if not isinstance(cond, dict):
            continue
        sites["bool_slot"].append((strategy, key))
        for node in iter_nodes(cond):
            if "window" in node:
                sites["window"].append(node)
            if node.get("op") in ("greater", "less"):
                sites["cmp"].append(node)
                sites["threshold"].append(node)
            if node.get("op") == "holds_for":
                sites["bars"].append(node)
            for ck in ("a", "b"):
                if isinstance(node.get(ck), dict) and node[ck].get("op") not in (None,):
                    # bool 子スロット(and/or/not/holds_for の子は必ず bool)
                    if node.get("op") in ("and", "or", "not", "holds_for"):
                        sites["bool_slot"].append((node, ck))
    return sites


def _neighbor(menu: tuple, value, rng: random.Random):
    if value not in menu:
        return rng.choice(menu)
    i = menu.index(value)
    j = min(len(menu) - 1, max(0, i + rng.choice((-1, 1))))
    return menu[j]


def mutate(strategy: dict, rng: random.Random) -> dict:
    """1箇所だけ変異させた deep copy を返す(元は変更しない)。"""
    s = copy.deepcopy(strategy)
    sites = _collect_sites(s)
    r = rng.random()
    if r < 0.5:  # param 摂動
        kind = rng.choice([k for k in ("window", "threshold", "bars") if sites[k]] or ["window"])
        if kind == "window" and sites["window"]:
            node = rng.choice(sites["window"])
            node["window"] = _neighbor(grammar.WINDOW_MENU, node["window"], rng)
        elif kind == "threshold" and sites["threshold"]:
            node = rng.choice(sites["threshold"])
            menu, signed = grammar.THRESHOLD_MENUS[_threshold_kind(node)]
            sign = -1 if node["threshold"] < 0 else 1
            mag = _neighbor(menu, abs(node["threshold"]), rng)
            if signed and rng.random() < 0.2:
                sign = -sign
            node["threshold"] = sign * mag
        elif sites["bars"]:
            node = rng.choice(sites["bars"])
            node["bars"] = _neighbor(grammar.HOLDS_MENU, node["bars"], rng)
    elif r < 0.65:  # 比較演算子反転
        if sites["cmp"]:
            node = rng.choice(sites["cmp"])
            node["op"] = "less" if node["op"] == "greater" else "greater"
    elif r < 0.9:  # bool 部分木の再サンプル
        parent, key = rng.choice(sites["bool_slot"])
        parent[key] = grammar.sample_bool(rng, 3)
    else:  # max_holding 付替え
        if s.get("max_holding_bars") is None:
            s["max_holding_bars"] = rng.choice(grammar.HOLDING_MENU)
        elif rng.random() < 0.5:
            s["max_holding_bars"] = None
        else:
            s["max_holding_bars"] = _neighbor(grammar.HOLDING_MENU, s["max_holding_bars"], rng)
    return s


def crossover(a: dict, b: dict, rng: random.Random) -> dict:
    """親Aの条件スロット1つを親Bの条件部分木で置き換える(条件移植)。"""
    child = copy.deepcopy(a)
    slots_a = [k for k in ROOT_CONDITION_KEYS if isinstance(a.get(k), dict)] or ["long_if"]
    slots_b = [k for k in ROOT_CONDITION_KEYS if isinstance(b.get(k), dict)]
    if not slots_b:
        return child
    child[rng.choice(slots_a)] = copy.deepcopy(b[rng.choice(slots_b)])
    return child


# ---- GA 本体 ----

def _tournament(pop: list[tuple[float, dict]], rng: random.Random) -> dict:
    best = max(rng.sample(pop, min(TOURNAMENT, len(pop))), key=lambda x: x[0])
    return best[1]


def run_genetic(cfg: SearchConfig, out_dir: Path) -> dict:
    ev = Evaluator(cfg, out_dir)
    rng = random.Random(cfg.seed)
    pop: list[tuple[float, dict]] = []  # (fitness, record)
    generations = 0

    def add(record: dict | None):
        if record is None:
            return
        h = record["ast_hash"]
        if any(r["ast_hash"] == h for _, r in pop):
            return
        pop.append((fitness(record), record))
        pop.sort(key=lambda x: -x[0])
        del pop[POPULATION:]

    try:
        while len(pop) < POPULATION:  # 初期集団
            add(ev.evaluate(grammar.sample_strategy(rng)))
        while ev.budget_left() > 0:  # steady-state 世代ループ
            generations += 1
            record = None
            for _ in range(MAX_CHILD_ATTEMPTS):
                if rng.random() < P_CROSSOVER and len(pop) >= 2:
                    child = crossover(_tournament(pop, rng)["ast"], _tournament(pop, rng)["ast"], rng)
                else:
                    child = mutate(_tournament(pop, rng)["ast"], rng)
                record = ev.evaluate(child)
                if record is not None:
                    break
            if record is None:  # 有効な子が得られない → 新規サンプルで多様性注入
                record = ev.evaluate(grammar.sample_strategy(rng))
            add(record)
    except BudgetExhausted:
        pass

    if ev.counters.evaluated_count < cfg.budget:
        raise RuntimeError(f"budget 未消化(evaluated={ev.counters.evaluated_count})")
    return ev.summary(
        extra={
            "arm": "genetic",
            "population": POPULATION,
            "tournament": TOURNAMENT,
            "p_crossover": P_CROSSOVER,
            "fitness": f"sharpe - {COMPLEXITY_PENALTY}*nodes - ({MIN_TRADES_PENALTY} if trades<30)",
            "generations": generations,
            "final_population": [
                {"fitness": round(f, 4), "ast_hash": r["ast_hash"]} for f, r in pop
            ],
        }
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Arm B: genetic search(凍結プロトコル)")
    parser.add_argument("--budget", type=int, default=FROZEN_BUDGET)
    parser.add_argument("--seed", type=int, default=FROZEN_SEED)
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()

    if args.seed != FROZEN_SEED or args.budget != FROZEN_BUDGET:
        print(f"警告: 凍結値(seed={FROZEN_SEED}, budget={FROZEN_BUDGET})以外での実行。公式runとしては無効")

    out_dir = args.out or Path("experiments") / "phase3" / f"genetic_seed{args.seed}"
    cfg = SearchConfig(method="genetic", seed=args.seed, budget=args.budget)
    report = run_genetic(cfg, out_dir)

    record = {"created_at": datetime.now().astimezone().isoformat(), **report}
    summary_path = out_dir / "summary.json"
    summary_path.write_text(json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    print(f"method=genetic seed={args.seed} budget={args.budget} generations={report['arm_meta']['generations']}")
    print("counters:", json.dumps(report["counters"], ensure_ascii=False))
    if report["survivors"]:
        for s in report["survivors"]:
            print(
                f"  survivor {s['ast_hash'][:12]}: research net {s['research']['total_return']:+.4f} "
                f"/ validation net {s['validation']['total_return']:+.4f} (trades {s['validation']['trade_count']})"
            )
    else:
        print("  survivors: なし")
    print(f"summary -> {summary_path}")


if __name__ == "__main__":
    main()

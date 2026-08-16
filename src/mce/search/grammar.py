"""Random / Genetic 共用の sampling grammar(凍結: docs/phase3/bakeoff_protocol.md §4)。

DSL 仕様 v1(凍結)の AST を、制御付き一様サンプリングで生成する。
乱数は必ず呼び出し側が渡す random.Random(seed) を使う(決定的)。
制約(depth / features / params)違反のサンプルはここでは弾かず、
runner 側の validator が拒否して rejected として記録する。
"""

import random

FEATURE_OPS = ("return", "trend", "volatility", "range", "volume_z", "ma_slope")
WINDOW_MENU = (3, 6, 12, 24, 48, 96, 144, 288)
HOLDS_MENU = (2, 3, 6, 12)
HOLDING_MENU = (6, 12, 24, 48)

# feature/transform 種別 → (閾値メニュー, 符号を乱択するか)
THRESHOLD_MENUS: dict[str, tuple[tuple[float, ...], bool]] = {
    "return": ((0.0005, 0.001, 0.002, 0.005), True),
    "trend": ((0.0005, 0.001, 0.002, 0.005), True),
    "ma_slope": ((0.0001, 0.0002, 0.0005, 0.001), True),
    "volatility": ((0.0005, 0.001, 0.002, 0.005), False),
    "range": ((0.002, 0.005, 0.01, 0.02), False),
    "volume_z": ((0.5, 1.0, 2.0, 3.0), True),
    "zscore": ((0.5, 1.0, 2.0), True),
}


def _sample_comparison(rng: random.Random, depth_left: int) -> dict:
    feat = rng.choice(FEATURE_OPS)
    node: dict = {"op": feat, "window": rng.choice(WINDOW_MENU)}
    kind = feat
    if depth_left >= 4 and rng.random() < 0.2:
        transform = rng.choice(("zscore", "rolling_mean"))
        node = {"op": transform, "x": node, "window": rng.choice(WINDOW_MENU)}
        if transform == "zscore":
            kind = "zscore"
    menu, signed = THRESHOLD_MENUS[kind]
    thr = rng.choice(menu)
    if signed:
        thr *= rng.choice((1, -1))
    return {"op": rng.choice(("greater", "less")), "x": node, "threshold": thr}


def _sample_clock(rng: random.Random) -> dict:
    period = rng.choice((15, 60))
    return {"op": "clock_is", "period": period, "phase": rng.choice(range(0, period, 5))}


def sample_bool(rng: random.Random, depth_left: int) -> dict:
    if depth_left <= 2:
        return _sample_comparison(rng, depth_left) if rng.random() < 0.85 else _sample_clock(rng)
    r = rng.random()
    if r < 0.45:
        return _sample_comparison(rng, depth_left)
    if r < 0.55:
        return _sample_clock(rng)
    if r < 0.75:
        return {
            "op": rng.choice(("and", "or")),
            "a": sample_bool(rng, depth_left - 1),
            "b": sample_bool(rng, depth_left - 1),
        }
    if r < 0.85:
        return {"op": "not", "a": sample_bool(rng, depth_left - 1)}
    return {"op": "holds_for", "a": sample_bool(rng, depth_left - 1), "bars": rng.choice(HOLDS_MENU)}


def sample_strategy(rng: random.Random) -> dict:
    strat: dict = {"type": "strategy", "long_if": None, "short_if": None}
    form = rng.random()
    if form < 0.25:
        strat["long_if"] = sample_bool(rng, 5)
    elif form < 0.5:
        strat["short_if"] = sample_bool(rng, 5)
    else:
        strat["long_if"] = sample_bool(rng, 5)
        strat["short_if"] = sample_bool(rng, 5)
    if rng.random() < 0.25:
        strat["abstain_unless"] = sample_bool(rng, 3)
    if rng.random() < 0.5:
        strat["max_holding_bars"] = rng.choice(HOLDING_MENU)
    return strat


def strategy_stream(seed: int):
    """seed から決定的に無限サンプルを生成する(runner が budget まで消費する)。"""
    rng = random.Random(seed)
    while True:
        yield sample_strategy(rng)

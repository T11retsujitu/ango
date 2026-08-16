"""Phase 7 Tier 0 incremental information test の実行(事前登録 v1.1 準拠)。

    python -m mce.tier0_screening --stage dev
    python -m mce.tier0_screening --stage confirmation   # dev artifact が無いと実行不可

設計は `docs/phase7/tier0_screening_preregistration_v1.md` と、その機械可読版
`mce.tier0_prereg` が正。**本 module は閾値・列・窓を自前で持たない。**

骨格:

```text
A(27列) と X(集合ごと 3-5列)を因果変換で構築
  -> cell = (information set, horizon, target) ごとに complete-case 行を確定
  -> expanding walk-forward(purge / embargo / block 末尾 purge)
  -> ridge(標準化 -> ±10 クリップ -> 内側 purged CV で alpha 選択)
  -> pooled OOS R2 の差 dR2 = R2(B) - R2(A)
  -> A-projection placebo Bp で帰無分布 -> p, MDE
  -> 安定性・機序・コスト換算 -> artifact
```
"""

import argparse
import hashlib
import json
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import polars as pl

from mce import config, experiments, tier0_prereg as P
from mce.binance_vision import DEFAULT_SYMBOL

UTC = timezone.utc
BAR_MINUTES = 5
BAR_MS = BAR_MINUTES * 60 * 1000
BARS_PER_DAY = 288
ARTIFACT_DIR = Path("experiments") / "phase7"
CLIP = P.ESTIMATOR["post_standardization_clip"]
ALPHAS = np.array(P.ESTIMATOR["alpha_grid"], dtype=float)


# --------------------------------------------------------------------------------------
# 特徴量の構築(事前登録 §3–§4。因果変換のみ)
# --------------------------------------------------------------------------------------


def _z20d(column: str, name: str) -> pl.Expr:
    mean = pl.col(column).rolling_mean_by("ts", window_size=P.ZSCORE_WINDOW, closed="left")
    std = pl.col(column).rolling_std_by("ts", window_size=P.ZSCORE_WINDOW, closed="left")
    count = (
        pl.col(column)
        .is_not_null()
        .cast(pl.Int32)
        .rolling_sum_by("ts", window_size=P.ZSCORE_WINDOW, closed="left")
    )
    return (
        pl.when((count >= P.ZSCORE_MIN_VALID_BARS) & (std > 0))
        .then((pl.col(column) - mean) / std)
        .alias(name)
    )


def _rv(bars: int, name: str) -> pl.Expr:
    """窓 [t-bars, t) の5分対数リターン二乗和の平方根(現在バーを含まない・完全窓のみ)。

    行シフトではなく ts ベースの窓なので、欠損バーを跨いだ集計にならない。
    """
    window = f"{BAR_MINUTES * bars}m"
    total = pl.col("_u").pow(2).rolling_sum_by("ts", window_size=window, closed="left")
    count = (
        pl.col("_u")
        .is_not_null()
        .cast(pl.Int32)
        .rolling_sum_by("ts", window_size=window, closed="left")
    )
    return pl.when(count == bars).then(total.sqrt()).alias(name)


def _lagged(df: pl.DataFrame, column: str, bars: int, name: str) -> pl.DataFrame:
    """ts 完全一致 join による過去値(行シフトではない)。"""
    return df.select(
        (pl.col("ts") + pl.duration(minutes=BAR_MINUTES * bars)).alias("ts"),
        pl.col(column).alias(name),
    )


def build_design(features: pl.DataFrame) -> pl.DataFrame:
    """A の 27 列・X の全列・sham S0 を作る(事前登録 §3/§4/§12.4)。"""
    df = features.sort("ts")
    df = df.with_columns((pl.col("close").log() - pl.col("close").log().shift(1)).alias("_u"))
    df = df.with_columns(_rv(12, "rv_12"), _rv(48, "rv_48"))
    df = df.with_columns(
        ((pl.col("high") - pl.col("low")) / pl.col("close")).alias("_hl"),
        (pl.col("volume") + 1).log().alias("_lv"),
        pl.col("close").log().alias("_lc"),
        (2 * pl.col("taker_buy_ratio") - 1).alias("signed_imb"),
        pl.col("trade_count").cast(pl.Float64).log().alias("_ltc"),
        pl.col("avg_trade_size").log().alias("_lats"),
        pl.col("open_interest").log().alias("_loi"),
        pl.col("top_trader_position_ls_ratio").log().alias("_lttp"),
        pl.col("global_account_ls_ratio").log().alias("_lgls"),
        pl.col("taker_ls_vol_ratio").log().alias("_ltls"),
    )
    # 1時間ラグ(ts 完全一致)
    df = df.join(_lagged(df, "_loi", 12, "_loi_lag"), on="ts", how="left").join(
        _lagged(df, "premium_close", 12, "_prem_lag"), on="ts", how="left"
    )
    df = df.with_columns(
        (pl.col("_loi") - pl.col("_loi_lag")).alias("dlog_oi_12"),
        (pl.col("premium_close") - pl.col("_prem_lag")).alias("dprem_12"),
    )
    df = df.with_columns(
        _z20d("_hl", "hl_range_z20d"),
        _z20d("_lv", "log_volume_z20d"),
        _z20d("_lc", "z20d_log_close"),
        _z20d("return_1h", "z20d_return_1h"),
        _z20d("signed_imb", "z20d_signed_imb"),
        _z20d("_ltc", "z20d_log_trade_count"),
        _z20d("_lats", "z20d_log_avg_trade_size"),
        _z20d("_loi", "z20d_log_open_interest"),
        _z20d("dlog_oi_12", "z20d_dlog_oi_12"),
        _z20d("_lttp", "z20d_log_top_trader_position_ls_ratio"),
        _z20d("_lgls", "z20d_log_global_account_ls_ratio"),
        _z20d("_ltls", "z20d_log_taker_ls_vol_ratio"),
        _z20d("premium_close", "z20d_premium_close"),
    )
    df = df.with_columns(
        pl.when(pl.col("rv_12") > 0)
        .then((pl.col("return_5m") / pl.col("rv_12")).clip(-10, 10))
        .alias("norm_move_1"),
        (pl.col("hour_utc") * 60 + pl.col("minute_mod_60")).cast(pl.Float64).alias("_tod"),
        pl.col("weekday_utc").cast(pl.Float64).alias("_dow"),
    )
    harmonics = []
    for k in (1, 2, 3):
        harmonics += [
            (2 * np.pi * k * pl.col("_tod") / 1440).sin().alias(f"tod_sin_{k}"),
            (2 * np.pi * k * pl.col("_tod") / 1440).cos().alias(f"tod_cos_{k}"),
        ]
    for k in (1, 2):
        harmonics += [
            (2 * np.pi * k * pl.col("_dow") / 7).sin().alias(f"dow_sin_{k}"),
            (2 * np.pi * k * pl.col("_dow") / 7).cos().alias(f"dow_cos_{k}"),
        ]
    df = df.with_columns(harmonics)
    df = df.with_columns(
        pl.col("norm_move_1").pow(2).alias("norm_move_1_sq"),
        pl.col("z20d_return_1h").pow(2).alias("z20d_return_1h_sq"),
        (pl.col("weekday_utc") >= 5).cast(pl.Float64).alias("is_weekend"),
        (pl.col("minute_mod_15") == 0).cast(pl.Float64).alias("is_quarter_hour"),
        (pl.col("minute_mod_60") == 0).cast(pl.Float64).alias("is_hour_boundary"),
    )
    # X の交互作用(事前登録 §4)
    df = df.with_columns(
        (pl.col("signed_imb") * pl.col("norm_move_1")).alias("signed_imb_x_norm_move_1"),
        (pl.col("dlog_oi_12") * pl.col("z20d_return_1h")).alias("dlog_oi_12_x_z20d_return_1h"),
    )
    # sham S0(§12.4): OHLCV 由来・A に含まれない・因果
    df = df.join(_lagged(df, "norm_move_1", 1, "s03"), on="ts", how="left").join(
        _lagged(df, "norm_move_1", 2, "s04"), on="ts", how="left"
    )
    df = df.with_columns(
        pl.when(pl.col("rv_48") > 0)
        .then((pl.col("rv_12") / pl.col("rv_48") - 1).clip(-10, 10))
        .alias("s01"),
        pl.col("_hl")
        .rolling_mean_by("ts", window_size=f"{BAR_MINUTES * 12}m", closed="left")
        .alias("_hl12"),
    )
    df = df.with_columns(_z20d("_hl12", "s02"))
    df = df.with_columns(
        pl.Series("s05", _causal_percentile_rank(df["volume_ratio_20"].to_numpy()) - 0.5)
    )
    return df


def _causal_percentile_rank(values: np.ndarray, window: int = 5760) -> np.ndarray:
    """直近 window 本(現在バーを含まない)の中での順位割合。未来を見ない。

    有効本数が Z20d と同じ床(5,184)未満なら NaN。chunk 化した総当たり比較で厳密に計算する。
    """
    n = len(values)
    out = np.full(n, np.nan)
    chunk = 4096
    for start in range(window, n, chunk):
        stop = min(start + chunk, n)
        rows = np.arange(start, stop)
        # 各行 t について window 本の過去窓 [t-window, t)
        offsets = np.arange(-window, 0)
        idx = rows[:, None] + offsets[None, :]
        past = values[idx]
        current = values[rows][:, None]
        valid = np.isfinite(past)
        counts = valid.sum(axis=1)
        below = ((past < current) & valid).sum(axis=1)
        rank = np.where(counts >= P.ZSCORE_MIN_VALID_BARS, below / np.maximum(counts, 1), np.nan)
        out[rows] = np.where(np.isfinite(current[:, 0]), rank, np.nan)
    return out


SHAM_COLUMNS = ("s01", "s02", "s03", "s04", "s05")


# --------------------------------------------------------------------------------------
# fold(事前登録 §9)
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Fold:
    train_start: datetime
    train_end: datetime  # 排他(purge 前)
    test_start: datetime
    test_end: datetime


def _add_months(moment: datetime, months: int) -> datetime:
    year = moment.year + (moment.month - 1 + months) // 12
    month = (moment.month - 1 + months) % 12 + 1
    return moment.replace(year=year, month=month)


def make_folds(start: datetime, end: datetime) -> list[Fold]:
    """expanding・初期学習 6ヶ月・テストブロック 3ヶ月(§9.2)。"""
    init = P.FOLD["initial_train_months"]
    block = P.FOLD["test_block_months"]
    folds: list[Fold] = []
    test_start = _add_months(start, init)
    while test_start < end:
        test_end = min(_add_months(test_start, block), end)
        folds.append(Fold(start, test_start, test_start, test_end))
        test_start = test_end
    return folds


# --------------------------------------------------------------------------------------
# ridge(事前登録 §10)
# --------------------------------------------------------------------------------------


def _standardize(train: np.ndarray, other: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    mean = train.mean(axis=0)
    std = train.std(axis=0)
    if np.any(std <= 0):
        raise QualityFailure("学習集合に std == 0 の列がある")
    return (
        np.clip((train - mean) / std, -CLIP, CLIP),
        np.clip((other - mean) / std, -CLIP, CLIP),
    )


class QualityFailure(RuntimeError):
    """事前登録が「黙って続行してはならない」と定めた状態。"""


def _ridge_path(gram: np.ndarray, xty: np.ndarray) -> np.ndarray:
    """alpha グリッド全点の係数(同じ Gram から解く)。戻り値 shape (n_alpha, p)。"""
    eye = np.eye(gram.shape[0])
    return np.stack([np.linalg.solve(gram + a * eye, xty) for a in ALPHAS])


def fit_nested_pair(
    z_train: np.ndarray,
    y_train: np.ndarray,
    z_test: np.ndarray,
    p_a: int,
    purge: int,
) -> tuple[np.ndarray, np.ndarray, float, tuple[float, float]]:
    """入れ子の A(先頭 p_a 列)と B(全列)を、**同じ Gram** から同時に解く。

    A の標準化列は B の中の同じ列と一致するので、`[A | X]` を一度だけ標準化し、
    Gram を1回だけ作れば A 用は左上ブロックを切り出すだけで済む。さらに内側 CV の
    prefix は入れ子なので、Gram を増分で積み上げれば学習行を1回走査するだけで済む。
    数学的には素朴実装と同一で、計算量だけが減る。

    戻り値: (A の test 予測, B の test 予測, 学習平均 y, (alpha_A, alpha_B))
    """
    n = len(z_train)
    y_mean = float(y_train.mean())
    yc = y_train - y_mean

    bounds = np.linspace(0, n, 5).astype(int)
    inner = [(max(bounds[i] - purge, 1), bounds[i], bounds[i + 1]) for i in range(1, 4)]
    inner = [(e, v0, v1) for e, v0, v1 in inner if e > 50 and v1 - v0 > 50]

    p = z_train.shape[1]
    gram = np.zeros((p, p))
    rhs = np.zeros(p)
    prefix: list[tuple[np.ndarray, np.ndarray]] = []
    cursor = 0
    for edge, _, _ in inner:
        if edge > cursor:
            block = z_train[cursor:edge]
            gram += block.T @ block
            rhs += block.T @ yc[cursor:edge]
            cursor = edge
        prefix.append((gram.copy(), rhs.copy()))
    if n > cursor:
        block = z_train[cursor:n]
        gram += block.T @ block
        rhs += block.T @ yc[cursor:n]

    def solve(width: int) -> tuple[np.ndarray, float]:
        sse = np.zeros(len(ALPHAS))
        for (edge, v0, v1), (g, r) in zip(inner, prefix):
            betas = _ridge_path(g[:width, :width], r[:width])
            zv = z_train[v0:v1, :width]
            residual = yc[v0:v1][None, :] - betas @ zv.T
            sse += (residual**2).sum(axis=1)
        alpha = float(ALPHAS[int(np.argmin(sse))]) if inner else float(ALPHAS[len(ALPHAS) // 2])
        beta = np.linalg.solve(
            gram[:width, :width] + alpha * np.eye(width), rhs[:width]
        )
        return beta, alpha

    beta_a, alpha_a = solve(p_a)
    beta_b, alpha_b = solve(p)
    return (
        y_mean + z_test[:, :p_a] @ beta_a,
        y_mean + z_test @ beta_b,
        y_mean,
        (alpha_a, alpha_b),
    )


# --------------------------------------------------------------------------------------
# cell の評価
# --------------------------------------------------------------------------------------


def _r2(y: np.ndarray, pred: np.ndarray, train_mean: np.ndarray) -> float:
    sse = float(((y - pred) ** 2).sum())
    sst = float(((y - train_mean) ** 2).sum())
    return 1.0 - sse / sst if sst > 0 else float("nan")


@dataclass
class CellData:
    """1 cell(set, horizon, target)の密グリッド表現。"""

    grid_ts: np.ndarray  # epoch ms(窓内の全 5分スロット)
    a: np.ndarray  # (n_slots, p_a)
    x: np.ndarray  # (n_slots, p_x)
    y: np.ndarray  # (n_slots,)
    valid: np.ndarray  # bool
    fold_train: list[np.ndarray]
    fold_test: list[np.ndarray]
    window_slots: int


def _dense(df: pl.DataFrame, start: datetime, end: datetime, columns: list[str]) -> tuple:
    """窓内の 5分グリッドへ密に配置する(欠損は NaN)。"""
    start_ms = int(start.timestamp() * 1000)
    n_slots = int((end.timestamp() * 1000 - start_ms) // BAR_MS)
    sub = df.filter((pl.col("ts") >= start) & (pl.col("ts") < end))
    idx = ((sub["ts"].dt.epoch("ms").to_numpy() - start_ms) // BAR_MS).astype(int)
    out = np.full((n_slots, len(columns)), np.nan)
    values = sub.select(columns).to_numpy()
    out[idx] = values
    grid_ts = start_ms + np.arange(n_slots) * BAR_MS
    return out, grid_ts


def prepare_cell(
    design: pl.DataFrame,
    info_set,
    horizon: int,
    target: str,
    start: datetime,
    end: datetime,
) -> CellData:
    a_cols = list(P.A_COLUMNS)
    x_cols = list(info_set.model_columns)
    y_col = f"fwd_{target.lower()}_h{horizon}"
    dense, grid_ts = _dense(design, start, end, a_cols + x_cols + [y_col])
    p_a, p_x = len(a_cols), len(x_cols)
    a = dense[:, :p_a]
    x = dense[:, p_a : p_a + p_x]
    y = dense[:, -1]
    valid = np.isfinite(a).all(axis=1) & np.isfinite(x).all(axis=1) & np.isfinite(y)

    # 月次被覆ゲート(§7): 被覆 95% 未満の暦月はブロックごと除外
    months = np.array(
        [datetime.fromtimestamp(t / 1000, UTC).strftime("%Y-%m") for t in grid_ts]
    )
    for month in np.unique(months):
        mask = months == month
        if valid[mask].mean() < P.MONTHLY_COVERAGE_MIN:
            valid[mask] = False

    folds = make_folds(start, end)
    purge = horizon + 1 + P.FOLD["embargo_bars"]
    tail = horizon + 1  # block 末尾 purge(§9.3)
    fold_train, fold_test = [], []
    for fold in folds:
        test_start_i = int((fold.test_start.timestamp() * 1000 - grid_ts[0]) // BAR_MS)
        test_end_i = int((fold.test_end.timestamp() * 1000 - grid_ts[0]) // BAR_MS)
        train_idx = np.where(valid[: max(test_start_i - purge, 0)])[0]
        test_slice = np.zeros(len(valid), dtype=bool)
        test_slice[test_start_i : max(test_end_i - tail, test_start_i)] = True
        test_idx = np.where(valid & test_slice)[0]
        if len(train_idx) > 500 and len(test_idx) > 100:
            fold_train.append(train_idx)
            fold_test.append(test_idx)
    return CellData(grid_ts, a, x, y, valid, fold_train, fold_test, len(valid))


def evaluate(cell: CellData, x_provider=None) -> dict:
    """A と B を全 fold で学習・評価し、pooled OOS の統計を返す。

    `x_provider(fold_index, train_idx) -> X 行列` を渡すと placebo を評価できる。
    placebo は fold ごとに Gamma_hat が違うので、fold 単位で X を作り直す必要がある。
    """
    preds_a, preds_b, ys, means, idxs, fold_ids, alphas = [], [], [], [], [], [], []
    p_a = cell.a.shape[1]
    purge = P.FOLD["embargo_bars"]
    for k, (train_idx, test_idx) in enumerate(zip(cell.fold_train, cell.fold_test)):
        x = cell.x if x_provider is None else x_provider(k, train_idx)
        ok_train = np.isfinite(x[train_idx]).all(axis=1)
        ok_test = np.isfinite(x[test_idx]).all(axis=1)
        tr, te = train_idx[ok_train], test_idx[ok_test]
        if len(tr) < 500 or len(te) < 100:
            continue
        z_train = np.empty((len(tr), p_a + x.shape[1]))
        z_test = np.empty((len(te), p_a + x.shape[1]))
        z_train[:, :p_a], z_train[:, p_a:] = cell.a[tr], x[tr]
        z_test[:, :p_a], z_test[:, p_a:] = cell.a[te], x[te]
        z_train, z_test = _standardize(z_train, z_test)
        pa, pb, mean, alpha = fit_nested_pair(z_train, cell.y[tr], z_test, p_a, purge)
        preds_a.append(pa)
        preds_b.append(pb)
        ys.append(cell.y[te])
        means.append(np.full(len(te), mean))
        idxs.append(te)
        fold_ids.append(np.full(len(te), k))
        alphas.append(alpha)
    if not ys:
        return {"n": 0}
    y = np.concatenate(ys)
    pa, pb = np.concatenate(preds_a), np.concatenate(preds_b)
    mean = np.concatenate(means)
    r2_a, r2_b = _r2(y, pa, mean), _r2(y, pb, mean)
    return {
        "n": int(len(y)),
        "r2_a": r2_a,
        "r2_b": r2_b,
        "dr2": r2_b - r2_a,
        "y": y,
        "pred_a": pa,
        "pred_b": pb,
        "mean_a": mean,
        "mean_b": mean,
        "index": np.concatenate(idxs),
        "fold": np.concatenate(fold_ids),
        "alphas": alphas,
    }


@dataclass
class FoldCache:
    """fold ごとに1回だけ作る A 側の材料(placebo 間で変わらない部分)。

    placebo が行を落とさない限り、A の標準化列・Gram・解は observed と同一である
    (行集合も標準化統計も同じなので)。そこを使い回して X 側だけ計算し直す。
    数学的には素朴実装と同一で、メモリ転送量だけが減る。
    """

    train: np.ndarray
    test: np.ndarray
    a_train: np.ndarray  # 生の A(学習行を連続メモリへ gather 済み)
    a_test: np.ndarray
    za_train: np.ndarray  # 標準化済み A(学習行)
    za_test: np.ndarray
    y_train: np.ndarray
    y_mean: float
    yc: np.ndarray
    edges: list[int]  # 内側 CV の prefix 境界(purge 済み)+ 学習末尾
    inner: list[tuple[int, int, int]]
    gaa: list[np.ndarray]  # block ごとの A'A
    ray: list[np.ndarray]  # block ごとの A'yc
    pred_a: np.ndarray
    alpha_a: float


def _standardize_inplace(train: np.ndarray, test: np.ndarray) -> None:
    mean = train.mean(axis=0)
    std = train.std(axis=0)
    if np.any(std <= 0):
        raise QualityFailure("学習集合に std == 0 の列がある")
    train -= mean
    train /= std
    np.clip(train, -CLIP, CLIP, out=train)
    test -= mean
    test /= std
    np.clip(test, -CLIP, CLIP, out=test)


def _blocks(n: int, purge: int) -> tuple[list[tuple[int, int, int]], list[int]]:
    bounds = np.linspace(0, n, 5).astype(int)
    inner = [(max(int(bounds[i]) - purge, 1), int(bounds[i]), int(bounds[i + 1])) for i in (1, 2, 3)]
    inner = [(e, v0, v1) for e, v0, v1 in inner if e > 50 and v1 - v0 > 50]
    edges = [e for e, _, _ in inner] + [n]
    return inner, edges


def _prefix_solve(
    gram_blocks: list[np.ndarray],
    rhs_blocks: list[np.ndarray],
    inner: list[tuple[int, int, int]],
    z_train: np.ndarray,
    yc: np.ndarray,
) -> tuple[np.ndarray, float]:
    """block を積み上げて prefix Gram を作り、内側 CV で alpha を選んで解く。"""
    gram = np.zeros_like(gram_blocks[0])
    rhs = np.zeros_like(rhs_blocks[0])
    sse = np.zeros(len(ALPHAS))
    for (edge, v0, v1), gb, rb in zip(inner, gram_blocks, rhs_blocks):
        gram += gb
        rhs += rb
        betas = _ridge_path(gram, rhs)
        zv = z_train[v0:v1]
        residual = yc[v0:v1][None, :] - betas @ zv.T
        sse += (residual**2).sum(axis=1)
    for gb, rb in zip(gram_blocks[len(inner):], rhs_blocks[len(inner):]):
        gram += gb
        rhs += rb
    alpha = float(ALPHAS[int(np.argmin(sse))]) if inner else float(ALPHAS[2])
    beta = np.linalg.solve(gram + alpha * np.eye(gram.shape[0]), rhs)
    return beta, alpha


def prepare_folds(cell: CellData) -> list[FoldCache]:
    """A 側の材料を fold ごとに1回だけ用意する(observed の A 解も含む)。"""
    purge = P.FOLD["embargo_bars"]
    caches: list[FoldCache] = []
    for train_idx, test_idx in zip(cell.fold_train, cell.fold_test):
        a_train = np.ascontiguousarray(cell.a[train_idx])
        a_test = np.ascontiguousarray(cell.a[test_idx])
        za_train, za_test = a_train.copy(), a_test.copy()
        _standardize_inplace(za_train, za_test)
        y_train = cell.y[train_idx]
        y_mean = float(y_train.mean())
        yc = y_train - y_mean
        inner, edges = _blocks(len(train_idx), purge)
        gaa, ray, cursor = [], [], 0
        for edge in edges:
            block = za_train[cursor:edge]
            gaa.append(block.T @ block)
            ray.append(block.T @ yc[cursor:edge])
            cursor = edge
        beta_a, alpha_a = _prefix_solve(gaa, ray, inner, za_train, yc)
        caches.append(
            FoldCache(
                train=train_idx,
                test=test_idx,
                a_train=a_train,
                a_test=a_test,
                za_train=za_train,
                za_test=za_test,
                y_train=y_train,
                y_mean=y_mean,
                yc=yc,
                edges=edges,
                inner=inner,
                gaa=gaa,
                ray=ray,
                pred_a=y_mean + za_test @ beta_a,
                alpha_a=alpha_a,
            )
        )
    return caches


def evaluate_fast(cell: CellData, caches: list[FoldCache], x_provider=None) -> dict:
    """A 側キャッシュを使って B(と必要なら A)を評価する。

    placebo が行を落とす場合(シフト後 null)は、その fold だけ素朴経路へフォールバックし、
    `S_d` の上で A と B を両方とも作り直す(事前登録 §12.3)。
    """
    p_a = cell.a.shape[1]
    preds_a, preds_b, ys, means, idxs, fold_ids, alphas, dropped = [], [], [], [], [], [], [], 0
    for k, cache in enumerate(caches):
        x = cell.x if x_provider is None else x_provider(k, cache.train)
        x_tr = x[cache.train]
        x_te = x[cache.test]
        ok_tr = np.isfinite(x_tr).all(axis=1)
        ok_te = np.isfinite(x_te).all(axis=1)
        if ok_tr.all() and ok_te.all():
            zx_tr = np.ascontiguousarray(x_tr)
            zx_te = np.ascontiguousarray(x_te)
            _standardize_inplace(zx_tr, zx_te)
            gram_blocks, rhs_blocks, cursor = [], [], 0
            for i, edge in enumerate(cache.edges):
                za_b = cache.za_train[cursor:edge]
                zx_b = zx_tr[cursor:edge]
                gax = za_b.T @ zx_b
                gxx = zx_b.T @ zx_b
                block = np.empty((p_a + zx_tr.shape[1], p_a + zx_tr.shape[1]))
                block[:p_a, :p_a] = cache.gaa[i]
                block[:p_a, p_a:] = gax
                block[p_a:, :p_a] = gax.T
                block[p_a:, p_a:] = gxx
                rhs = np.empty(p_a + zx_tr.shape[1])
                rhs[:p_a] = cache.ray[i]
                rhs[p_a:] = zx_b.T @ cache.yc[cursor:edge]
                gram_blocks.append(block)
                rhs_blocks.append(rhs)
                cursor = edge
            z_train_joint = np.empty((len(cache.train), p_a + zx_tr.shape[1]))
            z_train_joint[:, :p_a] = cache.za_train
            z_train_joint[:, p_a:] = zx_tr
            beta_b, alpha_b = _prefix_solve(
                gram_blocks, rhs_blocks, cache.inner, z_train_joint, cache.yc
            )
            pred_b = cache.y_mean + (cache.za_test @ beta_b[:p_a] + zx_te @ beta_b[p_a:])
            pred_a = cache.pred_a
            mean = cache.y_mean
            te_rows = cache.test
            alpha = (cache.alpha_a, alpha_b)
        else:  # 行が落ちた -> S_d の上で A も B も作り直す
            dropped += 1
            tr, te = cache.train[ok_tr], cache.test[ok_te]
            if len(tr) < 500 or len(te) < 100:
                continue
            width = p_a + x.shape[1]
            z_train = np.empty((len(tr), width))
            z_test = np.empty((len(te), width))
            z_train[:, :p_a] = cache.a_train[ok_tr]
            z_train[:, p_a:] = x_tr[ok_tr]
            z_test[:, :p_a] = cache.a_test[ok_te]
            z_test[:, p_a:] = x_te[ok_te]
            _standardize_inplace(z_train, z_test)
            y_tr = cell.y[tr]
            y_mean = float(y_tr.mean())
            yc = y_tr - y_mean
            inner, edges = _blocks(len(tr), P.FOLD["embargo_bars"])
            gram_blocks, rhs_blocks, cursor = [], [], 0
            for edge in edges:
                block = z_train[cursor:edge]
                gram_blocks.append(block.T @ block)
                rhs_blocks.append(block.T @ yc[cursor:edge])
                cursor = edge
            beta_b, alpha_b = _prefix_solve(gram_blocks, rhs_blocks, inner, z_train, yc)
            beta_a, alpha_a = _prefix_solve(
                [g[:p_a, :p_a] for g in gram_blocks],
                [r[:p_a] for r in rhs_blocks],
                inner,
                z_train[:, :p_a],
                yc,
            )
            pred_a = y_mean + z_test[:, :p_a] @ beta_a
            pred_b = y_mean + z_test @ beta_b
            mean = y_mean
            te_rows = te
            alpha = (alpha_a, alpha_b)
        preds_a.append(pred_a)
        preds_b.append(pred_b)
        ys.append(cell.y[te_rows])
        means.append(np.full(len(te_rows), mean))
        idxs.append(te_rows)
        fold_ids.append(np.full(len(te_rows), k))
        alphas.append(alpha)
    if not ys:
        return {"n": 0}
    y = np.concatenate(ys)
    pa, pb = np.concatenate(preds_a), np.concatenate(preds_b)
    mean = np.concatenate(means)
    r2_a, r2_b = _r2(y, pa, mean), _r2(y, pb, mean)
    return {
        "n": int(len(y)),
        "r2_a": r2_a,
        "r2_b": r2_b,
        "dr2": r2_b - r2_a,
        "y": y,
        "pred_a": pa,
        "pred_b": pb,
        "mean_a": mean,
        "mean_b": mean,
        "index": np.concatenate(idxs),
        "fold": np.concatenate(fold_ids),
        "alphas": alphas,
        "folds_recomputed": dropped,
    }


def placebo_shifts(window_days: int, k: int | None) -> list[int]:
    """許容シフト集合 S = {30 .. W-30}。k=None なら全数(§12.3)。"""
    low = P.PLACEBO["min_shift_days"]
    high = window_days - low
    all_shifts = list(range(low, high + 1))
    if k is None or k >= len(all_shifts):
        return all_shifts
    return [all_shifts[i] for i in np.linspace(0, len(all_shifts) - 1, k).astype(int)]


def fold_projections(cell: CellData) -> list[np.ndarray]:
    """fold ごとに、学習行だけで推定した Gamma_hat による残差 E = X - A @ Gamma_hat。

    Gamma_hat は **学習行のみ**で推定し(テスト期間の統計を使わない)、
    残差は fold の全行へ同じ Gamma_hat を適用して作る。
    """
    residuals = []
    for train_idx in cell.fold_train:
        ok = np.isfinite(cell.x[train_idx]).all(axis=1)
        tr = train_idx[ok]
        gamma, *_ = np.linalg.lstsq(cell.a[tr], cell.x[tr], rcond=None)
        fitted = cell.a @ gamma
        residuals.append((fitted, cell.x - fitted))
    return residuals


def a_projection_provider(projections: list, shift_days: int):
    """Bp: X_p = A @ Gamma_hat + roll(E)(fold ごとの Gamma_hat を使う)。

    roll は fold に依存しないので、シフトごとに1回だけ計算して使い回す。
    """
    shift = shift_days * BARS_PER_DAY
    rolled = [np.roll(residual, shift, axis=0) for _, residual in projections]

    def provider(fold_index: int, _train_idx: np.ndarray) -> np.ndarray:
        return projections[fold_index][0] + rolled[fold_index]

    return provider


def naive_shift_provider(cell: CellData, shift_days: int):
    """Bt: X 全体を巡回シフト(副次・反保守的なので判定に使わない)。"""
    shifted = np.roll(cell.x, shift_days * BARS_PER_DAY, axis=0)

    def provider(_fold_index: int, _train_idx: np.ndarray) -> np.ndarray:
        return shifted

    return provider


# --------------------------------------------------------------------------------------
# 診断(§13 / §15 / §16)
# --------------------------------------------------------------------------------------


def _fold_dr2(result: dict) -> list[float]:
    out = []
    for k in np.unique(result["fold"]):
        mask = result["fold"] == k
        out.append(
            _r2(result["y"][mask], result["pred_b"][mask], result["mean_b"][mask])
            - _r2(result["y"][mask], result["pred_a"][mask], result["mean_a"][mask])
        )
    return [float(v) for v in out]


def _leave_one_block_out(result: dict) -> list[float]:
    out = []
    for k in np.unique(result["fold"]):
        mask = result["fold"] != k
        out.append(
            _r2(result["y"][mask], result["pred_b"][mask], result["mean_b"][mask])
            - _r2(result["y"][mask], result["pred_a"][mask], result["mean_a"][mask])
        )
    return [float(v) for v in out]


def _by_year(result: dict, grid_ts: np.ndarray) -> dict:
    years = np.array(
        [datetime.fromtimestamp(t / 1000, UTC).year for t in grid_ts[result["index"]]]
    )
    out = {}
    for year in np.unique(years):
        mask = years == year
        out[str(year)] = float(
            _r2(result["y"][mask], result["pred_b"][mask], result["mean_b"][mask])
            - _r2(result["y"][mask], result["pred_a"][mask], result["mean_a"][mask])
        )
    return out


def _day_diagnostics(result: dict, grid_ts: np.ndarray) -> dict:
    days = (grid_ts[result["index"]] // 86_400_000).astype(int)
    dsse = (result["y"] - result["pred_a"]) ** 2 - (result["y"] - result["pred_b"]) ** 2
    unique_days, inverse = np.unique(days, return_inverse=True)
    per_day = np.bincount(inverse, weights=dsse)
    worst = unique_days[int(np.argmax(per_day))]
    keep = days != worst
    dr2_without = float(
        _r2(result["y"][keep], result["pred_b"][keep], result["mean_b"][keep])
        - _r2(result["y"][keep], result["pred_a"][keep], result["mean_a"][keep])
    )
    # day-cluster Rademacher randomization(副次 p。capacity を統制しないので判定に使わない)
    rng = np.random.default_rng(P.SEEDS["dev"])
    observed = per_day.sum()
    signs = rng.choice([-1.0, 1.0], size=(P.RANDOMIZATION["reps"], len(per_day)))
    null = signs @ per_day
    p_rand = float((1 + int((null >= observed).sum())) / (1 + P.RANDOMIZATION["reps"]))
    return {
        "utc_days": int(len(unique_days)),
        "dr2_without_most_influential_day": dr2_without,
        "randomization_p": p_rand,
    }


def _cost_translation(result: dict) -> dict:
    """Y1 の十分位スプレッド(上限指標。§16)。"""
    pred, y = result["pred_b"], result["y"]
    order = np.argsort(pred)
    n = len(y) // 10
    bottom, top = order[:n], order[-n:]
    top_bps = float(y[top].mean() * 1e4)
    bottom_bps = float(y[bottom].mean() * 1e4)
    return {
        "top_decile_bps": top_bps,
        "bottom_decile_bps": bottom_bps,
        "edge_bps": (top_bps - bottom_bps) / 2,
        "round_trip_bps": P.COST_BASIS_BPS["round_trip"],
        "stress_bps": P.COST_BASIS_BPS["stress"],
        "note": "重複保有・執行滑りを考慮しない上限指標。戦略損益ではない",
    }


def _sign_stability(cell: CellData, result: dict) -> dict:
    """A 残差化した偏相関の符号(§15-1)。fold ごとに学習行で射影を作る。"""
    signs = []
    for k, (train_idx, test_idx) in enumerate(zip(cell.fold_train, cell.fold_test)):
        mask = result["fold"] == k
        if not mask.any():
            continue
        ok = np.isfinite(cell.x[train_idx]).all(axis=1)
        tr = train_idx[ok]
        gamma, *_ = np.linalg.lstsq(cell.a[tr], cell.x[tr], rcond=None)
        te = result["index"][mask]
        resid_x = cell.x[te] - cell.a[te] @ gamma
        resid_y = result["y"][mask] - result["pred_a"][mask]
        col = resid_x[:, 0]
        if col.std() > 0 and resid_y.std() > 0:
            signs.append(float(np.corrcoef(col, resid_y)[0, 1]))
    if not signs:
        return {"partial_correlations": [], "sign_agreement": None}
    positive = sum(1 for s in signs if s > 0)
    return {
        "partial_correlations": [round(s, 6) for s in signs],
        "sign_agreement": round(max(positive, len(signs) - positive) / len(signs), 4),
        "dominant_sign": 1 if positive * 2 >= len(signs) else -1,
    }


def _ic(result: dict) -> dict:
    def corr(pred):
        if np.std(pred) == 0 or np.std(result["y"]) == 0:
            return float("nan")
        return float(np.corrcoef(pred, result["y"])[0, 1])

    return {"ic_a": corr(result["pred_a"]), "ic_b": corr(result["pred_b"])}


# --------------------------------------------------------------------------------------
# 実行
# --------------------------------------------------------------------------------------


def run_cell(
    design: pl.DataFrame, info_set, horizon: int, target: str, stage: str, placebo_k: int | None = None
) -> dict:
    start = info_set.dev_start if stage == "dev" else P.CONFIRMATION_START
    end = P.DEV_END if stage == "dev" else P.CONFIRMATION_END
    cell = prepare_cell(design, info_set, horizon, target, start, end)
    result = evaluate_fast(cell, prepare_folds(cell))
    entry: dict = {
        "set": info_set.id,
        "horizon_bars": horizon,
        "target": target,
        "window": [start.isoformat(), end.isoformat()],
        "folds": len(cell.fold_train),
    }
    if not result.get("n"):
        entry |= {"status": "insufficient_sample", "p": 1.0, "n": 0}
        return entry

    n_eff = result["n"] / horizon
    days = len(np.unique((cell.grid_ts[result["index"]] // 86_400_000).astype(int)))
    minimum = P.MIN_SAMPLE[stage]
    entry |= {
        "n": result["n"],
        "n_eff": round(n_eff, 1),
        "utc_days": days,
        "r2_a": result["r2_a"],
        "r2_b": result["r2_b"],
        "dr2": result["dr2"],
    }
    if n_eff < minimum["n_eff"] or days < minimum["utc_days"]:
        entry |= {"status": "insufficient_sample", "p": 1.0}
        return entry

    window_days = (end - start).days
    shifts = placebo_shifts(window_days, placebo_k or P.PLACEBO["k_stage1"])
    projections = fold_projections(cell)
    caches = prepare_folds(cell)
    null_dr2 = []
    for shift in shifts:
        res_p = evaluate_fast(cell, caches, x_provider=a_projection_provider(projections, shift))
        if res_p.get("n"):
            null_dr2.append(res_p["dr2"])
    null_dr2 = np.array(null_dr2)
    exceed = int((null_dr2 >= result["dr2"]).sum())
    p_value = (1 + exceed) / (1 + len(null_dr2))

    entry |= {
        "status": "tested",
        "placebo_k_stage1": len(null_dr2),
        "placebo_exceed": exceed,
        "p_stage1": p_value,
        "p": p_value,
        "mde": float(np.percentile(null_dr2, 95)),
        "placebo_mean": float(null_dr2.mean()),
        "fold_dr2": _fold_dr2(result),
        "leave_one_block_out_dr2": _leave_one_block_out(result),
        "dr2_by_year": _by_year(result, cell.grid_ts),
        "sign": _sign_stability(cell, result),
        **_ic(result),
        **_day_diagnostics(result, cell.grid_ts),
        "stage2_candidate": exceed <= 5,
    }
    entry["dic"] = entry["ic_b"] - entry["ic_a"]
    if target == "Y1":
        entry["cost"] = _cost_translation(result)
    return entry


def holm(entries: list[dict], alpha: float = 0.05) -> None:
    """Holm 補正(family size は固定。検定不能も p=1 で含める)。"""
    order = sorted(range(len(entries)), key=lambda i: entries[i].get("p", 1.0))
    m = len(entries)
    previous = 0.0
    for rank, i in enumerate(order):
        p = entries[i].get("p", 1.0)
        adjusted = max(previous, min(1.0, (m - rank) * p))
        previous = adjusted
        entries[i]["p_holm"] = adjusted
        entries[i]["holm_significant"] = adjusted <= alpha


def benjamini_hochberg(entries: list[dict], q: float = 0.10) -> None:
    order = sorted(range(len(entries)), key=lambda i: entries[i].get("p", 1.0))
    m = len(entries)
    threshold = 0
    for rank, i in enumerate(order, start=1):
        if entries[i].get("p", 1.0) <= q * rank / m:
            threshold = rank
    for rank, i in enumerate(order, start=1):
        entries[i]["bh_significant"] = rank <= threshold


def main() -> None:
    parser = argparse.ArgumentParser(description="Phase 7 Tier 0 screening")
    parser.add_argument("--stage", choices=("dev", "confirmation"), required=True)
    parser.add_argument("--symbol", default=DEFAULT_SYMBOL)
    parser.add_argument("--cells", type=int, default=None, help="先頭 N cell だけ(配管検証用)")
    parser.add_argument(
        "--placebo-k",
        type=int,
        default=None,
        help="placebo 数を減らして配管だけ確認する(smoke 専用。公式 artifact は書かない)",
    )
    args = parser.parse_args()

    dev_artifact = ARTIFACT_DIR / "tier0_screening_dev_v1.json"
    if args.stage == "confirmation" and not dev_artifact.exists():
        raise SystemExit("dev の artifact が無い。confirmation は dev の後にしか実行できない")

    features_path = config.binance_features_parquet(args.symbol)
    labels_path = config.LABELS_DIR / f"{config.BINANCE_SOURCE}_{args.symbol}_{config.BAR}.parquet"
    features = pl.read_parquet(features_path)
    labels = pl.read_parquet(labels_path)
    if args.stage == "dev":
        max_ts = features["ts"].max()
        if max_ts >= P.CONFIRMATION_START:
            features = features.filter(pl.col("ts") < P.CONFIRMATION_START)
            labels = labels.filter(pl.col("ts") < P.CONFIRMATION_START)

    design = build_design(features).join(
        labels.select(["ts"] + [c for c in labels.columns if c.startswith("fwd_")]),
        on="ts",
        how="left",
    )

    started = time.perf_counter()
    entries: list[dict] = []
    family = P.family()
    if args.cells:
        family = family[: args.cells]
    sets = {s.id: s for s in P.INFORMATION_SETS}
    for set_id, horizon, target in family:
        cell_started = time.perf_counter()
        entry = run_cell(design, sets[set_id], horizon, target, args.stage, args.placebo_k)
        entry["runtime_sec"] = round(time.perf_counter() - cell_started, 1)
        entries.append(entry)
        print(
            f"{set_id:6s} h={horizon:2d} {target}  n_eff={entry.get('n_eff', 0):>9} "
            f"dR2={entry.get('dr2', float('nan')):+.3e} p={entry.get('p', 1.0):.4f} "
            f"({entry['runtime_sec']}s)",
            flush=True,
        )
    holm(entries)
    benjamini_hochberg(entries)

    report = {
        "report": f"phase7_tier0_screening_{args.stage}_v1",
        "protocol": P.PROTOCOL,
        "stage": args.stage,
        "prereg_sha256": hashlib.sha256(Path(P.__file__).read_bytes()).hexdigest(),
        "freeze_record": json.loads((ARTIFACT_DIR / "tier0_freeze.json").read_text())
        if (ARTIFACT_DIR / "tier0_freeze.json").exists()
        else None,
        "source_commit": experiments.git_commit_hash(),
        "family_size": len(P.family()),
        "seeds": dict(P.SEEDS),
        "placebo": {k: v for k, v in P.PLACEBO.items()},
        "runtime_sec": round(time.perf_counter() - started, 1),
        "cells": entries,
    }
    smoke = args.placebo_k is not None or args.cells is not None
    if smoke:
        report["smoke_test"] = True
        report["warning"] = "配管検証。凍結値と違う placebo 数/cell 数なので公式結果ではない"
    suffix = "_smoke" if smoke else ""
    out = ARTIFACT_DIR / f"tier0_screening_{args.stage}_v1{suffix}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True, default=float) + "\n", encoding="utf-8")
    print(f"\nwrote {out}")


if __name__ == "__main__":
    main()

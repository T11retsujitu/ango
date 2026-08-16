"""experiment artifact(機械可読な run 記録)の保存。

ROADMAP §8 準拠。各 run について config・データ指紋(manifest sha256)・
source commit・seed・約定/コスト規則・metrics を JSON で experiments/runs/ へ残す。
docs/findings の Markdown 台帳は「人間向け結論」としてこの上に併存する。
"""

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from mce import config
from mce.backtest.engine import BacktestResult

RUNS_DIR = Path("experiments") / "runs"

# 取引所差異の分類(docs/phase0 レポート §4.5)。artifact に必ず記録する
REPLICATION_CLASSES = (
    "original",  # 本プロジェクト発の仮説
    "exact_replication",  # 同一取引所・同一足・同一手順(現状は該当なし)
    "replication_inspired",  # 原論文の仮説・手順を借り、データ条件を変えて検証
    "method_transfer",  # 手法のみ移植(性能主張は引き継がない)
    "cross_exchange_validation",  # 別取引所で同種構造を検証
)


def git_commit_hash() -> str | None:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True, timeout=10, check=True
        )
        return out.stdout.strip()
    except Exception:
        return None


def next_experiment_id(runs_dir: Path) -> str:
    existing = sorted(runs_dir.glob("EXP-*.json"))
    n = 0
    for p in existing:
        try:
            n = max(n, int(p.stem.split("-")[1]))
        except (IndexError, ValueError):
            continue
    return f"EXP-{n + 1:04d}"


def build_artifact(
    result: BacktestResult,
    split: str,
    replication_class: str = "original",
    manifest_hashes: dict[str, str] | None = None,
    notes: str | None = None,
) -> dict:
    """artifact dict(experiment_id / created_at は save 時に付与)。"""
    if replication_class not in REPLICATION_CLASSES:
        raise ValueError(f"replication_class は {REPLICATION_CLASSES} のいずれか")
    return {
        "data": {
            "source": config.SOURCE,
            "symbol": config.INST_ID,
            "market_type": config.MARKET_TYPE,
            "timeframe": config.BAR,
            "timezone": "UTC",
            "split": split,
            "manifest_sha256": manifest_hashes or {},
        },
        "signal": {"feature_cutoff": "close_t", "execution": "open_t_plus_1"},
        "execution": {
            "bar_ms": result.execution.bar_ms,
            "cancel_after_ms": result.execution.cancel_after_ms,
            "max_holding_bars": result.execution.max_holding_bars,
        },
        "cost": {
            "scenario": result.cost.name,
            "fee_bps": result.cost.fee_bps,
            "spread_bps": result.cost.spread_bps,
            "slippage_bps": result.cost.slippage_bps,
            "funding": "excluded_phase0",
        },
        "strategy": {
            "name": result.strategy.name,
            "params": result.strategy.params,
            "seed": result.strategy.seed,
        },
        "replication_class": replication_class,
        "source_commit": git_commit_hash(),
        "metrics": result.metrics,
        "counts": {
            "bars": result.metrics["bars"],
            "fills": result.fills.height,
            "trades_closed": result.metrics["trade_count"],
            "cancelled_fills": result.cancelled_count,
        },
        "notes": notes,
    }


def save_artifact(artifact: dict, runs_dir: Path = RUNS_DIR, experiment_id: str | None = None) -> Path:
    runs_dir.mkdir(parents=True, exist_ok=True)
    exp_id = experiment_id or next_experiment_id(runs_dir)
    record = {
        "experiment_id": exp_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        **artifact,
    }
    path = runs_dir / f"{exp_id}.json"
    if path.exists():
        raise FileExistsError(f"{path} は既に存在する(artifact は追記専用・上書き禁止)")
    path.write_text(json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path

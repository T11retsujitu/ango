"""search budget の会計と candidate 台帳(ROADMAP §4.5)。

「N個試して一番良かった値だけ報告する」を構造的に禁止するため、
全 draw を追記専用 JSONL に記録し、counters は機械的に集計される。
"""

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path


@dataclass
class BudgetCounters:
    candidate_count: int = 0  # 全 draw
    unique_candidate_count: int = 0  # validator 通過かつ非重複
    duplicate_count: int = 0
    rejected_candidate_count: int = 0  # validator 拒否
    runtime_failure_count: int = 0  # compile / backtest 例外
    evaluated_count: int = 0  # research 評価数(= budget 消費)
    research_pass_count: int = 0
    validation_count: int = 0
    survivor_count: int = 0


@dataclass
class SearchLedger:
    out_dir: Path
    counters: BudgetCounters = field(default_factory=BudgetCounters)

    def __post_init__(self):
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.out_dir / "candidates.jsonl"
        if self.path.exists():
            raise FileExistsError(f"{self.path} は既に存在する(公式runは1回。再実行は別ディレクトリで)")
        # ファイルは最初の record で作る。1件も記録せずに落ちた run(API 認証エラー等)が
        # 空ファイルを残して再実行を塞がないようにするため(記録済みの run は上書きされない)。

    def record(self, record: dict) -> None:
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")

    def summary_counters(self) -> dict:
        return asdict(self.counters)

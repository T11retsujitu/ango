"""ローカルデータ在庫の決定論的インベントリ(Phase 7 情報空間の入力)。

    python -m mce.data_inventory
    python -m mce.data_inventory --json data/analysis/data_inventory.json

「どの情報集合を、いつからいつまで、どれだけ持っているか」を機械的に出す。
data/ は git 管理外(manifests を除く)なので、clone 直後は manifests だけが
埋まり、実データ・microstructure shard は `present: false` になる。これは
異常ではなく、**その環境にデータが無い**という事実の記録である。

情報集合の優先順位付け(docs/phase7/information_space_expansion_v1.md)は、
「理論上強そうか」ではなく、この在庫と取得コストを入力に決める。
"""

import argparse
import json
from pathlib import Path

from mce import config

MICROSTRUCTURE_ROOT = Path("data/normalized/okx/microstructure/v3")
MICROSTRUCTURE_TABLES = (
    "trades",
    "bbo",
    "book_messages",
    "book_levels",
    "instrument_metadata",
    "session_controls",
)
RAW_WS_ROOT = Path("data/raw/okx/ws")
RAW_WS_STREAMS = ("public", "business")


def _file_stats(paths: list[Path]) -> dict:
    return {
        "files": len(paths),
        "bytes": sum(p.stat().st_size for p in paths),
    }


def manifest_inventory(manifests_dir: Path | None = None) -> dict:
    """git 管理された manifest(= データ指紋)の在庫。データ本体が無くても読める。"""
    manifests_dir = manifests_dir or config.MANIFESTS_DIR
    out: dict = {"dir": manifests_dir.as_posix(), "datasets": {}}
    if not manifests_dir.is_dir():
        out["present"] = False
        return out
    out["present"] = True
    for path in sorted(manifests_dir.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        out["datasets"][path.stem] = {
            "rows": data.get("rows"),
            "missing_rows": data.get("missing_rows"),
            "ts_min": data.get("ts_min"),
            "ts_max": data.get("ts_max"),
            "sha256": data.get("sha256"),
            "size_bytes": data.get("size_bytes"),
        }
    return out


def local_parquet_inventory() -> dict:
    """OHLCV 系 parquet の実体在庫(存在するかどうかとサイズのみ。読み込まない)。"""
    targets = {
        "ohlcv": config.ohlcv_parquet(),
        "funding_rate": config.funding_parquet(),
        "open_interest": config.open_interest_parquet(),
        "features": config.features_parquet(),
        "labels": config.labels_parquet(),
    }
    return {
        name: {
            "path": path.as_posix(),
            "present": path.exists(),
            "size_bytes": path.stat().st_size if path.exists() else 0,
        }
        for name, path in sorted(targets.items())
    }


def microstructure_inventory(root: Path = MICROSTRUCTURE_ROOT) -> dict:
    """normalized microstructure shard の table 別在庫と arrival_date 範囲。"""
    out: dict = {"root": root.as_posix(), "present": root.is_dir(), "tables": {}}
    if not out["present"]:
        return out
    all_days: set[str] = set()
    for table in MICROSTRUCTURE_TABLES:
        table_dir = root / table
        if not table_dir.is_dir():
            out["tables"][table] = {"present": False}
            continue
        shards = sorted(table_dir.rglob("*.parquet"))
        days = sorted(
            {
                part.name.split("=", 1)[1]
                for shard in shards
                for part in shard.parents
                if part.name.startswith("arrival_date=")
            }
        )
        all_days.update(days)
        out["tables"][table] = {
            "present": True,
            **_file_stats(shards),
            "arrival_days": len(days),
            "arrival_date_min": days[0] if days else None,
            "arrival_date_max": days[-1] if days else None,
        }
    out["arrival_days_total"] = len(all_days)
    out["arrival_date_min"] = min(all_days) if all_days else None
    out["arrival_date_max"] = max(all_days) if all_days else None
    return out


def raw_ws_inventory(root: Path = RAW_WS_ROOT) -> dict:
    """WS raw(再取得不能)の stream 別在庫。`.partial` は clean close していない証拠。"""
    out: dict = {"root": root.as_posix(), "present": root.is_dir(), "streams": {}}
    if not out["present"]:
        return out
    for stream in RAW_WS_STREAMS:
        stream_dir = root / stream
        if not stream_dir.is_dir():
            out["streams"][stream] = {"present": False}
            continue
        closed = sorted(stream_dir.rglob("*.jsonl.gz"))
        partial = sorted(stream_dir.rglob("*.jsonl.gz.partial"))
        # data/raw/okx/ws/<stream>/YYYY/MM/DD/<file>
        days = sorted(
            {
                "-".join(p.parts[-4:-1])
                for p in closed
                if len(p.parts) >= 4 and p.parts[-4].isdigit()
            }
        )
        out["streams"][stream] = {
            "present": True,
            **_file_stats(closed),
            "partial_files": len(partial),
            "utc_days": len(days),
            "utc_day_min": days[0] if days else None,
            "utc_day_max": days[-1] if days else None,
        }
    return out


def build_inventory() -> dict:
    """全セクションを1つの決定論的レポートにまとめる(時刻・乱数を含めない)。"""
    return {
        "report": "data_inventory_v1",
        "manifests": manifest_inventory(),
        "local_parquet": local_parquet_inventory(),
        "microstructure_normalized": microstructure_inventory(),
        "microstructure_raw_ws": raw_ws_inventory(),
    }


def render(inventory: dict) -> str:
    lines: list[str] = ["# data inventory", "", "## manifests (git 管理)", ""]
    manifests = inventory["manifests"]
    if not manifests.get("present"):
        lines.append(f"(なし: {manifests['dir']})")
    else:
        lines.append("| dataset | rows | missing | ts_min | ts_max |")
        lines.append("|---|---:|---:|---|---|")
        for name, d in manifests["datasets"].items():
            lines.append(
                f"| {name} | {d['rows']} | {d['missing_rows']} | {d['ts_min']} | {d['ts_max']} |"
            )
    lines += ["", "## local parquet", "", "| dataset | present | bytes |", "|---|---|---:|"]
    for name, d in inventory["local_parquet"].items():
        lines.append(f"| {name} | {d['present']} | {d['size_bytes']} |")

    micro = inventory["microstructure_normalized"]
    lines += ["", "## microstructure normalized", ""]
    if not micro["present"]:
        lines.append(f"(なし: {micro['root']} — この環境には収集データが存在しない)")
    else:
        lines.append("| table | shards | bytes | arrival days | min | max |")
        lines.append("|---|---:|---:|---:|---|---|")
        for table, d in micro["tables"].items():
            if not d.get("present"):
                lines.append(f"| {table} | - | - | - | - | - |")
                continue
            lines.append(
                f"| {table} | {d['files']} | {d['bytes']} | {d['arrival_days']} | "
                f"{d['arrival_date_min']} | {d['arrival_date_max']} |"
            )

    raw = inventory["microstructure_raw_ws"]
    lines += ["", "## microstructure raw WS", ""]
    if not raw["present"]:
        lines.append(f"(なし: {raw['root']})")
    else:
        lines.append("| stream | closed files | bytes | partial | days | min | max |")
        lines.append("|---|---:|---:|---:|---:|---|---|")
        for stream, d in raw["streams"].items():
            if not d.get("present"):
                lines.append(f"| {stream} | - | - | - | - | - | - |")
                continue
            lines.append(
                f"| {stream} | {d['files']} | {d['bytes']} | {d['partial_files']} | "
                f"{d['utc_days']} | {d['utc_day_min']} | {d['utc_day_max']} |"
            )
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="local data inventory")
    parser.add_argument("--json", type=Path, default=None, help="機械可読 inventory の出力先")
    args = parser.parse_args()
    inventory = build_inventory()
    print(render(inventory))
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(
            json.dumps(inventory, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        print(f"\nwrote {args.json}")


if __name__ == "__main__":
    main()

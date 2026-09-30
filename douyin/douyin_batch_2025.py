"""Sequential Douyin collection for the four established 2025 keyword categories."""

from __future__ import annotations

import argparse
import csv
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = PROJECT_ROOT / "tencent_news" / "keywords"
DEFAULT_OUTPUT_ROOT = Path(os.getenv("CRAWLER_OUTPUT_ROOT", PROJECT_ROOT / "outputs")) / "douyin"
CATEGORY_CONFIGS = {
    "养老服务": CONFIG_DIR / "elderly_service_2025.json",
    "政务服务": CONFIG_DIR / "government_service_2025.json",
    "公共文化": CONFIG_DIR / "public_culture_2025.json",
    "互联网销售": CONFIG_DIR / "internet_sales_2025.json",
}

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def save_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def load_keywords(path: Path) -> list[str]:
    values = json.loads(path.read_text(encoding="utf-8"))
    return [" ".join(str(value).replace("+", " ").split()) for value in values]


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as file:
        return list(csv.DictReader(file))


def is_in_range(row: dict[str, str]) -> bool:
    return str(row.get("in_date_range", "")).strip().lower() in {"true", "1"}


def merge_keyword(source: Path, keyword: str, combined: Path) -> tuple[int, int]:
    incoming = [row for row in read_csv(source) if is_in_range(row)]
    for row in incoming:
        row["keyword"] = keyword
    current = [row for row in read_csv(combined) if row.get("keyword") != keyword]
    merged: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for row in [*current, *incoming]:
        key = (str(row.get("keyword", "")), str(row.get("content_id") or row.get("final_url") or ""))
        if key in seen:
            continue
        seen.add(key)
        merged.append(row)
    columns = list(dict.fromkeys(["keyword", *(key for row in merged for key in row)]))
    combined.parent.mkdir(parents=True, exist_ok=True)
    temporary = combined.with_suffix(combined.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=columns)
        writer.writeheader()
        writer.writerows({column: row.get(column, "") for column in columns} for row in merged)
    temporary.replace(combined)
    return len(incoming), len(merged)


def safe_stem(index: int, keyword: str) -> str:
    cleaned = "".join("_" if value in '<>:"/\\|?*' else value for value in keyword)
    return f"{index:02d}_{cleaned}"


def seed_completed_trial(category: str, progress: dict, combined: Path, output_root: Path) -> None:
    if category != "养老服务" or "天津 养老服务" in progress.get("completed", {}):
        return
    trial = output_root / "数据" / "抖音—养老服务_试跑.csv"
    if not trial.exists():
        return
    rows, combined_rows = merge_keyword(trial, "天津 养老服务", combined)
    if rows:
        progress.setdefault("completed", {})["天津 养老服务"] = {
            "rows_2025": rows,
            "source": str(trial),
            "combined_rows": combined_rows,
        }


def run_category(category: str, config: Path, args: argparse.Namespace) -> bool:
    keywords = load_keywords(config)
    data_dir = args.output_root / "数据"
    state_dir = args.output_root / "运行状态" / f"{category}_2025"
    log_dir = args.output_root / "日志" / f"{category}_2025"
    combined = data_dir / f"抖音—{category}.csv"
    progress_path = args.output_root / "运行状态" / f"抖音—{category}_采集进度.json"
    progress = load_json(progress_path) or {"completed": {}, "failed": {}}
    seed_completed_trial(category, progress, combined, args.output_root)
    save_json(progress_path, progress)
    crawler = Path(__file__).resolve().parent / "main.py"

    for index, keyword in enumerate(keywords, start=1):
        if keyword in progress.get("completed", {}):
            print(f"[{category} {index}/20] 跳过已完成：{keyword}")
            continue
        stem = safe_stem(index, keyword)
        result_csv = state_dir / f"{stem}_result.csv"
        checkpoint = state_dir / f"{stem}_checkpoint.csv"
        log = log_dir / f"{stem}.log"
        command = [
            sys.executable, str(crawler),
            "--keyword", keyword,
            "--start-date", "2025-01-01",
            "--end-date", "2025-12-31",
            "--max-results", str(args.max_results),
            "--max-scrolls", str(args.max_scrolls),
            "--delay", str(args.delay),
            "--timeout", str(args.timeout),
            "--initial-wait", str(args.initial_wait),
            "--manual-verify-timeout", str(args.manual_verify_timeout),
            "--output", str(result_csv),
            "--checkpoint", str(checkpoint),
            "--log", str(log),
            "--csv-only",
        ]
        print(f"[{category} {index}/20] 开始：{keyword}")
        result = subprocess.run(command, cwd=PROJECT_ROOT, check=False)
        if result.returncode == 0 and result_csv.exists():
            rows, combined_rows = merge_keyword(result_csv, keyword, combined)
            progress.setdefault("completed", {})[keyword] = {
                "rows_2025": rows,
                "combined_rows": combined_rows,
            }
            progress.setdefault("failed", {}).pop(keyword, None)
            result_csv.unlink(missing_ok=True)
            checkpoint.unlink(missing_ok=True)
            print(f"[{category} {index}/20] 完成：2025年 {rows} 条；汇总 {combined_rows} 条")
        else:
            progress.setdefault("failed", {})[keyword] = {
                "returncode": result.returncode,
                "log": str(log),
            }
            print(f"[{category} {index}/20] 验证/访问限制未解除，停止批处理并保留断点")
            progress.update({"category": category, "year": 2025, "updated_at": datetime.now().astimezone().isoformat(timespec="seconds")})
            save_json(progress_path, progress)
            return False
        progress.update({"category": category, "year": 2025, "updated_at": datetime.now().astimezone().isoformat(timespec="seconds")})
        save_json(progress_path, progress)
        time.sleep(args.keyword_delay)
    return True


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--category", choices=[*CATEGORY_CONFIGS, "全部"], default="养老服务")
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--max-results", type=int, default=100)
    parser.add_argument("--max-scrolls", type=int, default=20)
    parser.add_argument("--delay", type=float, default=3.0)
    parser.add_argument("--timeout", type=int, default=60_000)
    parser.add_argument("--initial-wait", type=float, default=8.0)
    parser.add_argument("--manual-verify-timeout", type=float, default=900.0)
    parser.add_argument("--keyword-delay", type=float, default=30.0)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    selected = CATEGORY_CONFIGS if args.category == "全部" else {args.category: CATEGORY_CONFIGS[args.category]}
    for category, config in selected.items():
        if not run_category(category, config, args):
            return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

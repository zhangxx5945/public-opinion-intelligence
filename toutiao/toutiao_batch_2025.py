"""Batch public-search collection for one or all configured Toutiao categories."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = Path(__file__).resolve().parent / "keywords"
DEFAULT_OUTPUT_ROOT = Path(os.getenv("CRAWLER_OUTPUT_ROOT", PROJECT_ROOT / "outputs")) / "toutiao"
CATEGORY_CONFIGS = {
    "养老服务": CONFIG_DIR / "elderly_service_2025.json",
    "政务服务": CONFIG_DIR / "government_service_2025.json",
    "公共文化": CONFIG_DIR / "public_culture_2025.json",
    "互联网销售": CONFIG_DIR / "internet_sales_2025.json",
}
STRATEGY_VERSION = "desktop_pagination_v1"

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def safe_stem(index: int, keyword: str) -> str:
    cleaned = "".join("_" if value in '<>:"/\\|?*' else value for value in keyword)
    return f"{index:02d}_{cleaned}"


def load_json(path: Path, default: dict | None = None) -> dict:
    if not path.exists():
        return {} if default is None else default.copy()
    return json.loads(path.read_text(encoding="utf-8"))


def save_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def load_keywords(path: Path) -> list[str]:
    values = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(values, list):
        raise RuntimeError(f"关键词配置必须是 JSON 数组：{path}")
    keywords = [" ".join(str(value).replace("+", " ").split()) for value in values if str(value).strip()]
    if not keywords or len(keywords) != len(set(keywords)):
        raise RuntimeError(f"关键词配置为空或含重复项：{path}")
    return keywords


def merge_keyword(source: Path, keyword: str, combined: Path) -> int:
    incoming = pd.read_csv(source)
    incoming["keyword"] = keyword
    if combined.exists():
        current = pd.read_csv(combined)
        current = current[current["keyword"].astype(str) != keyword]
        frame = pd.concat([current, incoming], ignore_index=True)
    else:
        frame = incoming
    if "result_id" in frame.columns:
        frame = frame.drop_duplicates(subset=["keyword", "result_id"], keep="first")
    combined.parent.mkdir(parents=True, exist_ok=True)
    temporary = combined.with_suffix(combined.suffix + ".tmp")
    frame.to_csv(temporary, index=False, encoding="utf-8-sig")
    temporary.replace(combined)
    return len(frame)


def run_category(category: str, config: Path, args: argparse.Namespace) -> bool:
    keywords = load_keywords(config)
    if args.max_keywords > 0:
        keywords = keywords[: args.max_keywords]
    data_dir = args.output_root / "数据"
    state_dir = args.output_root / "运行状态" / f"{category}_2025"
    log_dir = args.output_root / "日志" / f"{category}_2025"
    for directory in (data_dir, state_dir, log_dir):
        directory.mkdir(parents=True, exist_ok=True)

    task_name = f"今日头条—{category}"
    combined = data_dir / f"{task_name}.csv"
    progress_path = args.output_root / "运行状态" / f"{task_name}_采集进度.json"
    progress = {"completed": {}, "failed": {}, "strategy": STRATEGY_VERSION} if args.fresh else load_json(progress_path)
    strategy_changed = progress.get("strategy") != STRATEGY_VERSION
    if strategy_changed:
        progress = {"completed": {}, "failed": {}, "strategy": STRATEGY_VERSION}
        print(f"[{category}] 已切换为登录态桌面翻页策略，旧关键词将逐个重跑并替换")
    if args.fresh and combined.exists():
        combined.unlink()
    crawler = Path(__file__).resolve().parent / "toutiao_crawler.py"

    print(f"[{category}] 共 {len(keywords)} 个关键词，合并表：{combined}")
    for index, keyword in enumerate(keywords, start=1):
        if not args.fresh and keyword in progress.get("completed", {}):
            print(f"[{index}/{len(keywords)}] 跳过已完成：{keyword}")
            continue
        stem = safe_stem(index, keyword)
        output = state_dir / f"{stem}_result.csv"
        checkpoint = state_dir / f"{stem}_checkpoint.csv"
        log = log_dir / f"{stem}.log"
        command = [
            sys.executable,
            str(crawler),
            "--keyword", keyword,
            "--target-year", "2025",
            "--mode", "desktop",
            "--max-pages", str(args.max_pages),
            "--delay", str(args.delay),
            "--page-wait", str(args.page_wait),
            "--output", str(output),
            "--checkpoint", str(checkpoint),
            "--log", str(log),
        ]
        if args.headed:
            command.append("--headed")
        if args.fresh or strategy_changed:
            command.append("--fresh")
        print(f"[{index}/{len(keywords)}] 开始：{keyword}")
        result = subprocess.run(command, cwd=PROJECT_ROOT, check=False)
        if result.returncode == 0 and output.exists():
            rows = max(sum(1 for _ in output.open("r", encoding="utf-8-sig")) - 1, 0)
            combined_rows = merge_keyword(output, keyword, combined)
            progress.setdefault("completed", {})[keyword] = {
                "rows": rows,
                "merged_into": str(combined),
            }
            progress.setdefault("failed", {}).pop(keyword, None)
            output.unlink(missing_ok=True)
            checkpoint.unlink(missing_ok=True)
            print(f"[{index}/{len(keywords)}] 完成：{keyword}，{rows} 条；总表 {combined_rows} 条")
        else:
            progress.setdefault("failed", {})[keyword] = {
                "returncode": result.returncode,
                "log": str(log),
            }
            print(f"[{index}/{len(keywords)}] 暂停/失败：{keyword}，可稍后续跑")
        progress.update({
            "category": category,
            "year": 2025,
            "keyword_count": len(keywords),
            "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        })
        save_json(progress_path, progress)
    return not bool(progress.get("failed"))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--category", choices=[*CATEGORY_CONFIGS, "全部"], default="养老服务")
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--delay", type=float, default=2.0)
    parser.add_argument("--page-wait", type=float, default=5.0)
    parser.add_argument("--max-pages", type=int, default=10, help="每个关键词最多翻页数")
    parser.add_argument("--max-keywords", type=int, default=0, help="0 表示运行该类别的全部关键词")
    parser.add_argument("--headed", action="store_true")
    parser.add_argument("--fresh", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    selected = CATEGORY_CONFIGS if args.category == "全部" else {args.category: CATEGORY_CONFIGS[args.category]}
    successful = True
    for category, config in selected.items():
        successful = run_category(category, config, args) and successful
    return 0 if successful else 1


if __name__ == "__main__":
    raise SystemExit(main())

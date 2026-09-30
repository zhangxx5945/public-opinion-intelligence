"""Batch Tencent News public-search collection for four configured categories."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = Path(__file__).resolve().parent / "keywords"
DEFAULT_OUTPUT_ROOT = Path(os.getenv("CRAWLER_OUTPUT_ROOT", PROJECT_ROOT / "outputs")) / "tencent_news"
CATEGORY_CONFIGS = {
    "养老服务": CONFIG_DIR / "elderly_service_2025.json",
    "政务服务": CONFIG_DIR / "government_service_2025.json",
    "公共文化": CONFIG_DIR / "public_culture_2025.json",
    "互联网销售": CONFIG_DIR / "internet_sales_2025.json",
}

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def safe_stem(index: int, keyword: str) -> str:
    cleaned = "".join("_" if value in '<>:"/\\|?*' else value for value in keyword)
    return f"{index:02d}_{cleaned}"


def load_keywords(path: Path) -> list[str]:
    values = json.loads(path.read_text(encoding="utf-8"))
    keywords = [" ".join(str(value).replace("+", " ").split()) for value in values if str(value).strip()]
    if len(keywords) != 20 or len(keywords) != len(set(keywords)):
        raise RuntimeError(f"关键词配置必须包含20个不重复关键词：{path}")
    return keywords


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def save_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def merge_keyword(source: Path, keyword: str, combined: Path) -> int:
    incoming = pd.read_csv(source)
    incoming["keyword"] = keyword
    if combined.exists():
        current = pd.read_csv(combined)
        current = current[current["keyword"].astype(str) != keyword]
        frame = pd.concat([current, incoming], ignore_index=True)
    else:
        frame = incoming
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

    task_name = f"腾讯新闻—{category}"
    combined = data_dir / f"{task_name}.csv"
    progress_path = args.output_root / "运行状态" / f"{task_name}_采集进度.json"
    progress = {"completed": {}, "failed": {}} if args.fresh else load_json(progress_path)
    if args.fresh and combined.exists():
        combined.unlink()
    crawler = Path(__file__).resolve().parent / "tencent_news_crawler.py"

    print(f"[{category}] 共 {len(keywords)} 个关键词，唯一汇总表：{combined}")
    for index, keyword in enumerate(keywords, start=1):
        if not args.fresh and keyword in progress.get("completed", {}):
            print(f"[{index}/{len(keywords)}] 跳过已完成：{keyword}")
            continue
        stem = safe_stem(index, keyword)
        output = state_dir / f"{stem}_result.csv"
        checkpoint = state_dir / f"{stem}_checkpoint.csv"
        word_progress = state_dir / f"{stem}_progress.json"
        log = log_dir / f"{stem}.log"
        command = [
            sys.executable,
            str(crawler),
            "--keyword", keyword,
            "--target-year", "2025",
            "--max-pages", str(args.max_pages),
            "--delay", str(args.delay),
            "--timeout", str(args.timeout),
            "--output", str(output),
            "--checkpoint", str(checkpoint),
            "--progress", str(word_progress),
            "--log", str(log),
        ]
        if args.fresh:
            command.append("--fresh")
        print(f"[{index}/{len(keywords)}] 开始：{keyword}")
        rate_attempt = 0
        while True:
            result = subprocess.run(command, cwd=PROJECT_ROOT, check=False)
            if result.returncode != 3:
                break
            rate_attempt += 1
            if rate_attempt > args.rate_limit_retries:
                print(
                    f"[{index}/{len(keywords)}] 限流持续存在，已达到 "
                    f"{args.rate_limit_retries} 次冷却重试上限"
                )
                break
            print(
                f"[{index}/{len(keywords)}] 检测到限流，第 {rate_attempt}/"
                f"{args.rate_limit_retries} 次冷却 {args.rate_limit_cooldown:.0f} 秒后重试"
            )
            time.sleep(args.rate_limit_cooldown)
        if result.returncode == 0 and output.exists():
            rows = max(sum(1 for _ in output.open("r", encoding="utf-8-sig")) - 1, 0)
            combined_rows = merge_keyword(output, keyword, combined)
            progress.setdefault("completed", {})[keyword] = {"rows": rows, "merged_into": str(combined)}
            progress.setdefault("failed", {}).pop(keyword, None)
            output.unlink(missing_ok=True)
            checkpoint.unlink(missing_ok=True)
            print(f"[{index}/{len(keywords)}] 完成：{keyword}，{rows} 条；总表 {combined_rows} 条")
        else:
            progress.setdefault("failed", {})[keyword] = {"returncode": result.returncode, "log": str(log)}
            print(f"[{index}/{len(keywords)}] 暂停/失败：{keyword}，后续可续跑")
        progress.update(
            {
                "category": category,
                "year": 2025,
                "keyword_count": len(keywords),
                "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            }
        )
        save_json(progress_path, progress)
        if result.returncode == 3:
            print("腾讯搜索限流尚未解除，本次批处理停止，避免继续消耗其余关键词。")
            return False
        if args.keyword_delay and index < len(keywords):
            time.sleep(args.keyword_delay)
    return not bool(progress.get("failed"))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--category", choices=[*CATEGORY_CONFIGS, "全部"], default="养老服务")
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--max-pages", type=int, default=0, help="0表示读取平台报告的全部公开结果页")
    parser.add_argument("--max-keywords", type=int, default=0)
    parser.add_argument("--delay", type=float, default=2.0)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--keyword-delay", type=float, default=30.0, help="关键词之间的冷却秒数")
    parser.add_argument("--rate-limit-cooldown", type=float, default=900.0, help="检测到限流后的冷却秒数")
    parser.add_argument("--rate-limit-retries", type=int, default=4, help="限流冷却重试次数")
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

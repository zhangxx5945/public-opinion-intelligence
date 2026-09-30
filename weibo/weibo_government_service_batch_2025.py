"""批量采集一组关键词的2025年微博公开搜索结果。"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

import pandas as pd

try:
    from .weibo_result_filter import filter_combined_frame, write_filtered_csv
except ImportError:
    from weibo_result_filter import filter_combined_frame, write_filtered_csv

try:
    from .weibo_elderly_batch_2025 import (
        count_csv_rows,
        failed_date_count,
        read_progress,
        safe_stem,
        save_progress,
    )
except ImportError:  # 直接执行本文件时从脚本目录导入
    from weibo_elderly_batch_2025 import (
        count_csv_rows,
        failed_date_count,
        read_progress,
        safe_stem,
        save_progress,
    )


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT_ROOT = Path(os.getenv("CRAWLER_OUTPUT_ROOT", PROJECT_ROOT / "outputs")) / "weibo"
TASK_NAME = "微博—政务服务"
KEYWORDS = (
    "天津 政务服务",
    "天津 一网通办",
    "天津 不见面审批",
    "天津 政务大厅",
    "天津 便民服务中心",
    "天津 津心办",
    "天津 行政审批",
    "天津 12345热线",
    "天津 政民零距离",
    "天津 政务公开",
    "天津 政策解读",
    "天津 营商环境",
    "天津 企业开办",
    "天津 跨省通办",
    "天津 京津冀通办",
    "天津 政务预约办理",
    "天津 政务办事体验",
    "天津 政务办事难",
    "天津 政务服务态度",
    "天津 高效办成一件事",
)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--task-name", default=TASK_NAME, help="合并表和批次进度的任务名")
    parser.add_argument("--category", help="运行状态和日志的分类目录名")
    parser.add_argument(
        "--keywords-file",
        type=Path,
        help="UTF-8 JSON文件，内容为关键词字符串数组；不传时使用政务服务默认词",
    )
    parser.add_argument("--fresh", action="store_true", help="从头重新采集")
    return parser.parse_args()


def load_task_keywords(path: Path | None) -> tuple[str, ...]:
    values = KEYWORDS if path is None else json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(values, (list, tuple)):
        raise RuntimeError("关键词配置必须是JSON字符串数组")
    keywords = tuple(" ".join(str(value).split()) for value in values if str(value).strip())
    if not keywords:
        raise RuntimeError("关键词配置为空")
    if len(keywords) != len(set(keywords)):
        raise RuntimeError("关键词配置存在重复项")
    return keywords


def merge_keyword_into_combined(source: Path, keyword: str, combined_csv: Path) -> int:
    """将一个关键词结果幂等合并到唯一数据表。"""
    frame = pd.read_csv(source)
    if "keyword" in frame.columns:
        frame = frame.drop(columns=["keyword"])
    frame.insert(0, "keyword", keyword)

    if combined_csv.exists():
        combined = pd.read_csv(combined_csv)
        if "keyword" in combined.columns:
            combined = combined[combined["keyword"].astype(str) != keyword]
        combined = pd.concat([combined, frame], ignore_index=True)
    else:
        combined = frame

    filtered, _ = filter_combined_frame(combined, max_rows_per_keyword=1000)
    write_filtered_csv(filtered, combined_csv)
    return len(filtered)


def main() -> int:
    args = parse_args()
    keywords = load_task_keywords(args.keywords_file)
    task_name = " ".join(args.task_name.split())
    category = " ".join((args.category or task_name.removeprefix("微博—")).split())
    state_dir = args.output_root / "运行状态" / f"{category}_2025"
    log_dir = args.output_root / "日志" / f"{category}_2025"
    for directory in (state_dir, log_dir):
        directory.mkdir(parents=True, exist_ok=True)

    batch_progress_path = args.output_root / "运行状态" / f"{task_name}_采集进度.json"
    combined_csv = args.output_root / "数据" / f"{task_name}.csv"
    progress = {"completed": {}, "failed": {}} if args.fresh else read_progress(batch_progress_path)
    if args.fresh and combined_csv.exists():
        combined_csv.unlink()
    crawler = Path(__file__).resolve().parent / "weibo_crawler.py"

    print(f"共 {len(keywords)} 个关键词，唯一合并数据表：{combined_csv}")
    for index, keyword in enumerate(keywords, start=1):
        stem = safe_stem(index, keyword)
        output = state_dir / f"{stem}_result.csv"
        checkpoint = state_dir / f"{stem}_checkpoint.csv"
        rejected = state_dir / f"{stem}_rejected.csv"
        window_progress = state_dir / f"{stem}_weekly_progress.json"
        quality = state_dir / f"{stem}_quality.json"
        log = log_dir / f"{stem}.log"

        if not args.fresh and keyword in progress.get("completed", {}):
            print(f"[{index}/{len(keywords)}] 跳过已完成：{keyword}")
            continue

        print(f"[{index}/{len(keywords)}] 开始：{keyword}")
        command = [
            sys.executable,
            str(crawler),
            "--keyword", keyword,
            "--start-date", "2025-01-01",
            "--end-date", "2025-12-31",
            "--delay", "0.8",
            "--date-delay", "0.4",
            "--page-wait", "1.2",
            "--initial-wait", "2.5",
            "--timeout", "15",
            "--page-retries", "1",
            "--retry-delay", "0",
            "--max-pages-per-day", "0",
            "--window-days", "7",
            "--accept-all-search-results",
            "--output", str(output),
            "--checkpoint", str(checkpoint),
            "--rejected", str(rejected),
            "--progress", str(window_progress),
            "--quality", str(quality),
            "--log", str(log),
        ]
        if args.fresh:
            command.append("--fresh")

        result = subprocess.run(command, cwd=PROJECT_ROOT, check=False)
        remaining_failures = failed_date_count(window_progress)
        if result.returncode == 0 and output.exists():
            rows = count_csv_rows(output)
            progress.setdefault("completed", {})[keyword] = {
                "search_keyword": keyword,
                "rows": rows,
                "merged_into": str(combined_csv),
                "remaining_failed_windows": remaining_failures,
            }
            progress.setdefault("failed", {}).pop(keyword, None)
            combined_rows = merge_keyword_into_combined(output, keyword, combined_csv)
            # 先持久化批次进度，再删除分词中间数据，保证异常中断可恢复。
            save_progress(batch_progress_path, progress, len(keywords))
            for intermediate in (output, checkpoint, rejected):
                intermediate.unlink(missing_ok=True)
            print(
                f"[{index}/{len(keywords)}] 完成：{keyword}，{rows} 条；"
                f"合并单表 {combined_rows} 条"
            )
        else:
            progress.setdefault("failed", {})[keyword] = {
                "search_keyword": keyword,
                "returncode": result.returncode,
            }
            print(f"[{index}/{len(keywords)}] 暂停/失败：{keyword}，后续可续跑")
        save_progress(batch_progress_path, progress, len(keywords))

    save_progress(batch_progress_path, progress, len(keywords))
    combined_rows = count_csv_rows(combined_csv)
    summary = {
        "task": task_name,
        "year": 2025,
        "keyword_count": len(keywords),
        "completed": len(progress.get("completed", {})),
        "failed": len(progress.get("failed", {})),
        "combined_rows": combined_rows,
        "combined_csv": str(combined_csv),
        "progress": str(batch_progress_path),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 1 if progress.get("failed") else 0


if __name__ == "__main__":
    raise SystemExit(main())

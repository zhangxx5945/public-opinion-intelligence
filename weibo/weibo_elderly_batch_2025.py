"""批量采集关键词表第三列的微博2025年公开搜索结果，支持断点续跑。"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_KEYWORD_BOOK = PROJECT_ROOT / "13.舆情关键词整理2026.80.20.xlsx"
DEFAULT_OUTPUT_ROOT = Path(os.getenv("CRAWLER_OUTPUT_ROOT", PROJECT_ROOT / "outputs")) / "weibo"

# 从原表29个词中保留覆盖政策/产业、机构、居家社区、医护、福利救助、
# 餐饮陪护和适老化的20个代表性主题，去掉高度重复的近义词。
REPRESENTATIVE_20 = (
    "天津+智慧养老",
    "天津+养老服务",
    "天津+养老产业",
    "天津+养老院",
    "天津+养老+机构",
    "天津+居家养老",
    "天津+社区+养老",
    "天津+老龄化+服务",
    "天津+养老+护理",
    "天津+养老+活动",
    "天津+老年人+福利",
    "天津+老年人+救助",
    "天津+老年人+医疗",
    "天津+老年人+康复",
    "天津+老年人+临终关怀",
    "天津+老年人+无障碍设施",
    "天津+老人陪护",
    "天津+老年+食堂",
    "天津+老年+助餐",
    "天津+适老化",
)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def load_keywords(path: Path) -> list[tuple[str, str]]:
    frame = pd.read_excel(path, sheet_name=0, header=None)
    values = [str(value).strip() for value in frame.iloc[:, 2].dropna().tolist()]
    keywords: list[tuple[str, str]] = []
    seen: set[str] = set()
    for value in values:
        if not value or value == "养老服务":
            continue
        search_keyword = " ".join(value.replace("+", " ").split())
        if search_keyword in seen:
            continue
        seen.add(search_keyword)
        keywords.append((value, search_keyword))
    return keywords


def safe_stem(index: int, value: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*]+', "_", value).strip(" .")
    return f"{index:02d}_{cleaned[:70]}"


def read_progress(path: Path) -> dict:
    if not path.exists():
        return {"completed": {}, "failed": {}}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"completed": {}, "failed": {}}


def save_progress(path: Path, progress: dict, keyword_count: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    progress.update(
        {
            "year": 2025,
            "keyword_count": keyword_count,
            "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        }
    )
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(progress, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def count_csv_rows(path: Path) -> int:
    if not path.exists():
        return 0
    return max(sum(1 for _ in path.open("r", encoding="utf-8-sig")) - 1, 0)


def failed_date_count(path: Path) -> int:
    if not path.exists():
        return 0
    try:
        progress = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return 0
    return len(progress.get("failed_dates", {}))


def rebuild_combined_csv(progress: dict, output: Path) -> int:
    frames: list[pd.DataFrame] = []
    for display_keyword, item in progress.get("completed", {}).items():
        source = Path(str(item.get("csv") or ""))
        if not source.exists():
            continue
        frame = pd.read_csv(source)
        if "keyword" in frame.columns:
            frame = frame.drop(columns=["keyword"])
        frame.insert(0, "keyword", display_keyword)
        frames.append(frame)
    if not frames:
        return 0
    combined = pd.concat(frames, ignore_index=True)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    combined.to_csv(temporary, index=False, encoding="utf-8-sig")
    temporary.replace(output)
    return len(combined)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--keywords-book", type=Path, default=DEFAULT_KEYWORD_BOOK)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--fresh", action="store_true", help="从头重新采集")
    parser.add_argument(
        "--all-keywords",
        action="store_true",
        help="使用原表全部29个词；默认使用20个代表词",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    all_keywords = load_keywords(args.keywords_book)
    if args.all_keywords:
        keywords = all_keywords
    else:
        selected = set(REPRESENTATIVE_20)
        keywords = [item for item in all_keywords if item[0] in selected]
        missing = selected.difference(display for display, _ in keywords)
        if missing:
            raise RuntimeError(f"关键词表缺少预设代表词：{sorted(missing)}")
    raw_dir = args.output_root / "数据" / "原始数据" / "养老_2025"
    state_dir = args.output_root / "运行状态" / "养老_2025"
    log_dir = args.output_root / "日志" / "养老_2025"
    for directory in (raw_dir, state_dir, log_dir):
        directory.mkdir(parents=True, exist_ok=True)

    batch_progress_path = args.output_root / "运行状态" / "微博-养老_采集进度.json"
    combined_csv = args.output_root / "数据" / "微博-养老_全部数据.csv"
    progress = {"completed": {}, "failed": {}} if args.fresh else read_progress(batch_progress_path)
    crawler = Path(__file__).resolve().parent / "weibo_crawler.py"

    print(f"共 {len(keywords)} 个关键词，原始数据目录：{raw_dir}")
    for index, (display_keyword, search_keyword) in enumerate(keywords, start=1):
        stem = safe_stem(index, display_keyword)
        output = raw_dir / f"{stem}.csv"
        checkpoint = state_dir / f"{stem}_checkpoint.csv"
        rejected = state_dir / f"{stem}_rejected.csv"
        # 周分片与旧的逐日进度分开，但共用 checkpoint，因此旧数据会保留且不重复。
        day_progress = state_dir / f"{stem}_weekly_progress.json"
        quality = state_dir / f"{stem}_quality.json"
        log = log_dir / f"{stem}.log"

        if not args.fresh and display_keyword in progress.get("completed", {}) and output.exists():
            print(f"[{index}/{len(keywords)}] 跳过已完成：{display_keyword}")
            continue

        print(f"[{index}/{len(keywords)}] 开始：{display_keyword} -> {search_keyword}")
        command = [
            sys.executable,
            str(crawler),
            "--keyword", search_keyword,
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
            "--progress", str(day_progress),
            "--quality", str(quality),
            "--log", str(log),
        ]
        if args.fresh:
            command.append("--fresh")

        result = subprocess.run(command, cwd=PROJECT_ROOT, check=False)
        remaining_failures = failed_date_count(day_progress)
        if result.returncode == 0 and output.exists():
            rows = count_csv_rows(output)
            progress.setdefault("completed", {})[display_keyword] = {
                "search_keyword": search_keyword,
                "rows": rows,
                "csv": str(output),
                "remaining_failed_dates": remaining_failures,
            }
            progress.setdefault("failed", {}).pop(display_keyword, None)
            print(f"[{index}/{len(keywords)}] 完成：{display_keyword}，{rows} 条")
            combined_rows = rebuild_combined_csv(progress, combined_csv)
            print(f"[{index}/{len(keywords)}] 单表CSV已更新：{combined_rows} 条")
        else:
            progress.setdefault("failed", {})[display_keyword] = {
                "search_keyword": search_keyword,
                "returncode": result.returncode,
            }
            print(f"[{index}/{len(keywords)}] 暂停/失败：{display_keyword}，后续可续跑")
        save_progress(batch_progress_path, progress, len(keywords))

    save_progress(batch_progress_path, progress, len(keywords))
    combined_rows = rebuild_combined_csv(progress, combined_csv)
    summary = {
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

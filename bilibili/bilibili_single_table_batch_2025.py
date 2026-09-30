"""按JSON关键词列表批量采集B站2025年公开视频，合并到唯一CSV。"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

try:
    from .bilibili_elderly_batch_2025 import query_variants, safe_stem
except ImportError:  # 直接执行脚本时从同目录导入
    from bilibili_elderly_batch_2025 import query_variants, safe_stem


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT_ROOT = Path(os.getenv("CRAWLER_OUTPUT_ROOT", PROJECT_ROOT / "outputs")) / "bilibili"

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def read_progress(path: Path) -> dict:
    if not path.exists():
        return {"completed": {}, "failed": {}}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"completed": {}, "failed": {}}


def write_progress(path: Path, progress: dict, keyword_count: int) -> None:
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


def load_keywords(path: Path) -> tuple[str, ...]:
    values = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(values, list):
        raise RuntimeError("关键词配置必须是JSON字符串数组")
    keywords = tuple(" ".join(str(value).split()) for value in values if str(value).strip())
    if not keywords or len(keywords) != len(set(keywords)):
        raise RuntimeError("关键词配置为空或存在重复项")
    return keywords


def count_csv_rows(path: Path) -> int:
    if not path.exists():
        return 0
    return max(sum(1 for _ in path.open("r", encoding="utf-8-sig")) - 1, 0)


def merge_keyword_into_combined(source: Path, keyword: str, combined_csv: Path) -> int:
    frame = pd.read_csv(source)
    for column in ("keyword", "sheet_keyword"):
        if column in frame.columns:
            frame = frame.drop(columns=[column])
    frame.insert(0, "keyword", keyword)

    if combined_csv.exists():
        combined = pd.read_csv(combined_csv)
        if "keyword" in combined.columns:
            combined = combined[combined["keyword"].astype(str) != keyword]
        combined = pd.concat([combined, frame], ignore_index=True)
    else:
        combined = frame

    combined_csv.parent.mkdir(parents=True, exist_ok=True)
    temporary = combined_csv.with_suffix(combined_csv.suffix + ".tmp")
    combined.to_csv(temporary, index=False, encoding="utf-8-sig")
    temporary.replace(combined_csv)
    return len(combined)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--keywords-file", type=Path, required=True)
    parser.add_argument("--task-name", required=True)
    parser.add_argument("--category", required=True)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--fresh", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    keywords = load_keywords(args.keywords_file)
    task_name = " ".join(args.task_name.split())
    category = " ".join(args.category.split())
    state_dir = args.output_root / "运行状态" / f"{category}_2025"
    log_dir = args.output_root / "日志" / f"{category}_2025"
    state_dir.mkdir(parents=True, exist_ok=True)
    log_dir.mkdir(parents=True, exist_ok=True)

    progress_path = args.output_root / "运行状态" / f"{task_name}_采集进度.json"
    combined_csv = args.output_root / "数据" / f"{task_name}.csv"
    progress = {"completed": {}, "failed": {}} if args.fresh else read_progress(progress_path)
    if args.fresh and combined_csv.exists():
        combined_csv.unlink()
    crawler = Path(__file__).resolve().parent / "bilibili_crawler.py"

    print(f"共 {len(keywords)} 个关键词，唯一合并数据表：{combined_csv}")
    for index, keyword in enumerate(keywords, start=1):
        if not args.fresh and keyword in progress.get("completed", {}):
            print(f"[{index}/{len(keywords)}] 跳过已完成：{keyword}")
            continue

        stem = safe_stem(index, keyword)
        result_file = state_dir / f"{stem}_result.csv"
        query_files: list[Path] = []
        frames: list[pd.DataFrame] = []
        query_failures: list[str] = []
        queries = query_variants(keyword)
        print(f"[{index}/{len(keywords)}] 开始：{keyword} -> {queries}")

        for query_index, query in enumerate(queries, start=1):
            query_output = state_dir / f"{stem}_q{query_index}.csv"
            checkpoint = state_dir / f"{stem}_q{query_index}_checkpoint.csv"
            log = log_dir / f"{stem}_q{query_index}.log"
            query_files.extend((query_output, checkpoint))
            command = [
                sys.executable,
                str(crawler),
                "--keyword", query,
                "--start-date", "2025-01-01",
                "--end-date", "2025-12-31",
                "--target-year", "2025",
                "--max-results", "0",
                "--max-pages", "0",
                "--page-delay", "1",
                "--delay", "0.3",
                "--csv-only",
                "--output", str(query_output),
                "--checkpoint", str(checkpoint),
                "--log", str(log),
            ]
            completed = subprocess.run(command, cwd=PROJECT_ROOT, check=False)
            if completed.returncode == 0 and query_output.exists():
                frame = pd.read_csv(query_output)
                frame["source_query"] = query
                frames.append(frame)
            else:
                query_failures.append(query)

        if frames:
            merged = pd.concat(frames, ignore_index=True)
            if "video_id" in merged.columns:
                merged = merged.drop_duplicates(subset=["video_id"], keep="first")
            merged.to_csv(result_file, index=False, encoding="utf-8-sig")
            rows = len(merged)
            combined_rows = merge_keyword_into_combined(result_file, keyword, combined_csv)
            progress.setdefault("completed", {})[keyword] = {
                "rows": rows,
                "queries": queries,
                "query_failures": query_failures,
                "merged_into": str(combined_csv),
            }
            progress.setdefault("failed", {}).pop(keyword, None)
            write_progress(progress_path, progress, len(keywords))
            for intermediate in (result_file, *query_files):
                intermediate.unlink(missing_ok=True)
            print(
                f"[{index}/{len(keywords)}] 完成：{keyword}，{rows} 条；"
                f"合并单表 {combined_rows} 条"
            )
        else:
            progress.setdefault("failed", {})[keyword] = {
                "queries": queries,
                "query_failures": query_failures,
            }
            print(f"[{index}/{len(keywords)}] 失败：{keyword}，继续下一个")
        write_progress(progress_path, progress, len(keywords))

    write_progress(progress_path, progress, len(keywords))
    summary = {
        "task": task_name,
        "year": 2025,
        "keyword_count": len(keywords),
        "completed": len(progress.get("completed", {})),
        "failed": len(progress.get("failed", {})),
        "combined_rows": count_csv_rows(combined_csv),
        "combined_csv": str(combined_csv),
        "progress": str(progress_path),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 1 if progress.get("failed") else 0


if __name__ == "__main__":
    raise SystemExit(main())

"""批量采集关键词表第三列的 B站2025养老类公开视频，保存可续跑中间结果。"""

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
DEFAULT_OUTPUT_ROOT = Path(os.getenv("CRAWLER_OUTPUT_ROOT", PROJECT_ROOT / "outputs")) / "bilibili"

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def query_variants(value: str) -> list[str]:
    normalized = " ".join(value.replace("+", " ").split())
    tokens = normalized.split()
    candidates = [normalized]
    if len(tokens) > 2 and tokens[0] == "天津":
        candidates.append(f"天津 {''.join(tokens[1:])}")
    return list(dict.fromkeys(candidates))


def load_keywords(path: Path) -> list[dict[str, object]]:
    frame = pd.read_excel(path, sheet_name=0, header=None)
    values = [str(value).strip() for value in frame.iloc[:, 2].dropna().tolist()]
    keywords: list[dict[str, str]] = []
    seen: set[str] = set()
    for value in values:
        if not value or value == "养老服务":
            continue
        search_keyword = " ".join(value.replace("+", " ").split())
        if search_keyword in seen:
            continue
        seen.add(search_keyword)
        keywords.append(
            {
                "sheet_name": value,
                "search_keyword": search_keyword,
                "queries": query_variants(value),
            }
        )
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


def write_progress(path: Path, progress: dict, keywords: list[dict[str, object]]) -> None:
    progress.update(
        {
            "year": 2025,
            "keyword_count": len(keywords),
            "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        }
    )
    path.write_text(json.dumps(progress, ensure_ascii=False, indent=2), encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--keywords-book", type=Path, default=DEFAULT_KEYWORD_BOOK)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--fresh", action="store_true", help="忽略已完成关键词并重新运行")
    parser.add_argument(
        "--augment-existing",
        action="store_true",
        help="复用已有主查询CSV，仅补跑额外查询词形",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    keywords = load_keywords(args.keywords_book)
    data_dir = args.output_root / "数据" / "养老_2025_中间数据"
    log_dir = args.output_root / "日志" / "养老_2025"
    data_dir.mkdir(parents=True, exist_ok=True)
    log_dir.mkdir(parents=True, exist_ok=True)
    progress_path = args.output_root / "数据" / "b站—养老_采集进度.json"
    progress = {"completed": {}, "failed": {}} if args.fresh else read_progress(progress_path)

    print(f"共 {len(keywords)} 个关键词，输出目录：{data_dir}")
    crawler = Path(__file__).resolve().parent / "bilibili_crawler.py"
    for index, item in enumerate(keywords, start=1):
        sheet_name = item["sheet_name"]
        search_keyword = str(item["search_keyword"])
        queries = [str(query) for query in item["queries"]]
        stem = safe_stem(index, sheet_name)
        output = data_dir / f"{stem}.csv"
        query_dir = data_dir / "查询明细"
        query_dir.mkdir(parents=True, exist_ok=True)
        if not args.fresh and sheet_name in progress.get("completed", {}) and output.exists():
            print(f"[{index}/{len(keywords)}] 跳过已完成：{sheet_name}")
            continue

        print(f"[{index}/{len(keywords)}] 开始：{sheet_name} -> {queries}")
        frames: list[pd.DataFrame] = []
        query_failures: list[str] = []
        queries_to_run = queries
        query_start_index = 1
        if args.augment_existing and output.exists():
            existing = pd.read_csv(output)
            if "source_query" not in existing.columns:
                existing["source_query"] = queries[0]
            existing["sheet_keyword"] = sheet_name
            frames.append(existing)
            queries_to_run = queries[1:]
            query_start_index = 2
        for query_index, query in enumerate(queries_to_run, start=query_start_index):
            query_output = query_dir / f"{stem}_q{query_index}.csv"
            checkpoint = query_dir / f"{stem}_q{query_index}_checkpoint.csv"
            log = log_dir / f"{stem}_q{query_index}.log"
            command = [
                sys.executable,
                str(crawler),
                "--keyword",
                query,
                "--start-date",
                "2025-01-01",
                "--end-date",
                "2025-12-31",
                "--target-year",
                "2025",
                "--max-results",
                "0",
                "--max-pages",
                "0",
                "--page-delay",
                "1",
                "--delay",
                "0.3",
                "--csv-only",
                "--output",
                str(query_output),
                "--checkpoint",
                str(checkpoint),
                "--log",
                str(log),
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
            merged["sheet_keyword"] = sheet_name
            merged.to_csv(output, index=False, encoding="utf-8-sig")
            row_count = len(merged)
            progress.setdefault("completed", {})[sheet_name] = {
                "search_keyword": search_keyword,
                "queries": queries,
                "rows": row_count,
                "csv": str(output),
                "query_failures": query_failures,
            }
            progress.setdefault("failed", {}).pop(sheet_name, None)
            print(f"[{index}/{len(keywords)}] 完成：{sheet_name}，{row_count} 条")
        else:
            progress.setdefault("failed", {})[sheet_name] = {
                "search_keyword": search_keyword,
                "queries": queries,
                "query_failures": query_failures,
            }
            print(f"[{index}/{len(keywords)}] 失败：{sheet_name}，继续下一个")
        write_progress(progress_path, progress, keywords)

    write_progress(progress_path, progress, keywords)
    failed_count = len(progress.get("failed", {}))
    print(
        json.dumps(
            {
                "keyword_count": len(keywords),
                "completed": len(progress.get("completed", {})),
                "failed": failed_count,
                "progress": str(progress_path),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 1 if failed_count else 0


if __name__ == "__main__":
    raise SystemExit(main())

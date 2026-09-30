"""统计多个关键词在指定日期范围内的微博公开搜索条目出现次数，不保存正文且不去重。"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

try:
    from .weibo_crawler import (
        PROJECT_DIR,
        build_search_url,
        configure_logging,
        create_session,
        has_next_page,
        iter_dates,
        normalize_published_at,
        parse_date,
        write_progress,
    )
    from .weibo_keyword_test import blocked_reason, clean_text
except ImportError:
    from weibo_crawler import (
        PROJECT_DIR,
        build_search_url,
        configure_logging,
        create_session,
        has_next_page,
        iter_dates,
        normalize_published_at,
        parse_date,
        write_progress,
    )
    from weibo_keyword_test import blocked_reason, clean_text


DATA_DIR = PROJECT_DIR / "data"
LOG_DIR = PROJECT_DIR / "logs"
SUMMARY_COLUMNS = (
    "platform",
    "keyword",
    "search_occurrences",
    "completed_dates",
    "expected_dates",
    "failed_dates",
    "coverage_rate",
    "raw_cards_seen",
    "excluded_wrong_date_cards",
    "status",
)
DAILY_COLUMNS = (
    "platform",
    "keyword",
    "date",
    "search_occurrences",
    "pages_visited",
    "raw_cards_seen",
    "excluded_wrong_date_cards",
)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def read_keywords(path: Path) -> list[str]:
    values: list[str] = []
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        keyword = line.strip().replace("+", " ")
        keyword = " ".join(keyword.split())
        if keyword and not keyword.startswith("#") and keyword not in values:
            values.append(keyword)
    if not values:
        raise RuntimeError(f"关键词文件为空：{path}")
    return values


def load_progress(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def write_csv(path: Path, columns: tuple[str, ...], rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=columns)
        writer.writeheader()
        writer.writerows({column: row.get(column, "") for column in columns} for row in rows)
    temporary.replace(path)


def card_published_at(card: Any, window_date: date) -> str:
    links = card.css("div.from a")
    if not links:
        return ""
    raw = clean_text(links[0].attrib.get("title")) or clean_text(links[0].text)
    return normalize_published_at(raw, window_date)


def expected_date_count(start: date, end: date) -> int:
    return (end - start).days + 1


def build_output_rows(
    progress: dict[str, Any], keywords: list[str], start: date, end: date
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    daily_counts = progress.get("daily_counts", {})
    completed = progress.get("completed_dates", {})
    failed = progress.get("failed_dates", {})
    expected = expected_date_count(start, end)
    summary_rows: list[dict[str, Any]] = []
    daily_rows: list[dict[str, Any]] = []

    for keyword in keywords:
        keyword_daily = daily_counts.get(keyword, {})
        completed_count = len(completed.get(keyword, []))
        failed_count = len(failed.get(keyword, {}))
        occurrences = sum(int(value.get("count", 0)) for value in keyword_daily.values())
        raw_cards = sum(int(value.get("raw_cards", 0)) for value in keyword_daily.values())
        wrong_date = sum(int(value.get("wrong_date", 0)) for value in keyword_daily.values())
        summary_rows.append(
            {
                "platform": "微博",
                "keyword": keyword,
                "search_occurrences": occurrences,
                "completed_dates": completed_count,
                "expected_dates": expected,
                "failed_dates": failed_count,
                "coverage_rate": round(completed_count / expected, 4),
                "raw_cards_seen": raw_cards,
                "excluded_wrong_date_cards": wrong_date,
                "status": "complete" if completed_count == expected else "partial",
            }
        )
        for day, value in sorted(keyword_daily.items()):
            daily_rows.append(
                {
                    "platform": "微博",
                    "keyword": keyword,
                    "date": day,
                    "search_occurrences": value.get("count", 0),
                    "pages_visited": value.get("pages", 0),
                    "raw_cards_seen": value.get("raw_cards", 0),
                    "excluded_wrong_date_cards": value.get("wrong_date", 0),
                }
            )
    return summary_rows, daily_rows


def save_state(args: argparse.Namespace, progress: dict[str, Any], keywords: list[str]) -> None:
    progress["updated_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
    write_progress(args.progress, progress)
    summary, daily = build_output_rows(progress, keywords, args.start_date, args.end_date)
    write_csv(args.output, SUMMARY_COLUMNS, summary)
    write_csv(args.daily_output, DAILY_COLUMNS, daily)


def validate_or_initialize(
    args: argparse.Namespace, keywords: list[str]
) -> dict[str, Any]:
    progress = {} if args.fresh else load_progress(args.progress)
    expected = {
        "platform": "微博",
        "keywords": keywords,
        "start_date": args.start_date.isoformat(),
        "end_date": args.end_date.isoformat(),
        "deduplicate": False,
    }
    for key, value in expected.items():
        if key in progress and progress[key] != value:
            raise RuntimeError(f"进度文件参数不一致：{key}={progress[key]!r}，当前={value!r}")
    progress.update(expected)
    progress.setdefault("daily_counts", {})
    progress.setdefault("completed_dates", {})
    progress.setdefault("failed_dates", {})
    for keyword in keywords:
        progress["daily_counts"].setdefault(keyword, {})
        progress["completed_dates"].setdefault(keyword, [])
        progress["failed_dates"].setdefault(keyword, {})
    return progress


def count_keyword(
    args: argparse.Namespace,
    logger: logging.Logger,
    session: Any,
    progress: dict[str, Any],
    keyword: str,
    first_request: bool,
) -> bool:
    completed = set(progress["completed_dates"][keyword])
    for window_date in iter_dates(args.start_date, args.end_date, args.newest_first):
        day = window_date.isoformat()
        if day in completed:
            continue
        logger.info("关键词=%s 日期=%s 开始", keyword, day)
        page_number = 1
        day_count = 0
        raw_cards = 0
        wrong_date = 0
        day_complete = True

        while True:
            if args.max_pages_per_day and page_number > args.max_pages_per_day:
                progress["failed_dates"][keyword][day] = {
                    "reason": "page_limit",
                    "page_number": page_number,
                }
                day_complete = False
                break
            url = build_search_url(keyword, window_date, page_number)
            wait_seconds = args.initial_wait if first_request else args.page_wait
            first_request = False
            response = None
            for attempt in range(1, args.page_retries + 1):
                try:
                    response = session.fetch(url, wait=wait_seconds * 1_000)
                    break
                except PlaywrightTimeoutError as exc:
                    logger.warning(
                        "关键词=%s 日期=%s 页=%s 超时 %s/%s：%s",
                        keyword,
                        day,
                        page_number,
                        attempt,
                        args.page_retries,
                        exc,
                    )
                    if attempt < args.page_retries and args.retry_delay:
                        time.sleep(args.retry_delay)
            if response is None:
                progress["failed_dates"][keyword][day] = {
                    "reason": "page_timeout",
                    "page_number": page_number,
                    "attempts": args.page_retries,
                }
                day_complete = False
                break
            if int(response.status) >= 400:
                progress["failed_dates"][keyword][day] = {
                    "reason": f"http_{response.status}",
                    "page_number": page_number,
                }
                day_complete = False
                break

            cards = response.css('div.card-wrap[action-type="feed_list_item"]')
            if not cards:
                reason = blocked_reason(response)
                if reason:
                    raise RuntimeError(f"搜索被停止：{reason}")
                break

            page_valid = 0
            for card in cards:
                raw_cards += 1
                published_at = card_published_at(card, window_date)
                if published_at.startswith(day):
                    page_valid += 1
                else:
                    wrong_date += 1
            day_count += page_valid
            logger.info(
                "关键词=%s 日期=%s 页=%s 本页=%s 累计=%s 原始卡片=%s",
                keyword,
                day,
                page_number,
                page_valid,
                day_count,
                raw_cards,
            )
            if not has_next_page(response):
                break
            page_number += 1
            if args.delay:
                time.sleep(args.delay)

        if day_complete:
            progress["daily_counts"][keyword][day] = {
                "count": day_count,
                "pages": page_number,
                "raw_cards": raw_cards,
                "wrong_date": wrong_date,
            }
            completed.add(day)
            progress["completed_dates"][keyword] = sorted(completed)
            progress["failed_dates"][keyword].pop(day, None)
        save_state(args, progress, args.keywords)
        if args.date_delay:
            time.sleep(args.date_delay)
    return first_request


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--keywords-file", type=Path, default=PROJECT_DIR / "keywords_2025.txt")
    parser.add_argument("--start-date", type=parse_date, default=date(2025, 1, 1))
    parser.add_argument("--end-date", type=parse_date, default=date(2025, 12, 31))
    parser.add_argument("--newest-first", action="store_true")
    parser.add_argument("--headed", action="store_true")
    parser.add_argument("--fresh", action="store_true")
    parser.add_argument("--profile-dir", type=Path, default=PROJECT_DIR / ".weibo_profile")
    parser.add_argument("--page-wait", type=float, default=1.5)
    parser.add_argument("--initial-wait", type=float, default=4.0)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--page-retries", type=int, default=2)
    parser.add_argument("--retry-delay", type=float, default=2.0)
    parser.add_argument("--delay", type=float, default=1.0)
    parser.add_argument("--date-delay", type=float, default=0.3)
    parser.add_argument("--max-pages-per-day", type=int, default=0)
    parser.add_argument("--output", type=Path, default=DATA_DIR / "weibo_keyword_counts_2025.csv")
    parser.add_argument(
        "--daily-output", type=Path, default=DATA_DIR / "weibo_keyword_daily_counts_2025.csv"
    )
    parser.add_argument("--progress", type=Path, default=DATA_DIR / "weibo_keyword_counts_2025_progress.json")
    parser.add_argument("--log", type=Path, default=LOG_DIR / "weibo_keyword_counts_2025.log")
    args = parser.parse_args()
    if args.start_date > args.end_date:
        parser.error("开始日期不能晚于结束日期")
    if args.page_retries < 1:
        parser.error("page-retries 必须大于0")
    args.keywords = read_keywords(args.keywords_file)
    return args


def main() -> int:
    args = parse_args()
    logger = configure_logging(args.log)
    try:
        progress = validate_or_initialize(args, args.keywords)
        save_state(args, progress, args.keywords)
        first_request = True
        with create_session(args) as session:
            for keyword in args.keywords:
                first_request = count_keyword(
                    args, logger, session, progress, keyword, first_request
                )
        save_state(args, progress, args.keywords)
    except Exception as exc:
        logger.exception("计数任务停止")
        print(f"运行失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    logger.info("全部关键词计数结束，汇总=%s", args.output.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

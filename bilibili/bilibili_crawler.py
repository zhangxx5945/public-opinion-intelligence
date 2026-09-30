"""按关键词采集 B站公开视频，提取详情并按日期范围导出。"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pandas as pd
from dateutil import parser as date_parser
from playwright.sync_api import Page, sync_playwright

from bilibili_keyword_test import crawl as crawl_search


COLUMNS = [
    "keyword",
    "search_rank",
    "video_id",
    "title",
    "author",
    "published_at",
    "duration",
    "views",
    "likes",
    "comments",
    "in_date_range",
    "url",
    "http_status",
    "crawled_at",
    "status",
    "error",
]

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def configure_logging(path: Path) -> logging.Logger:
    logger = logging.getLogger("bilibili_crawler")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    formatter = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s")
    for handler in (
        logging.FileHandler(path, mode="w", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ):
        handler.setFormatter(formatter)
        logger.addHandler(handler)
    return logger


def meta_content(page: Page, selector: str) -> str | None:
    element = page.locator(selector).first
    if element.count() == 0:
        return None
    value = element.get_attribute("content")
    return value.strip() if value else None


def parse_json_ld(page: Page) -> dict[str, Any]:
    scripts = page.locator('script[type="application/ld+json"]')
    for index in range(scripts.count()):
        raw = scripts.nth(index).text_content()
        if not raw:
            continue
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            continue
        values = value if isinstance(value, list) else [value]
        for item in values:
            if isinstance(item, dict) and item.get("@type") == "VideoObject":
                return item
    return {}


def interaction_counts(json_ld: dict[str, Any]) -> dict[str, int | None]:
    result = {"views": None, "likes": None, "comments": None}
    mapping = {"WatchAction": "views", "LikeAction": "likes", "CommentAction": "comments"}
    statistics = json_ld.get("interactionStatistic", [])
    if isinstance(statistics, dict):
        statistics = [statistics]
    for statistic in statistics:
        if not isinstance(statistic, dict):
            continue
        interaction_type = statistic.get("interactionType", {})
        type_name = interaction_type.get("@type") if isinstance(interaction_type, dict) else None
        field = mapping.get(type_name)
        if field:
            value = statistic.get("userInteractionCount")
            try:
                result[field] = int(value)
            except (TypeError, ValueError):
                result[field] = None
    return result


def published_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date_parser.parse(value).date()
    except (TypeError, ValueError, OverflowError):
        return None


def clean_bilibili_title(title: str | None) -> str | None:
    if not title:
        return None
    title = re.sub(r"\s+", " ", title).strip()
    return re.sub(r"_哔哩哔哩_bilibili$", "", title).strip() or None


def extract_detail(
    page: Page,
    candidate: dict[str, Any],
    start_date: date,
    end_date: date,
) -> dict[str, Any]:
    response = page.goto(candidate["url"], wait_until="domcontentloaded", timeout=30_000)
    page.wait_for_timeout(3_000)
    status_code = response.status if response else None
    if status_code and status_code >= 400:
        raise RuntimeError(f"HTTP {status_code}")

    json_ld = parse_json_ld(page)
    author_value = json_ld.get("author", {})
    author = author_value.get("name") if isinstance(author_value, dict) else None
    title = clean_bilibili_title(
        json_ld.get("name")
        or meta_content(page, 'meta[itemprop="name"]')
        or meta_content(page, 'meta[property="og:title"]')
        or page.title()
    )
    published_at = (
        json_ld.get("datePublished")
        or json_ld.get("uploadDate")
        or meta_content(page, 'meta[itemprop="datePublished"]')
        or meta_content(page, 'meta[itemprop="uploadDate"]')
    )
    date_value = published_date(published_at)
    counts = interaction_counts(json_ld)
    return {
        "keyword": candidate["keyword"],
        "search_rank": candidate["search_rank"],
        "video_id": candidate["video_id"],
        "title": title,
        "author": author or meta_content(page, 'meta[name="author"]'),
        "published_at": published_at,
        "duration": json_ld.get("duration"),
        **counts,
        "in_date_range": bool(date_value and start_date <= date_value <= end_date),
        "url": candidate["url"],
        "http_status": status_code,
        "crawled_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "status": "ok",
        "error": None,
    }


def error_row(candidate: dict[str, Any], exc: Exception) -> dict[str, Any]:
    row = {column: None for column in COLUMNS}
    row.update(
        {
            "keyword": candidate["keyword"],
            "search_rank": candidate["search_rank"],
            "video_id": candidate["video_id"],
            "title": candidate.get("title"),
            "in_date_range": False,
            "url": candidate["url"],
            "crawled_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "status": "error",
            "error": f"{type(exc).__name__}: {exc}",
        }
    )
    return row


def write_csv(rows: list[dict[str, Any]], path: Path) -> None:
    pd.DataFrame(rows, columns=COLUMNS).to_csv(path, index=False, encoding="utf-8-sig")


def export(rows: list[dict[str, Any]], output: Path, summary: dict[str, Any]) -> Path:
    all_rows = pd.DataFrame(rows, columns=COLUMNS)
    filtered = all_rows[all_rows["in_date_range"] == True]  # noqa: E712
    summary_frame = pd.DataFrame([{"项目": key, "值": value} for key, value in summary.items()])
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        filtered.to_excel(writer, sheet_name="日期范围内结果", index=False)
        all_rows.to_excel(writer, sheet_name="全部候选", index=False)
        summary_frame.to_excel(writer, sheet_name="运行摘要", index=False)
    csv_path = output.with_suffix(".csv")
    write_csv(rows, csv_path)
    return csv_path


def parse_date(value: str) -> date:
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as exc:
        raise argparse.ArgumentTypeError("日期必须是 YYYY-MM-DD") from exc


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--keyword", required=True)
    parser.add_argument("--start-date", required=True, type=parse_date)
    parser.add_argument("--end-date", required=True, type=parse_date)
    parser.add_argument("--max-results", type=int, default=0, help="最多处理条数，0 表示不限")
    parser.add_argument("--max-pages", type=int, default=0, help="最多搜索页数，0 表示直到无新增")
    parser.add_argument("--page-delay", type=float, default=2.0, help="翻页间隔秒数")
    parser.add_argument("--delay", type=float, default=2.0)
    parser.add_argument("--output", type=Path, default=Path("bilibili_results.xlsx"))
    parser.add_argument("--checkpoint", type=Path, default=Path("bilibili_checkpoint.csv"))
    parser.add_argument("--log", type=Path, default=Path("bilibili_crawler.log"))
    parser.add_argument("--channel", choices=("msedge", "chrome"), default="msedge")
    parser.add_argument("--headed", action="store_true")
    parser.add_argument("--target-year", type=int, help="仅处理搜索卡片中属于该年份的候选")
    parser.add_argument("--csv-only", action="store_true", help="只导出 CSV 中间数据")
    args = parser.parse_args()
    if args.start_date > args.end_date:
        parser.error("开始日期不能晚于结束日期")
    if args.max_results < 0 or args.max_pages < 0 or args.page_delay < 0 or args.delay < 0:
        parser.error("max-results、max-pages、page-delay 和 delay 不能小于 0")
    return args


def main() -> int:
    args = parse_args()
    logger = configure_logging(args.log)
    started_at = datetime.now().astimezone()
    rows: list[dict[str, Any]] = []
    try:
        logger.info("搜索关键词：%s", args.keyword)
        candidates = crawl_search(
            args.keyword,
            args.max_results,
            args.channel,
            args.headed,
            args.max_pages,
            args.page_delay,
            args.target_year,
        )
        logger.info("发现 %s 条候选视频", len(candidates))
        if not candidates and not args.target_year:
            raise RuntimeError("搜索页没有返回视频链接")

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel=args.channel, headless=not args.headed)
            try:
                page = browser.new_page(viewport={"width": 1400, "height": 900})
                for index, candidate in enumerate(candidates, start=1):
                    logger.info("处理 %s/%s：%s", index, len(candidates), candidate["url"])
                    try:
                        row = extract_detail(page, candidate, args.start_date, args.end_date)
                    except Exception as exc:
                        logger.exception("详情提取失败：%s", candidate["url"])
                        row = error_row(candidate, exc)
                    rows.append(row)
                    write_csv(rows, args.checkpoint)
                    if index < len(candidates):
                        time.sleep(args.delay)
            finally:
                browser.close()
    except Exception as exc:
        logger.exception("程序运行失败")
        print(f"运行失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    summary = {
        "关键词": args.keyword,
        "开始日期": args.start_date.isoformat(),
        "结束日期": args.end_date.isoformat(),
        "候选数量": len(rows),
        "成功数量": sum(row["status"] == "ok" for row in rows),
        "日期范围内数量": sum(bool(row["in_date_range"]) for row in rows),
        "失败数量": sum(row["status"] != "ok" for row in rows),
        "开始时间": started_at.isoformat(timespec="seconds"),
        "结束时间": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    if args.csv_only:
        csv_path = args.output if args.output.suffix.lower() == ".csv" else args.output.with_suffix(".csv")
        write_csv(rows, csv_path)
    else:
        csv_path = export(rows, args.output, summary)
        logger.info("Excel 已保存：%s", args.output.resolve())
    logger.info("CSV 已保存：%s", csv_path.resolve())
    logger.info("Checkpoint：%s", args.checkpoint.resolve())
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Collect one keyword from Tencent News' public PC search result endpoint."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import logging
import math
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import quote

from playwright.sync_api import BrowserContext, Error as PlaywrightError, Page
from playwright.sync_api import sync_playwright


SEARCH_PAGE = "https://news.qq.com/search"
SEARCH_API = "https://i.news.qq.com/gw/pc_search/result"
PAGE_SIZE = 10
PROFILE_DIR = Path(__file__).resolve().parent / ".tencent_news_profile"
COLUMNS = (
    "keyword",
    "page_number",
    "search_rank",
    "result_id",
    "title",
    "summary",
    "source",
    "published_at",
    "content_type",
    "comments",
    "likes",
    "shares",
    "reads",
    "matches_tianjin",
    "matches_topic",
    "quality_status",
    "url",
    "search_url",
    "crawled_at",
)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def setup_logger(path: Path) -> logging.Logger:
    path.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(f"tencent_news.{path.stem}")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    formatter = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s")
    for handler in (logging.FileHandler(path, encoding="utf-8"), logging.StreamHandler(sys.stdout)):
        handler.setFormatter(formatter)
        logger.addHandler(handler)
    return logger


class LoginRequired(RuntimeError):
    pass


class RateLimited(RuntimeError):
    """Tencent search temporarily refuses otherwise valid public queries."""


def read_rows(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as file:
        return list(csv.DictReader(file))


def write_rows(rows: list[dict[str, Any]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows({column: row.get(column, "") for column in COLUMNS} for row in rows)
    temporary.replace(path)


def save_progress(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def topic_terms(keyword: str) -> tuple[str, ...]:
    values = [value for value in re.split(r"[\s+]+", keyword.strip()) if value]
    return tuple(value for value in values if value != "天津")


def publication_date(item: dict[str, Any]) -> str:
    value = str(item.get("time") or "").strip()
    match = re.match(r"(\d{4})-(\d{2})-(\d{2})", value)
    if match:
        return "-".join(match.groups())
    identifier = str(item.get("id") or "")
    match = re.search(r"((?:19|20)\d{2})(\d{2})(\d{2})", identifier)
    if match:
        try:
            return datetime(*(int(part) for part in match.groups())).date().isoformat()
        except ValueError:
            return ""
    timestamp = item.get("timestamp")
    if isinstance(timestamp, (int, float)) and timestamp > 0:
        return datetime.fromtimestamp(timestamp).astimezone().date().isoformat()
    return ""


def iter_result_items(payload: dict[str, Any]) -> Iterable[tuple[dict[str, Any], str]]:
    for section in payload.get("secList") or []:
        if not isinstance(section, dict):
            continue
        component = str(section.get("component") or "")
        for key in ("newsList", "videoList", "dataList", "list"):
            values = section.get(key)
            if isinstance(values, list):
                for item in values:
                    if isinstance(item, dict):
                        yield item, component
    for key in ("newsList", "videoList", "dataList", "list"):
        values = payload.get(key)
        if isinstance(values, list):
            for item in values:
                if isinstance(item, dict):
                    yield item, key.removesuffix("List")


def parse_item(
    item: dict[str, Any],
    component: str,
    keyword: str,
    page_number: int,
    rank: int,
    target_year: int,
    search_url: str,
) -> dict[str, Any] | None:
    result_id = str(item.get("id") or "").strip()
    title = str(item.get("title") or item.get("longtitle") or "").strip()
    published_at = publication_date(item)
    if not result_id or not title or not published_at or not published_at.startswith(f"{target_year}-"):
        return None
    summary = str(item.get("abstract") or item.get("nlpAbstract") or "").strip()
    source = str(item.get("source") or item.get("chlname") or item.get("uinnick") or "").strip()
    content_type = str(item.get("entity_type") or component or item.get("articletype") or "unknown")
    url = str(item.get("url") or item.get("surl") or item.get("shareUrl") or "").strip()
    if not url:
        url = f"https://news.qq.com/rain/a/{result_id}"
    terms = topic_terms(keyword)
    searchable = f"{title}\n{summary}"
    matches_tianjin = "天津" in searchable
    matches_topic = any(term in searchable for term in terms) if terms else True
    stable_id = hashlib.sha256(f"{result_id}|{url}".encode("utf-8")).hexdigest()[:20]
    return {
        "keyword": keyword,
        "page_number": page_number,
        "search_rank": rank,
        "result_id": stable_id,
        "title": title,
        "summary": summary,
        "source": source,
        "published_at": published_at,
        "content_type": content_type,
        "comments": item.get("commentNum", item.get("comments", "")),
        "likes": item.get("likeInfo", ""),
        "shares": item.get("shareCount", ""),
        "reads": item.get("readCount", ""),
        "matches_tianjin": matches_tianjin,
        "matches_topic": matches_topic,
        "quality_status": "strict" if matches_tianjin and matches_topic else "broad",
        "url": url,
        "search_url": search_url,
        "crawled_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }


def create_context(playwright: Any, profile_dir: Path, headed: bool) -> BrowserContext:
    profile_dir.mkdir(parents=True, exist_ok=True)
    return playwright.chromium.launch_persistent_context(
        user_data_dir=str(profile_dir.resolve()),
        channel="msedge",
        headless=not headed,
        viewport={"width": 1440, "height": 1000},
        locale="zh-CN",
    )


def fetch_page(page: Page, keyword: str, api_page: int, args: argparse.Namespace) -> dict[str, Any]:
    last_payload: object = None
    page_number = api_page + 1
    target_url = f"{SEARCH_PAGE}?query={quote(keyword)}&page={page_number}"
    for attempt in range(4):
        try:
            with page.expect_response(
                lambda response: response.url.startswith(SEARCH_API),
                timeout=int(args.timeout * 1000),
            ) as response_info:
                page.goto(
                    target_url,
                    wait_until="domcontentloaded",
                    timeout=int(args.timeout * 1000),
                )
            payload = response_info.value.json()
        except PlaywrightError as exc:
            last_payload = str(exc)
            if attempt < 3:
                time.sleep((attempt + 1) * 3)
                continue
            raise RuntimeError(f"腾讯新闻页面连续加载失败：{exc}") from exc
        last_payload = payload
        if isinstance(payload, dict) and payload.get("ret") in (0, "0", None):
            return payload
        if (
            isinstance(payload, dict)
            and str(payload.get("ret")) == "400"
            and payload.get("isEmpty") in (1, "1", True)
            and int(payload.get("total_num") or 0) == 0
        ):
            raise RateLimited(
                "腾讯新闻搜索返回 ret=400/isEmpty=1，疑似触发临时频率限制；"
                "停止当前请求并交由批处理冷却后重试"
            )
        body = page.locator("body").inner_text()[:5000]
        if any(marker in body for marker in ("安全验证", "扫码登录", "请先登录", "验证码")):
            raise LoginRequired("腾讯新闻页面要求人工登录或验证")
        if attempt < 3:
            time.sleep((attempt + 1) * 3)
    raise RuntimeError(f"腾讯新闻搜索连续返回异常：{str(last_payload)[:500]}")


def open_manual_window(args: argparse.Namespace, logger: logging.Logger) -> None:
    logger.warning("页面要求登录或验证，正在打开可见 Edge；请手动完成后关闭整个窗口。")
    with sync_playwright() as playwright:
        context = create_context(playwright, args.profile_dir, headed=True)
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(
            f"{SEARCH_PAGE}?query={quote(args.keyword)}",
            wait_until="domcontentloaded",
            timeout=int(args.timeout * 1000),
        )
        deadline = time.time() + args.login_timeout
        while time.time() < deadline and not page.is_closed():
            time.sleep(1)
        try:
            context.close()
        except Exception:
            pass


def collect(args: argparse.Namespace, logger: logging.Logger) -> list[dict[str, Any]]:
    rows = [] if args.fresh else read_rows(args.checkpoint)
    seen = {str(row.get("result_id")) for row in rows if row.get("result_id")}
    total_results = 0
    total_pages = args.max_pages if args.max_pages > 0 else 1_000_000
    empty_pages = 0

    with sync_playwright() as playwright:
        context = create_context(playwright, args.profile_dir, args.headed)
        page = context.pages[0] if context.pages else context.new_page()
        try:
            for api_page in range(total_pages):
                page_number = api_page + 1
                search_url = f"{SEARCH_PAGE}?query={quote(args.keyword)}&page={page_number}"
                logger.info("搜索：%s｜第 %s 页", args.keyword, page_number)
                try:
                    payload = fetch_page(page, args.keyword, api_page, args)
                except (ValueError, RuntimeError) as exc:
                    logger.error("第 %s 页请求失败：%s", page_number, exc)
                    raise

                if api_page == 0:
                    total_results = int(payload.get("total_num") or 0)
                    available_pages = max(math.ceil(total_results / PAGE_SIZE), 1)
                    if args.max_pages <= 0:
                        total_pages = available_pages
                    else:
                        total_pages = min(args.max_pages, available_pages)
                    logger.info("平台报告约 %s 条搜索结果，计划最多读取 %s 页", total_results, total_pages)

                page_ids: set[str] = set()
                added = 0
                eligible = 0
                for index, (item, component) in enumerate(iter_result_items(payload), start=1):
                    raw_id = str(item.get("id") or "")
                    if not raw_id or raw_id in page_ids:
                        continue
                    page_ids.add(raw_id)
                    row = parse_item(
                        item, component, args.keyword, page_number,
                        api_page * PAGE_SIZE + index, args.target_year, search_url,
                    )
                    if not row:
                        continue
                    eligible += 1
                    if row["result_id"] in seen:
                        continue
                    seen.add(row["result_id"])
                    rows.append(row)
                    added += 1

                write_rows(rows, args.checkpoint)
                save_progress(
                    args.progress,
                    {
                        "keyword": args.keyword,
                        "target_year": args.target_year,
                        "page_number": page_number,
                        "planned_pages": total_pages,
                        "platform_total_results": total_results,
                        "rows": len(rows),
                        "has_more": bool(payload.get("hasMore")),
                        "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                    },
                )
                logger.info("第 %s 页识别到 %s 条 %s 年结果，新增 %s 条；累计 %s 条", page_number, eligible, args.target_year, added, len(rows))

                empty_pages = empty_pages + 1 if not page_ids else 0
                if empty_pages >= 2 or not payload.get("hasMore") or page_number >= total_pages:
                    break
                if args.delay:
                    time.sleep(args.delay)
        finally:
            context.close()
    return rows


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--keyword", required=True)
    parser.add_argument("--target-year", type=int, default=2025)
    parser.add_argument("--max-pages", type=int, default=0, help="0 表示按平台报告页数读取")
    parser.add_argument("--delay", type=float, default=1.2)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--headed", action="store_true")
    parser.add_argument("--auto-login-window", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--login-timeout", type=int, default=600)
    parser.add_argument("--profile-dir", type=Path, default=PROFILE_DIR)
    parser.add_argument("--fresh", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--checkpoint", type=Path, required=True)
    parser.add_argument("--progress", type=Path, required=True)
    parser.add_argument("--log", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    logger = setup_logger(args.log)
    try:
        rows = collect(args, logger)
    except LoginRequired as exc:
        if args.headed or not args.auto_login_window:
            logger.error("页面要求人工处理：%s", exc)
            return 2
        open_manual_window(args, logger)
        logger.info("人工窗口已关闭，重新尝试当前关键词")
        try:
            rows = collect(args, logger)
        except LoginRequired as retry_exc:
            logger.error("人工处理后仍要求登录或验证：%s", retry_exc)
            return 2
    except RateLimited as exc:
        logger.warning("检测到腾讯搜索限流：%s", exc)
        return 3
    except Exception as exc:
        logger.error("当前关键词采集失败：%s", exc)
        return 1
    write_rows(rows, args.output)
    print(
        json.dumps(
            {
                "keyword": args.keyword,
                "target_year": args.target_year,
                "rows": len(rows),
                "output": str(args.output.resolve()),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

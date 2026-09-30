"""Low-frequency collector for public Today Toutiao search result cards.

The script does not call private APIs or bypass login/verification.  When a
login or verification page is detected it can open a visible persistent Edge
window and wait for the user to complete the platform-provided flow.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import logging
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urljoin, urlparse

from playwright.sync_api import BrowserContext, Error as PlaywrightError, Page
from playwright.sync_api import sync_playwright


PROJECT_DIR = Path(__file__).resolve().parent
PROFILE_DIR = PROJECT_DIR / ".toutiao_profile"
BASE_SEARCH_URL = "https://so.toutiao.com/search"
LOGIN_URL = "https://www.toutiao.com/"
DEFAULT_TABS = ("synthesis", "information", "video", "weitoutiao")
TAB_NAMES = {
    "synthesis": "综合",
    "information": "资讯",
    "video": "视频",
    "weitoutiao": "微头条",
}
COLUMNS = (
    "keyword",
    "query",
    "tab",
    "tab_name",
    "page_number",
    "search_rank",
    "result_id",
    "title",
    "summary",
    "source",
    "published_at",
    "published_at_raw",
    "date_basis",
    "content_type",
    "matches_tianjin",
    "matches_topic",
    "quality_status",
    "url",
    "search_url",
    "http_status",
    "crawled_at",
)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


class LoginRequired(RuntimeError):
    pass


def setup_logger(path: Path) -> logging.Logger:
    path.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(f"toutiao.{path.stem}")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    formatter = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s")
    for handler in (
        logging.FileHandler(path, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ):
        handler.setFormatter(formatter)
        logger.addHandler(handler)
    return logger


def build_search_url(query: str, tab: str) -> str:
    return f"{BASE_SEARCH_URL}?keyword={quote(query)}&pd={quote(tab)}"


def decode_result_url(href: str) -> str:
    """Unwrap Toutiao's public search jump URL without requesting it."""
    absolute = urljoin(BASE_SEARCH_URL, href)
    values = parse_qs(urlparse(absolute).query)
    candidate = values.get("url", [absolute])[0]
    candidate = unquote(candidate)
    nested = parse_qs(urlparse(candidate).query).get("h5_url", [])
    if nested:
        candidate = unquote(nested[0])
    return candidate


def normalize_date(text: str, target_year: int) -> tuple[str, str, str]:
    """Return ISO date, raw match and basis; only explicit target-year dates pass."""
    patterns = (
        (rf"{target_year}年\s*(\d{{1,2}})月\s*(\d{{1,2}})日", "explicit_chinese"),
        (rf"{target_year}[-/.](\d{{1,2}})[-/.](\d{{1,2}})", "explicit_numeric"),
    )
    for pattern, basis in patterns:
        match = re.search(pattern, text)
        if match:
            month, day = (int(value) for value in match.groups())
            try:
                value = datetime(target_year, month, day).date().isoformat()
                return value, match.group(0), basis
            except ValueError:
                continue
    return "", "", "not_explicit_target_year"


def topic_terms(keyword: str) -> tuple[str, ...]:
    values = [value for value in re.split(r"[\s+]+", keyword.strip()) if value]
    return tuple(value for value in values if value != "天津")


def blocked_reason(page: Page, desktop: bool = False) -> str:
    title = page.title()
    body = page.locator("body").inner_text()[:5000]
    combined = f"{title}\n{body}"
    for marker in ("验证码", "安全验证", "访问过于频繁", "扫码登录", "请先登录"):
        if marker in combined:
            return marker
    result_count = (
        page.locator("a.text-ellipsis.text-underline-hover").count()
        if desktop else page.locator("div.l-card").count()
    )
    if result_count == 0 and re.search(r"(?:^|\n)登录(?:\n|$)", body):
        return "登录后才能查看搜索结果"
    return ""


def card_text(locator: Any, selector: str) -> str:
    item = locator.locator(selector)
    return item.first.inner_text().strip() if item.count() else ""


def parse_card(card: Any, keyword: str, query: str, tab: str, rank: int, status: int, search_url: str, target_year: int) -> dict[str, Any] | None:
    title_link = card.locator("a.l-card-title")
    if not title_link.count():
        return None
    title = title_link.first.inner_text().strip()
    href = title_link.first.get_attribute("href") or ""
    if not title or not href:
        return None

    all_text = card.inner_text().strip()
    lines = [line.strip() for line in all_text.splitlines() if line.strip()]
    footer = card_text(card, ".l-source")
    footer_lines = [line.strip() for line in footer.splitlines() if line.strip()]
    source = footer_lines[0] if footer_lines else ""
    published_at, published_raw, date_basis = normalize_date(footer, target_year)
    if not published_at:
        published_at, published_raw, date_basis = normalize_date(all_text, target_year)
        if published_at:
            date_basis = f"card_{date_basis}"

    summary_lines = [line for line in lines[1:] if line not in footer_lines]
    summary = " ".join(summary_lines).strip()
    final_url = decode_result_url(href)
    result_id = hashlib.sha256(final_url.encode("utf-8")).hexdigest()[:20]
    terms = topic_terms(keyword)
    searchable = f"{title}\n{summary}"
    matches_tianjin = "天津" in searchable
    matches_topic = any(term in searchable for term in terms) if terms else True
    return {
        "keyword": keyword,
        "query": query,
        "tab": tab,
        "tab_name": TAB_NAMES.get(tab, tab),
        "page_number": 1,
        "search_rank": rank,
        "result_id": result_id,
        "title": title,
        "summary": summary,
        "source": source,
        "published_at": published_at,
        "published_at_raw": published_raw,
        "date_basis": date_basis,
        "content_type": tab,
        "matches_tianjin": matches_tianjin,
        "matches_topic": matches_topic,
        "quality_status": "strict" if matches_tianjin and matches_topic else "broad",
        "url": final_url,
        "search_url": search_url,
        "http_status": status,
        "crawled_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }


def desktop_source_and_summary(lines: list[str], title: str, published_raw: str) -> tuple[str, str]:
    """Best-effort split of a desktop result card's summary and source."""
    source_index = -1
    metric_pattern = re.compile(r"^(?:\d+(?:\.\d+)?(?:万)?(?:评论|点赞|次观看|粉丝)|\d+评论)$")
    date_pattern = re.compile(r"(?:\d{4}年\d{1,2}月\d{1,2}日|\d{4}[-/.]\d{1,2}[-/.]\d{1,2}|\d{1,2}月\d{1,2}日|\d+(?:分钟|小时|天|个月|年)前)$")
    for index in range(len(lines) - 1, 0, -1):
        candidate = lines[index]
        if metric_pattern.fullmatch(candidate) or date_pattern.search(candidate):
            continue
        if len(candidate) <= 80:
            source_index = index
            break
    source = lines[source_index] if source_index > 0 else ""
    summary_end = source_index if source_index > 0 else len(lines)
    summary = " ".join(lines[1:summary_end]).strip()
    return source, summary


def parse_desktop_card(
    title_link: Any,
    keyword: str,
    rank: int,
    page_number: int,
    status: int,
    search_url: str,
    target_year: int,
) -> dict[str, Any] | None:
    title = title_link.inner_text().strip()
    href = title_link.get_attribute("href") or ""
    parent_class = title_link.locator("..").get_attribute("class") or ""
    if not title or not href or "text-xl" not in parent_class or title.endswith("-视频"):
        return None
    card = title_link.locator("xpath=ancestor::div[contains(@class,'cs-card-content')][1]")
    if not card.count():
        return None
    all_text = card.first.inner_text().strip()
    published_at, published_raw, date_basis = normalize_date(all_text, target_year)
    if not published_at:
        return None
    lines = [line.strip() for line in all_text.splitlines() if line.strip()]
    source, summary = desktop_source_and_summary(lines, title, published_raw)
    final_url = decode_result_url(href)
    result_id = hashlib.sha256(final_url.encode("utf-8")).hexdigest()[:20]
    terms = topic_terms(keyword)
    searchable = f"{title}\n{summary}"
    matches_tianjin = "天津" in searchable
    matches_topic = any(term in searchable for term in terms) if terms else True
    return {
        "keyword": keyword,
        "query": keyword,
        "tab": "desktop_synthesis",
        "tab_name": "桌面综合",
        "page_number": page_number,
        "search_rank": rank,
        "result_id": result_id,
        "title": title,
        "summary": summary,
        "source": source,
        "published_at": published_at,
        "published_at_raw": published_raw,
        "date_basis": f"desktop_{date_basis}",
        "content_type": "article_or_video",
        "matches_tianjin": matches_tianjin,
        "matches_topic": matches_topic,
        "quality_status": "strict" if matches_tianjin and matches_topic else "broad",
        "url": final_url,
        "search_url": search_url,
        "http_status": status,
        "crawled_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }


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


def create_context(playwright: Any, profile_dir: Path, headed: bool) -> BrowserContext:
    profile_dir.mkdir(parents=True, exist_ok=True)
    return playwright.chromium.launch_persistent_context(
        user_data_dir=str(profile_dir.resolve()),
        channel="msedge",
        headless=not headed,
        viewport={"width": 1440, "height": 1000},
        locale="zh-CN",
    )


def open_manual_login_window(profile_dir: Path, timeout_seconds: int, logger: logging.Logger) -> None:
    logger.warning("检测到登录/验证要求，正在打开可见 Edge。请手动扫码或验证，完成后关闭整个 Edge 窗口。")
    with sync_playwright() as playwright:
        context = create_context(playwright, profile_dir, headed=True)
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=60_000)
        deadline = time.time() + timeout_seconds
        while time.time() < deadline:
            try:
                if not context.pages:
                    break
                time.sleep(1)
            except Exception:
                break
        try:
            context.close()
        except Exception:
            pass


def collect_mobile(args: argparse.Namespace, logger: logging.Logger) -> list[dict[str, Any]]:
    rows = [] if args.fresh else read_rows(args.checkpoint)
    seen = {str(row.get("result_id")) for row in rows if row.get("result_id")}
    queries = [args.keyword]
    hinted = f"{args.keyword} {args.target_year}"
    if hinted not in queries:
        queries.append(hinted)

    with sync_playwright() as playwright:
        context = create_context(playwright, args.profile_dir, args.headed)
        page = context.pages[0] if context.pages else context.new_page()
        try:
            for query in queries:
                for tab in args.tabs:
                    search_url = build_search_url(query, tab)
                    logger.info("搜索：%s｜%s", query, TAB_NAMES.get(tab, tab))
                    try:
                        response = page.goto(
                            search_url,
                            wait_until="domcontentloaded",
                            timeout=args.timeout * 1000,
                        )
                        page.wait_for_timeout(args.page_wait * 1000)
                    except PlaywrightError as exc:
                        logger.warning("页面导航失败，跳过当前标签：%s", exc)
                        continue
                    reason = blocked_reason(page)
                    if reason:
                        raise LoginRequired(reason)
                    status = response.status if response else 0
                    cards = page.locator("div.l-card")
                    added = 0
                    for index in range(cards.count()):
                        row = parse_card(
                            cards.nth(index), args.keyword, query, tab,
                            index + 1, status, search_url, args.target_year,
                        )
                        if not row or not row["published_at"]:
                            continue
                        if row["result_id"] in seen:
                            continue
                        seen.add(row["result_id"])
                        rows.append(row)
                        added += 1
                    write_rows(rows, args.checkpoint)
                    logger.info("当前标签新增 %s 条明确属于 %s 年的结果；累计 %s 条", added, args.target_year, len(rows))
                    if args.delay:
                        time.sleep(args.delay)
        finally:
            context.close()
    return rows


def collect_desktop(args: argparse.Namespace, logger: logging.Logger) -> list[dict[str, Any]]:
    rows = [] if args.fresh else read_rows(args.checkpoint)
    seen = {str(row.get("result_id")) for row in rows if row.get("result_id")}
    first_url = f"https://www.toutiao.com/search/?keyword={quote(args.keyword)}"

    with sync_playwright() as playwright:
        context = create_context(playwright, args.profile_dir, args.headed)
        page = context.pages[0] if context.pages else context.new_page()
        current_url = first_url
        visited_urls: set[str] = set()
        try:
            for page_number in range(1, args.max_pages + 1):
                if current_url in visited_urls:
                    logger.warning("翻页地址重复，停止当前关键词")
                    break
                visited_urls.add(current_url)
                logger.info("桌面综合搜索：%s｜第 %s/%s 页", args.keyword, page_number, args.max_pages)
                try:
                    response = page.goto(
                        current_url,
                        wait_until="domcontentloaded",
                        timeout=args.timeout * 1000,
                    )
                    page.wait_for_timeout(args.page_wait * 1000)
                except PlaywrightError as exc:
                    logger.warning("第 %s 页导航失败：%s", page_number, exc)
                    break
                reason = blocked_reason(page, desktop=True)
                if reason:
                    raise LoginRequired(reason)
                status = response.status if response else 0
                candidates = page.locator("a.text-ellipsis.text-underline-hover")
                added = 0
                eligible = 0
                for index in range(candidates.count()):
                    row = parse_desktop_card(
                        candidates.nth(index), args.keyword,
                        (page_number - 1) * 100 + index + 1,
                        page_number, status, page.url, args.target_year,
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
                logger.info("第 %s 页识别到 %s 条 %s 年结果，新增 %s 条；累计 %s 条", page_number, eligible, args.target_year, added, len(rows))

                next_link = page.locator("a", has_text="下一页")
                next_href = ""
                for index in range(next_link.count()):
                    text_value = next_link.nth(index).inner_text().strip()
                    if text_value == "下一页":
                        next_href = next_link.nth(index).get_attribute("href") or ""
                        if next_href:
                            break
                if not next_href:
                    logger.info("未发现下一页，当前关键词结束")
                    break
                current_url = urljoin(page.url, next_href).replace("http://so.toutiao.com/", "https://so.toutiao.com/")
                if args.delay:
                    time.sleep(args.delay)
        finally:
            context.close()
    return rows


def collect(args: argparse.Namespace, logger: logging.Logger) -> list[dict[str, Any]]:
    if args.mode == "desktop":
        return collect_desktop(args, logger)
    return collect_mobile(args, logger)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--keyword", required=True)
    parser.add_argument("--target-year", type=int, default=2025)
    parser.add_argument("--mode", choices=("desktop", "mobile"), default="desktop")
    parser.add_argument("--max-pages", type=int, default=10)
    parser.add_argument("--tabs", nargs="+", default=list(DEFAULT_TABS), choices=sorted(TAB_NAMES))
    parser.add_argument("--delay", type=float, default=2.0)
    parser.add_argument("--page-wait", type=float, default=5.0)
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument("--headed", action="store_true")
    parser.add_argument("--auto-login-window", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--login-timeout", type=int, default=600)
    parser.add_argument("--fresh", action="store_true")
    parser.add_argument("--profile-dir", type=Path, default=PROFILE_DIR)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--checkpoint", type=Path, required=True)
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
        open_manual_login_window(args.profile_dir, args.login_timeout, logger)
        logger.info("人工登录窗口已关闭，重新尝试当前关键词")
        try:
            rows = collect(args, logger)
        except LoginRequired as retry_exc:
            logger.error("人工处理后页面仍要求登录/验证：%s", retry_exc)
            return 2
    write_rows(rows, args.output)
    summary = {
        "keyword": args.keyword,
        "target_year": args.target_year,
        "rows": len(rows),
        "output": str(args.output.resolve()),
        "checkpoint": str(args.checkpoint.resolve()),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

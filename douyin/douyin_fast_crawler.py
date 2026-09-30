"""Fast Douyin search collector using JSON responses loaded by the visible search page."""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import quote, urlsplit

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Response, sync_playwright


PROFILE_DIR = Path(__file__).resolve().parent / ".douyin_profile"
COLUMNS = (
    "keyword", "title", "author", "published_at", "content_type", "url",
    "content_id", "likes", "comments", "shares", "crawled_at",
)
VERIFY_MARKERS = ("验证码", "安全验证", "请完成验证", "滑块验证", "点击验证")
RATE_LIMIT_MARKERS = ("搜索过于频繁", "操作过于频繁")

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


class VerificationTimeout(RuntimeError):
    pass


class RateLimitDetected(RuntimeError):
    pass


def rate_limit_marker(page: Any) -> str:
    """Return a visible Douyin search rate-limit message, if present."""
    for marker in RATE_LIMIT_MARKERS:
        try:
            locator = page.get_by_text(marker, exact=False)
            for index in range(min(locator.count(), 5)):
                if locator.nth(index).is_visible():
                    return marker
        except PlaywrightError:
            continue
    return ""


def iter_objects(value: Any) -> Iterable[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from iter_objects(child)
    elif isinstance(value, list):
        for child in value:
            yield from iter_objects(child)


def decode_json_documents(raw_text: str) -> list[Any]:
    decoder = json.JSONDecoder()
    documents: list[Any] = []
    cursor = 0
    while cursor < len(raw_text):
        starts = [index for index in (raw_text.find("{", cursor), raw_text.find("[", cursor)) if index >= 0]
        if not starts:
            break
        start = min(starts)
        try:
            payload, end = decoder.raw_decode(raw_text, start)
        except json.JSONDecodeError:
            cursor = start + 1
            continue
        documents.append(payload)
        cursor = end
    return documents


def parse_item(obj: dict[str, Any], keyword: str, target_year: int) -> dict[str, Any] | None:
    item = obj.get("aweme_info") if isinstance(obj.get("aweme_info"), dict) else obj
    content_id = str(item.get("aweme_id") or item.get("item_id") or "").strip()
    if not content_id:
        return None
    raw_time = item.get("create_time")
    try:
        published = datetime.fromtimestamp(int(raw_time)).astimezone()
    except (TypeError, ValueError, OSError):
        return None
    if published.year != target_year:
        return None
    author = item.get("author") if isinstance(item.get("author"), dict) else {}
    statistics = item.get("statistics") if isinstance(item.get("statistics"), dict) else {}
    content_type = "图文" if item.get("images") else "视频"
    return {
        "keyword": keyword,
        "title": str(item.get("desc") or item.get("title") or "").strip(),
        "author": str(author.get("nickname") or "").strip(),
        "published_at": published.date().isoformat(),
        "content_type": content_type,
        "url": f"https://www.douyin.com/video/{content_id}",
        "content_id": content_id,
        "likes": statistics.get("digg_count", ""),
        "comments": statistics.get("comment_count", ""),
        "shares": statistics.get("share_count", ""),
        "crawled_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }


def write_rows(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows({column: row.get(column, "") for column in COLUMNS} for row in rows)
    temporary.replace(path)


def read_rows(path: Path) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8-sig", newline="") as file:
        rows = list(csv.DictReader(file))
    return {str(row.get("content_id")): row for row in rows if row.get("content_id")}


def body_marker(page: Any) -> str:
    """Return only a visible verification marker, ignoring hidden page text."""
    try:
        title = page.title().strip()
    except PlaywrightError:
        return ""
    if any(marker in title for marker in ("验证码中间页", "安全验证", "请完成验证")):
        return title
    selectors = (
        'iframe[src*="captcha"]', '[class*="captcha"]', '[id*="captcha"]',
        'text="请完成验证"', 'text="滑块验证"', 'text="点击验证"', 'text="安全验证"',
    )
    for selector in selectors:
        try:
            locator = page.locator(selector)
            for index in range(min(locator.count(), 5)):
                if locator.nth(index).is_visible():
                    return selector
        except PlaywrightError:
            continue
    return ""


def wait_for_verification(page: Any, timeout: float, interactive: bool) -> None:
    rate_marker = rate_limit_marker(page)
    if rate_marker:
        raise RateLimitDetected(f"检测到‘{rate_marker}’，保存断点并进入自动冷却")
    marker = body_marker(page)
    if not marker:
        return
    if not interactive:
        raise VerificationTimeout("后台模式检测到可见验证，需要弹出浏览器人工处理")
    print(f"出现{marker}，已暂停；请在可见窗口中人工完成验证（最多等待 {timeout:.0f} 秒）", flush=True)
    deadline = time.time() + timeout
    while time.time() < deadline and not page.is_closed():
        time.sleep(2)
        if not body_marker(page):
            print("验证已解除，继续快速采集", flush=True)
            return
    raise VerificationTimeout(f"人工验证等待超时（{timeout:.0f}秒）")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--keyword", required=True)
    parser.add_argument("--target-year", type=int, default=2025)
    parser.add_argument("--max-scrolls", type=int, default=30)
    parser.add_argument("--delay", type=float, default=2.0)
    parser.add_argument("--initial-wait", type=float, default=8.0)
    parser.add_argument("--manual-verify-timeout", type=float, default=900.0)
    parser.add_argument("--profile-dir", type=Path, default=PROFILE_DIR)
    parser.add_argument("--headless", action="store_true", help="后台运行；验证时由批处理改用可见窗口重试")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    items: dict[str, dict[str, Any]] = read_rows(args.output)
    response_count = 0

    def handle_response(response: Response) -> None:
        nonlocal response_count
        path = urlsplit(response.url).path
        if "/aweme/v1/web/general/search/" not in path:
            return
        if response.status != 200:
            return
        try:
            documents = decode_json_documents(response.text())
        except (PlaywrightError, ValueError):
            return
        response_count += 1
        for payload in documents:
            for obj in iter_objects(payload):
                row = parse_item(obj, args.keyword, args.target_year)
                if row:
                    items[row["content_id"]] = row

    try:
        with sync_playwright() as playwright:
            context = playwright.chromium.launch_persistent_context(
                user_data_dir=str(args.profile_dir.resolve()),
                channel="msedge", headless=args.headless, viewport={"width": 1440, "height": 900},
            )
            page = context.pages[0] if context.pages else context.new_page()
            page.on("response", handle_response)
            page.goto(
                f"https://www.douyin.com/search/{quote(args.keyword)}",
                wait_until="domcontentloaded", timeout=60_000,
            )
            page.wait_for_timeout(int(args.initial_wait * 1000))
            wait_for_verification(page, args.manual_verify_timeout, not args.headless)
            # Direct keyword URL normally triggers the search itself. Only submit the
            # search box when the page produced no search response at all; submitting
            # twice increases the chance of another verification challenge.
            page.wait_for_timeout(3_000)
            search_input = page.locator('[data-e2e="searchbar-input"]')
            search_button = page.locator('[data-e2e="searchbar-button"]')
            if response_count == 0 and search_input.count() and search_button.count():
                search_input.fill(args.keyword)
                search_button.click()
                page.wait_for_timeout(5_000)
                wait_for_verification(page, args.manual_verify_timeout, not args.headless)

            unchanged = 0
            last_count = -1
            for scroll in range(args.max_scrolls + 1):
                wait_for_verification(page, args.manual_verify_timeout, not args.headless)
                write_rows(args.output, list(items.values()))
                print(
                    f"{args.keyword}｜滚动 {scroll}/{args.max_scrolls}｜"
                    f"搜索响应 {response_count}｜2025年 {len(items)} 条",
                    flush=True,
                )
                unchanged = unchanged + 1 if len(items) == last_count else 0
                last_count = len(items)
                if unchanged >= 6:
                    break
                page.mouse.wheel(0, 2_200)
                page.wait_for_timeout(int(args.delay * 1000))
            context.close()
    except RateLimitDetected as exc:
        write_rows(args.output, list(items.values()))
        print(str(exc), file=sys.stderr, flush=True)
        return 3
    except VerificationTimeout as exc:
        write_rows(args.output, list(items.values()))
        print(str(exc), file=sys.stderr, flush=True)
        return 2
    except PlaywrightError as exc:
        write_rows(args.output, list(items.values()))
        print(f"浏览器错误：{exc}", file=sys.stderr, flush=True)
        return 1

    write_rows(args.output, list(items.values()))
    print(json.dumps({"keyword": args.keyword, "rows_2025": len(items), "output": str(args.output)}, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

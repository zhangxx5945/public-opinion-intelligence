"""小规模测试新浪微博公开关键词搜索页，并导出公开帖子字段。"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote, urljoin

from scrapling.fetchers import DynamicFetcher, Fetcher


BASE_URL = "https://s.weibo.com"
EDGE_PATHS = (
    Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
    Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"),
)
COLUMNS = [
    "keyword",
    "search_rank",
    "post_id",
    "author",
    "content",
    "published_at",
    "reposts",
    "reposts_raw",
    "comments",
    "comments_raw",
    "likes",
    "likes_raw",
    "url",
    "http_status",
    "crawled_at",
]

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def clean_text(value: str | None) -> str:
    if not value:
        return ""
    return re.sub(r"\s+", " ", value).strip()


def parse_count(value: str | None) -> int | None:
    text = clean_text(value).replace(",", "")
    match = re.search(r"(\d+(?:\.\d+)?)\s*([万亿]?)", text)
    if not match:
        return None
    number = float(match.group(1))
    multiplier = {"": 1, "万": 10_000, "亿": 100_000_000}[match.group(2)]
    return int(number * multiplier)


def parse_action_count(value: str | None, label: str) -> int | None:
    text = clean_text(value)
    parsed = parse_count(text)
    if parsed is not None:
        return parsed
    return 0 if label in text else None


def first_text(node: Any, selector: str) -> str:
    selected = node.css(selector)
    if not selected:
        return ""
    return clean_text(selected[0].get_all_text(separator=" ", strip=True))


def first_attr(node: Any, selector: str, attribute: str) -> str:
    selected = node.css(selector)
    if not selected:
        return ""
    return clean_text(selected[0].attrib.get(attribute))


def parse_card(card: Any, keyword: str, rank: int, http_status: int) -> dict[str, Any]:
    post_id = clean_text(card.attrib.get("mid"))
    author = first_text(card, "a.name")
    content = first_text(card, 'p[node-type="feed_list_content_full"]')
    if not content:
        content = first_text(card, 'p[node-type="feed_list_content"]')

    time_links = card.css("div.from a")
    published_at = ""
    relative_url = ""
    if time_links:
        published_at = clean_text(time_links[0].attrib.get("title")) or clean_text(time_links[0].text)
        relative_url = clean_text(time_links[0].attrib.get("href"))
    url = urljoin("https://weibo.com", relative_url) if relative_url else ""

    action_values: dict[str, str] = {}
    for action in card.css('div.card-act a[action-type]'):
        action_type = clean_text(action.attrib.get("action-type"))
        action_values[action_type] = clean_text(action.get_all_text(separator=" ", strip=True))
    reposts_raw = action_values.get("feed_list_forward", "")
    comments_raw = action_values.get("feed_list_comment", "")
    likes_raw = action_values.get("feed_list_like", "")
    reposts = parse_action_count(reposts_raw, "转发")
    comments = parse_action_count(comments_raw, "评论")
    likes = parse_action_count(likes_raw, "赞")

    if not post_id:
        identity = f"{url}\n{author}\n{content}".encode("utf-8")
        post_id = "derived_" + hashlib.sha256(identity).hexdigest()[:16]

    return {
        "keyword": keyword,
        "search_rank": rank,
        "post_id": post_id,
        "author": author,
        "content": content,
        "published_at": published_at,
        "reposts": reposts,
        "reposts_raw": reposts_raw,
        "comments": comments,
        "comments_raw": comments_raw,
        "likes": likes,
        "likes_raw": likes_raw,
        "url": url,
        "http_status": http_status,
        "crawled_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }


def blocked_reason(page: Any) -> str | None:
    title = clean_text(page.css("title::text").get())
    body = clean_text(page.css("body").get())[:1_000]
    text = f"{title} {body}"
    for marker in ("访问频繁", "安全验证", "请输入验证码", "登录"):
        if marker in text:
            return marker
    return None


def fetch_search_page(
    url: str,
    mode: str,
    headed: bool,
    profile_dir: Path,
    initial_wait: float,
) -> Any:
    if mode == "http":
        return Fetcher.get(
            url,
            impersonate="chrome",
            stealthy_headers=True,
            timeout=30,
            retries=2,
            follow_redirects="safe",
        )

    edge_path = next((path for path in EDGE_PATHS if path.exists()), None)
    options: dict[str, Any] = {
        "headless": not headed,
        "network_idle": False,
        "wait": initial_wait * 1_000,
        "timeout": 30_000,
        "user_data_dir": str(profile_dir.resolve()),
        "retries": 1,
    }
    if edge_path:
        options["executable_path"] = str(edge_path)
    return DynamicFetcher.fetch(url, **options)


def crawl(
    keyword: str,
    max_pages: int,
    max_results: int,
    delay: float,
    mode: str,
    headed: bool,
    profile_dir: Path,
    initial_wait: float,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()

    for page_number in range(1, max_pages + 1):
        search_url = f"{BASE_URL}/weibo?q={quote(keyword)}&page={page_number}"
        page = fetch_search_page(search_url, mode, headed, profile_dir, initial_wait)
        http_status = int(page.status)
        if http_status >= 400:
            raise RuntimeError(f"搜索第 {page_number} 页 HTTP {http_status}")

        cards = page.css('div.card-wrap[action-type="feed_list_item"]')
        if not cards:
            reason = blocked_reason(page)
            suffix = f"（页面提示：{reason}）" if reason else ""
            if reason == "登录":
                suffix += "；请先运行 weibo_login.py 正常登录"
            if page_number == 1:
                raise RuntimeError(f"公开搜索页没有返回微博帖子{suffix}")
            print(f"第 {page_number} 页没有新帖子，停止翻页{suffix}")
            break

        added = 0
        for card in cards:
            row = parse_card(card, keyword, len(rows) + 1, http_status)
            if row["post_id"] in seen:
                continue
            seen.add(row["post_id"])
            rows.append(row)
            added += 1
            if max_results and len(rows) >= max_results:
                break

        print(f"搜索页 {page_number}：新增 {added} 条，累计 {len(rows)} 条")
        if (max_results and len(rows) >= max_results) or added == 0:
            break
        if page_number < max_pages and delay:
            time.sleep(delay)

    return rows


def write_csv(rows: list[dict[str, Any]], output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(rows)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--keyword", default="人工智能")
    parser.add_argument("--max-pages", type=int, default=1)
    parser.add_argument("--max-results", type=int, default=10, help="0 表示不限制")
    parser.add_argument("--delay", type=float, default=2.0)
    parser.add_argument("--mode", choices=("browser", "http"), default="browser")
    parser.add_argument("--headed", action="store_true", help="显示浏览器窗口")
    parser.add_argument(
        "--initial-wait",
        type=float,
        default=5.0,
        help="页面打开后等待秒数；首次可见登录建议设为 60",
    )
    parser.add_argument(
        "--profile-dir",
        type=Path,
        default=Path(__file__).resolve().parent / ".weibo_profile",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(__file__).resolve().parent / "weibo_keyword_test.csv",
    )
    args = parser.parse_args()
    if args.max_pages <= 0 or args.max_results < 0 or args.delay < 0 or args.initial_wait < 0:
        parser.error("max-pages 必须大于 0，其他数值参数不能小于 0")
    return args


def main() -> int:
    args = parse_args()
    try:
        rows = crawl(
            args.keyword,
            args.max_pages,
            args.max_results,
            args.delay,
            args.mode,
            args.headed,
            args.profile_dir,
            args.initial_wait,
        )
        if not rows:
            raise RuntimeError("没有提取到公开微博帖子")
        write_csv(rows, args.output)
    except Exception as exc:
        print(f"运行失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    print(
        json.dumps(
            {"keyword": args.keyword, "count": len(rows), "output": str(args.output.resolve())},
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

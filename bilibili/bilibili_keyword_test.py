"""按页采集 B 站公开搜索页中的视频链接和标题。"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import date, datetime
from pathlib import Path
from urllib.parse import quote, urlunsplit

import pandas as pd
from playwright.sync_api import Page, sync_playwright


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def normalize_video_url(raw_url: str) -> tuple[str, str] | None:
    if raw_url.startswith("//"):
        raw_url = "https:" + raw_url
    match = re.search(r"/video/(BV[a-zA-Z0-9]+)", raw_url)
    if not match:
        return None
    video_id = match.group(1)
    clean_url = urlunsplit(("https", "www.bilibili.com", f"/video/{video_id}", "", ""))
    return video_id, clean_url


def parse_search_date(raw: str) -> str | None:
    value = raw.replace("·", "").strip()
    if re.fullmatch(r"\d{2}-\d{2}", value):
        return f"{date.today().year}-{value}"
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return value
    return None


def collect_page(
    page: Page,
    keyword: str,
    by_id: dict[str, dict[str, str | int]],
    target_year: int | None = None,
) -> tuple[int, list[int], list[str]]:
    """收集当前搜索页，返回本页新增数及可识别的发布年份。"""
    before = len(by_id)
    cards = page.locator(".bili-video-card")
    # 只读取搜索结果卡片，避免把页头、页脚中的公共视频链接误当成结果。
    items = cards.evaluate_all(
        """
        elements => elements.map(card => {
            const links = Array.from(card.querySelectorAll('a[href*="/video/BV"]'));
            const anchor = links[0];
            const heading = card.querySelector('h3');
            return {
                href: anchor ? (anchor.href || anchor.getAttribute('href') || '') : '',
                title: (
                    (heading && (heading.innerText || heading.textContent))
                    || (anchor && anchor.getAttribute('title'))
                    || ''
                ).trim(),
                date_text: (
                    card.querySelector('.bili-video-card__info--date')?.innerText
                    || card.querySelector('.bili-video-card__info--date')?.textContent
                    || ''
                ).trim()
            };
        })
        """
    )
    observed_years: list[int] = []
    raw_video_ids: list[str] = []
    for item in items:
        search_published_at = parse_search_date(str(item.get("date_text") or ""))
        if search_published_at:
            observed_years.append(int(search_published_at[:4]))
        if target_year and (not search_published_at or int(search_published_at[:4]) != target_year):
            continue
        raw_url = str(item.get("href") or "")
        normalized = normalize_video_url(raw_url)
        if not normalized:
            continue
        video_id, url = normalized
        raw_video_ids.append(video_id)
        title = str(item.get("title") or "").strip()
        title = re.sub(r"\s+", " ", title)
        current = by_id.get(video_id)
        if current is None:
            by_id[video_id] = {
                "keyword": keyword,
                "search_rank": len(by_id) + 1,
                "video_id": video_id,
                "title": title,
                "url": url,
                "search_published_at": search_published_at or "",
                "crawled_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            }
        elif len(title) > len(str(current["title"])):
            current["title"] = title
    return len(by_id) - before, observed_years, raw_video_ids


def crawl(
    keyword: str,
    max_results: int,
    channel: str,
    headed: bool,
    max_pages: int = 1,
    page_delay: float = 2.0,
    target_year: int | None = None,
) -> list[dict[str, str | int]]:
    """逐页搜索；max_results/max_pages 为 0 时表示不主动限制。"""
    by_id: dict[str, dict[str, str | int]] = {}
    seen_raw_ids: set[str] = set()
    empty_pages = 0
    page_number = 1

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel=channel, headless=not headed)
        try:
            page = browser.new_page(viewport={"width": 1400, "height": 900})
            while True:
                search_url = (
                    f"https://search.bilibili.com/video?keyword={quote(keyword)}"
                    f"&order=pubdate&page={page_number}"
                )
                response = page.goto(search_url, wait_until="domcontentloaded", timeout=30_000)
                page.wait_for_timeout(4_000)
                if response and response.status >= 400:
                    raise RuntimeError(f"搜索第 {page_number} 页 HTTP {response.status}")

                card_count = page.locator(".bili-video-card").count()
                if page_number == 1 and card_count == 0:
                    raise RuntimeError("B站视频搜索页没有加载结果卡片，可能需要稍后重试")
                added, observed_years, raw_video_ids = collect_page(
                    page, keyword, by_id, target_year
                )
                raw_added = sum(video_id not in seen_raw_ids for video_id in raw_video_ids)
                seen_raw_ids.update(raw_video_ids)
                print(f"搜索页 {page_number}：新增 {added} 条，累计 {len(by_id)} 条")
                empty_pages = empty_pages + 1 if raw_added == 0 else 0

                if max_results and len(by_id) >= max_results:
                    break
                if max_pages and page_number >= max_pages:
                    break
                # 连续两页没有新增，通常表示已到末页或平台重复返回末页。
                if empty_pages >= 2:
                    break
                if target_year and any(year < target_year for year in observed_years):
                    print(f"搜索结果已越过 {target_year} 年，停止继续翻页")
                    break

                page_number += 1
                if page_delay:
                    time.sleep(page_delay)
        finally:
            browser.close()

    rows = list(by_id.values())
    return rows[:max_results] if max_results else rows


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--keyword", required=True)
    parser.add_argument("--max-results", type=int, default=0, help="最多结果数，0 表示不限")
    parser.add_argument("--max-pages", type=int, default=0, help="最多搜索页数，0 表示直到无新增")
    parser.add_argument("--page-delay", type=float, default=2.0, help="翻页间隔秒数")
    parser.add_argument("--output", type=Path, default=Path("bilibili_results.csv"))
    parser.add_argument("--channel", choices=("msedge", "chrome"), default="msedge")
    parser.add_argument("--headed", action="store_true")
    parser.add_argument("--target-year", type=int, help="只保留该年份，并在搜索结果越过该年后停止")
    args = parser.parse_args()
    if args.max_results < 0 or args.max_pages < 0 or args.page_delay < 0:
        parser.error("max-results、max-pages 和 page-delay 不能小于 0")
    return args


def main() -> int:
    args = parse_args()
    try:
        rows = crawl(
            args.keyword,
            args.max_results,
            args.channel,
            args.headed,
            args.max_pages,
            args.page_delay,
            args.target_year,
        )
        if not rows:
            raise RuntimeError("搜索页没有发现 B 站视频链接")
        pd.DataFrame(rows).to_csv(args.output, index=False, encoding="utf-8-sig")
    except Exception as exc:
        print(f"运行失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    print(
        json.dumps(
            {"keyword": args.keyword, "count": len(rows), "output": str(args.output)},
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

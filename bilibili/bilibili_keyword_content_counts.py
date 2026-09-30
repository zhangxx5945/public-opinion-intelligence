"""统计多个关键词在 B 站 2025 年公开搜索结果中的视频和专栏条数。"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

from playwright.sync_api import Page, sync_playwright


YEAR = 2025
ROOT = Path(__file__).resolve().parent
DEFAULT_KEYWORDS = ROOT.parent / "xiaohongshu" / "keywords_2025.txt"
DEFAULT_OUTPUT = ROOT / "data" / "bilibili_keyword_content_counts_2025.csv"
DEFAULT_PROGRESS = ROOT / "data" / "bilibili_keyword_content_counts_2025_progress.json"
DATE_2025 = re.compile(r"(?<!\d)2025[-./]\d{1,2}[-./]\d{1,2}(?!\d)")
FULL_DATE = re.compile(r"(?<!\d)(20\d{2})[-./]\d{1,2}[-./]\d{1,2}(?!\d)")

SEARCH_TYPES = {
    "video": {
        "label": "视频",
        "url": "https://search.bilibili.com/video?keyword={keyword}&order=pubdate&page={page}",
        "anchor": 'a[href*="/video/BV"]',
        "id_re": re.compile(r"/video/(BV[A-Za-z0-9]+)"),
    },
    "article": {
        "label": "专栏（帖子）",
        "url": "https://search.bilibili.com/article?keyword={keyword}&order=pubdate&page={page}",
        "anchor": 'a[href*="/read/cv"]',
        "id_re": re.compile(r"/read/(cv\d+)"),
    },
}

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def load_keywords(path: Path) -> list[str]:
    return [line.strip().replace("+", " ") for line in path.read_text(encoding="utf-8-sig").splitlines() if line.strip()]


def anchor_records(page: Page, selector: str) -> list[dict[str, str]]:
    """从链接向上寻找包含发布日期的最小祖先，以适应搜索页样式变化。"""
    return page.locator(selector).evaluate_all(
        """
        elements => elements.map(a => {
          let node = a;
          let best = '';
          for (let i = 0; i < 7 && node; i += 1, node = node.parentElement) {
            const text = (node.innerText || node.textContent || '').replace(/\s+/g, ' ').trim();
            if (text.length > best.length) best = text;
            if (/20\d{2}[-./]\d{1,2}[-./]\d{1,2}/.test(text) && text.length < 1800) {
              best = text;
              break;
            }
          }
          return {href: a.href || a.getAttribute('href') || '', text: best};
        })
        """
    )


def collect_type(page: Page, keyword: str, kind: str, max_pages: int, wait_ms: int) -> dict:
    config = SEARCH_TYPES[kind]
    seen: dict[str, str] = {}
    counted_2025: set[str] = set()
    pages_scanned = 0
    stop_reason = "达到页数上限"
    consecutive_empty = 0
    consecutive_old = 0

    for page_number in range(1, max_pages + 1):
        url = config["url"].format(keyword=quote(keyword), page=page_number)
        response = page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        page.wait_for_timeout(wait_ms)
        if response and response.status >= 400:
            stop_reason = f"HTTP {response.status}"
            break

        records = anchor_records(page, config["anchor"])
        new_ids = 0
        page_years: list[int] = []
        for record in records:
            match = config["id_re"].search(record["href"])
            if not match:
                continue
            item_id = match.group(1)
            text = record["text"]
            for year_match in FULL_DATE.finditer(text):
                page_years.append(int(year_match.group(1)))
            if item_id in seen:
                if len(text) > len(seen[item_id]):
                    seen[item_id] = text
                continue
            seen[item_id] = text
            new_ids += 1
            if DATE_2025.search(text):
                counted_2025.add(item_id)

        pages_scanned = page_number
        consecutive_empty = consecutive_empty + 1 if new_ids == 0 else 0
        if page_years and max(page_years) < YEAR:
            consecutive_old += 1
        else:
            consecutive_old = 0

        print(
            f"{keyword} | {config['label']} | 第 {page_number} 页 | "
            f"新增 {new_ids} | 2025累计 {len(counted_2025)}"
        )
        if consecutive_empty >= 2:
            stop_reason = "连续两页无新增结果"
            break
        if consecutive_old >= 2:
            stop_reason = "连续两页全部早于2025年"
            break

    return {
        "count_2025": len(counted_2025),
        "unique_results_scanned": len(seen),
        "pages_scanned": pages_scanned,
        "stop_reason": stop_reason,
    }


def save(rows: list[dict], output: Path, progress: Path, keywords: list[str]) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    columns = [
        "platform", "keyword", "year", "video_count", "article_post_count",
        "post_definition", "video_pages", "article_pages", "video_stop_reason",
        "article_stop_reason", "count_scope", "updated_at",
    ]
    with output.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)
    progress.write_text(
        json.dumps(
            {
                "keywords": keywords,
                "completed": [row["keyword"] for row in rows],
                "results": rows,
                "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--keywords", type=Path, default=DEFAULT_KEYWORDS)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--progress", type=Path, default=DEFAULT_PROGRESS)
    parser.add_argument("--max-pages", type=int, default=50)
    parser.add_argument("--wait-ms", type=int, default=1800)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    keywords = load_keywords(args.keywords)
    rows: list[dict] = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="msedge", headless=True)
        try:
            page = browser.new_page(viewport={"width": 1440, "height": 1000})
            for keyword in keywords:
                video = collect_type(page, keyword, "video", args.max_pages, args.wait_ms)
                article = collect_type(page, keyword, "article", args.max_pages, args.wait_ms)
                rows.append(
                    {
                        "platform": "B站",
                        "keyword": keyword,
                        "year": YEAR,
                        "video_count": video["count_2025"],
                        "article_post_count": article["count_2025"],
                        "post_definition": "专栏；动态搜索入口当前不可用，未计入",
                        "video_pages": video["pages_scanned"],
                        "article_pages": article["pages_scanned"],
                        "video_stop_reason": video["stop_reason"],
                        "article_stop_reason": article["stop_reason"],
                        "count_scope": "2025年公开搜索可返回结果；同一内容按BV号或cv号仅计一次",
                        "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                    }
                )
                save(rows, args.output, args.progress, keywords)
        finally:
            browser.close()
    print(f"完成 {len(rows)}/{len(keywords)}，结果：{args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

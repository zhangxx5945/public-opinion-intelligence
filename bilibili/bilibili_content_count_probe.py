"""检查B站视频、专栏和动态搜索页的公开卡片结构。"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from urllib.parse import quote

from playwright.sync_api import sync_playwright


KEYWORD = "天津 养老服务"
OUTPUT = Path(__file__).resolve().parent / "bilibili_content_count_probe.json"
SEARCH_TYPES = {
    "video": "https://search.bilibili.com/video?keyword={keyword}&order=pubdate&page=1",
    "article": "https://search.bilibili.com/article?keyword={keyword}&page=1",
    "dynamic": "https://search.bilibili.com/dynamic?keyword={keyword}&page=1",
}

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def main() -> int:
    results = {}
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="msedge", headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        for content_type, template in SEARCH_TYPES.items():
            url = template.format(keyword=quote(KEYWORD))
            response = page.goto(url, wait_until="domcontentloaded", timeout=60_000)
            page.wait_for_timeout(4_000)
            selectors = {
                "video_links": 'a[href*="/video/BV"]',
                "article_links": 'a[href*="/read/cv"]',
                "opus_links": 'a[href*="/opus/"]',
                "dynamic_links": 'a[href*="t.bilibili.com/"]',
                "cards": ".bili-video-card, .video-item, .article-item, .dynamic-card, .search-content-item",
            }
            counts = {name: page.locator(selector).count() for name, selector in selectors.items()}
            samples = []
            cards = page.locator(selectors["cards"])
            for index in range(min(cards.count(), 5)):
                samples.append(" ".join(cards.nth(index).inner_text().split())[:400])
            body_text = " ".join(page.locator("body").inner_text().split())
            results[content_type] = {
                "requested_url": url,
                "final_url": page.url,
                "status": response.status if response else None,
                "title": page.title(),
                "counts": counts,
                "samples": samples,
                "body_preview": body_text[:800],
            }
        browser.close()
    OUTPUT.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(results, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

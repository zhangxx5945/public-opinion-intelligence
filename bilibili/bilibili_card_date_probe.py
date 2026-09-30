"""诊断 B站搜索卡片公开展示的发布时间字段。"""

from __future__ import annotations

import json
import sys
from urllib.parse import quote

from playwright.sync_api import sync_playwright


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def main() -> None:
    keyword = "天津 养老服务"
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="msedge", headless=True)
        page = browser.new_page(viewport={"width": 1400, "height": 900})
        page.goto(
            f"https://search.bilibili.com/video?keyword={quote(keyword)}&order=pubdate&page=1",
            wait_until="domcontentloaded",
            timeout=30_000,
        )
        page.wait_for_timeout(4_000)
        rows = page.locator(".bili-video-card").evaluate_all(
            """
            cards => cards.slice(0, 8).map(card => ({
                text: (card.innerText || '').trim(),
                date_nodes: [...card.querySelectorAll('[class*="date"], [class*="time"]')]
                    .map(el => ({className: el.className, text: (el.innerText || el.textContent || '').trim()}))
            }))
            """
        )
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        browser.close()


if __name__ == "__main__":
    main()

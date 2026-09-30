"""Probe logged-in Toutiao desktop search and scrolling without saving content."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from urllib.parse import quote

from playwright.sync_api import sync_playwright


PROFILE_DIR = Path(__file__).resolve().parent / ".toutiao_profile"
SELECTORS = (
    "div.l-card",
    "a.l-card-title",
    'a[href*="/article/"]',
    'a[href*="/video/"]',
    'a[href*="/search/jump"]',
)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def counts(page: object) -> dict[str, int]:
    return {selector: page.locator(selector).count() for selector in SELECTORS}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--keyword", default="天津 养老服务")
    parser.add_argument("--scrolls", type=int, default=8)
    parser.add_argument("--wait", type=float, default=2.5)
    args = parser.parse_args()

    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR.resolve()),
            channel="msedge",
            headless=True,
            viewport={"width": 1440, "height": 1000},
            locale="zh-CN",
        )
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(
            f"https://www.toutiao.com/search/?keyword={quote(args.keyword)}",
            wait_until="domcontentloaded",
            timeout=60_000,
        )
        page.wait_for_timeout(8_000)
        result = {
            "url": page.url,
            "title": page.title(),
            "body_preview": page.locator("body").inner_text()[:2500],
            "steps": [{"scroll": 0, "height": page.evaluate("document.body.scrollHeight"), "counts": counts(page)}],
        }
        result["pagination_links"] = []
        anchors = page.locator("a")
        for index in range(anchors.count()):
            anchor = anchors.nth(index)
            text_value = anchor.inner_text().strip()
            if not re.fullmatch(r"(?:\d{1,3}|上一页|下一页)", text_value):
                continue
            result["pagination_links"].append(
                {
                    "text": text_value,
                    "href": anchor.get_attribute("href") or "",
                    "class": anchor.get_attribute("class") or "",
                }
            )
        links = page.locator('a[href*="/search/jump"]')
        result["jump_link_samples"] = []
        for index in range(min(links.count(), 40)):
            link = links.nth(index)
            parent = link.locator("..")
            result["jump_link_samples"].append(
                {
                    "index": index,
                    "text": link.inner_text().strip()[:240],
                    "class": link.get_attribute("class") or "",
                    "parent_class": parent.get_attribute("class") or "",
                }
            )
        result["article_container_samples"] = []
        candidates = page.locator("a.text-ellipsis.text-underline-hover")
        for index in range(candidates.count()):
            link = candidates.nth(index)
            if "text-xl" not in (link.locator("..").get_attribute("class") or ""):
                continue
            node = link
            levels = []
            for level in range(5):
                levels.append(
                    {
                        "level": level,
                        "tag": node.evaluate("element => element.tagName"),
                        "class": node.get_attribute("class") or "",
                        "text": node.inner_text().strip()[:700],
                    }
                )
                node = node.locator("..")
            result["article_container_samples"].append(levels)
            if len(result["article_container_samples"]) >= 2:
                break
        for index in range(1, args.scrolls + 1):
            page.mouse.wheel(0, 3000)
            page.wait_for_timeout(int(args.wait * 1000))
            result["steps"].append(
                {
                    "scroll": index,
                    "height": page.evaluate("document.body.scrollHeight"),
                    "counts": counts(page),
                }
            )
        context.close()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

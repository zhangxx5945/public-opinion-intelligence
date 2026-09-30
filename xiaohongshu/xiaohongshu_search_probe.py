"""检查小红书搜索页结构和登录状态，不采集完整内容。"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from urllib.parse import quote

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

from xiaohongshu_login import EDGE_PATHS, PROFILE_DIR


OUTPUT = Path(__file__).resolve().parent / "search_probe.json"
KEYWORD = "天津 养老服务"

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def main() -> int:
    edge_path = next((path for path in EDGE_PATHS if path.exists()), None)
    if edge_path is None:
        print("未找到 Microsoft Edge。", file=sys.stderr)
        return 1
    url = f"https://www.xiaohongshu.com/search_result?keyword={quote(KEYWORD)}&source=web_search_result_notes"

    try:
        with sync_playwright() as playwright:
            context = playwright.chromium.launch_persistent_context(
                user_data_dir=str(PROFILE_DIR),
                executable_path=str(edge_path),
                headless=True,
                viewport={"width": 1440, "height": 1000},
            )
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=60_000)
            page.wait_for_timeout(8_000)
            selectors = (
                "section.note-item",
                ".note-item",
                "a[href*='/explore/']",
                "a[href*='/search_result/']",
            )
            selector_counts = {selector: page.locator(selector).count() for selector in selectors}
            links = page.locator("a[href*='/explore/']")
            samples = []
            for index in range(min(5, links.count())):
                link = links.nth(index)
                samples.append(
                    {
                        "href": link.get_attribute("href") or "",
                        "text_preview": " ".join(link.inner_text().split())[:120],
                    }
                )
            card_samples = []
            cards = page.locator("section.note-item")
            for index in range(min(5, cards.count())):
                card = cards.nth(index)
                card_links = card.locator("a")
                card_samples.append(
                    {
                        "text_preview": " ".join(card.inner_text().split())[:300],
                        "rank_estimator_title_candidates": card.evaluate(
                            """element => Array.from(element.querySelectorAll(
                                'a.title span, .title span, a[href*="/search_result/"] span'
                            )).map(node => node.textContent.trim()).filter(Boolean)"""
                        ),
                        "short_text_elements": card.evaluate(
                            """element => Array.from(element.querySelectorAll('*'))
                                .map(node => ({
                                    tag: node.tagName.toLowerCase(),
                                    class_name: node.className || '',
                                    text: (node.childNodes.length === 1 && node.firstChild.nodeType === Node.TEXT_NODE)
                                        ? node.textContent.trim() : ''
                                }))
                                .filter(item => item.text && item.text.length <= 30)
                                .slice(0, 30)"""
                        ),
                        "links": [
                            card_links.nth(link_index).get_attribute("href") or ""
                            for link_index in range(min(4, card_links.count()))
                        ],
                    }
                )
            body_text = " ".join(page.locator("body").inner_text().split())
            result = {
                "keyword": KEYWORD,
                "url": page.url,
                "title": page.title(),
                "selector_counts": selector_counts,
                "login_text_visible": any(marker in body_text[:2_000] for marker in ("登录后", "扫码登录", "手机号登录")),
                "body_preview": body_text[:500],
                "samples": samples,
                "card_samples": card_samples,
            }
            context.close()
    except PlaywrightError as exc:
        print(f"搜索探针失败：{exc}", file=sys.stderr)
        return 1

    OUTPUT.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

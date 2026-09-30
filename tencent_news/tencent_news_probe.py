"""Inspect Tencent News public search DOM and network request names."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from urllib.parse import quote

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright


PROFILE_DIR = Path(__file__).resolve().parent / ".tencent_news_profile"

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--keyword", default="天津 养老服务")
    parser.add_argument("--headed", action="store_true")
    parser.add_argument("--wait", type=float, default=10.0)
    parser.add_argument("--page", type=int, default=1)
    args = parser.parse_args()

    requests: list[dict[str, str]] = []
    responses: list[dict[str, object]] = []
    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR.resolve()),
            channel="msedge",
            headless=not args.headed,
            viewport={"width": 1440, "height": 1000},
            locale="zh-CN",
        )
        page = context.pages[0] if context.pages else context.new_page()

        def on_request(request: object) -> None:
            url = request.url
            lowered = url.lower()
            if any(value in lowered for value in ("search", "api", "graphql", "query")):
                requests.append(
                    {
                        "url": url,
                        "method": request.method,
                        "post_data": (request.post_data or "")[:2000],
                    }
                )

        def on_response(response: object) -> None:
            url = response.url
            lowered = url.lower()
            if any(value in lowered for value in ("search", "api", "graphql", "query")):
                responses.append({"url": url, "status": response.status})

        page.on("request", on_request)
        page.on("response", on_response)
        error = ""
        try:
            page.goto(
                f"https://news.qq.com/search?query={quote(args.keyword)}&page={args.page}",
                wait_until="domcontentloaded",
                timeout=60_000,
            )
            page.wait_for_timeout(int(args.wait * 1000))
        except PlaywrightError as exc:
            error = str(exc)

        links = page.locator("a")
        link_samples = []
        for index in range(min(links.count(), 80)):
            link = links.nth(index)
            text = link.inner_text().strip()
            href = link.get_attribute("href") or ""
            if text or href:
                link_samples.append(
                    {
                        "text": text[:240],
                        "href": href[:500],
                        "class": link.get_attribute("class") or "",
                    }
                )
        result = {
            "error": error,
            "url": page.url,
            "title": page.title(),
            "body_preview": page.locator("body").inner_text()[:7000],
            "link_samples": link_samples,
            "requests": requests[-80:],
            "responses": responses[-80:],
        }
        context.close()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""诊断抖音正常网页搜索产生的公开 JSON 响应，不保存 Cookie 或请求头。"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import quote, urlsplit

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Response, sync_playwright


PROJECT_DIR = Path(__file__).resolve().parent
PROFILE_DIR = PROJECT_DIR / ".douyin_profile"
OUTPUT = PROJECT_DIR / "douyin_search_response_probe.json"
KEYWORD = "天津 文化活动"

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def iter_objects(value: Any) -> Iterable[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from iter_objects(child)
    elif isinstance(value, list):
        for child in value:
            yield from iter_objects(child)


def sample_item(obj: dict[str, Any]) -> dict[str, Any] | None:
    item = obj.get("aweme_info") if isinstance(obj.get("aweme_info"), dict) else obj
    content_id = item.get("aweme_id") or item.get("item_id")
    if not content_id:
        return None
    author = item.get("author") if isinstance(item.get("author"), dict) else {}
    statistics = item.get("statistics") if isinstance(item.get("statistics"), dict) else {}
    return {
        "content_id": str(content_id),
        "description": str(item.get("desc") or item.get("title") or "")[:300],
        "create_time": item.get("create_time"),
        "author": str(author.get("nickname") or "")[:100],
        "digg_count": statistics.get("digg_count"),
        "comment_count": statistics.get("comment_count"),
        "share_count": statistics.get("share_count"),
    }


def decode_json_documents(raw_text: str) -> list[Any]:
    """从普通 JSON 或抖音分块流响应中依次解析 JSON 文档。"""
    decoder = json.JSONDecoder()
    documents: list[Any] = []
    cursor = 0
    while cursor < len(raw_text):
        object_at = raw_text.find("{", cursor)
        array_at = raw_text.find("[", cursor)
        starts = [index for index in (object_at, array_at) if index >= 0]
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


def main() -> int:
    responses: list[dict[str, Any]] = []
    items: dict[str, dict[str, Any]] = {}

    def handle_response(response: Response) -> None:
        url_lower = response.url.lower()
        if "search" not in url_lower and "aweme" not in url_lower:
            return
        content_type = (response.headers.get("content-type") or "").lower()
        if "json" not in content_type and response.request.resource_type not in {"xhr", "fetch"}:
            return
        summary: dict[str, Any] = {
            "path": urlsplit(response.url).path,
            "status": response.status,
            "resource_type": response.request.resource_type,
            "content_type": content_type,
            "items_found": 0,
        }
        try:
            raw_text = response.text()
            payloads = decode_json_documents(raw_text)
            if not payloads:
                summary["parse_status"] = "no_json_start"
                summary["body_prefix"] = raw_text[:160]
                responses.append(summary)
                return
        except (PlaywrightError, ValueError) as exc:
            summary["parse_status"] = f"error:{type(exc).__name__}"
            summary["body_prefix"] = locals().get("raw_text", "")[:160]
            responses.append(summary)
            return
        found = 0
        for payload in payloads:
            for obj in iter_objects(payload):
                sample = sample_item(obj)
                if not sample:
                    continue
                items[sample["content_id"]] = sample
                found += 1
        summary.update(
            {
                "parse_status": "ok",
                "json_documents": len(payloads),
                "top_level_keys": list(payloads[0])[:30]
                if isinstance(payloads[0], dict)
                else [],
                "items_found": found,
            }
        )
        responses.append(summary)

    try:
        with sync_playwright() as playwright:
            context = playwright.chromium.launch_persistent_context(
                user_data_dir=str(PROFILE_DIR),
                channel="msedge",
                headless=False,
                viewport={"width": 1440, "height": 900},
            )
            page = context.pages[0] if context.pages else context.new_page()
            page.on("response", handle_response)
            page.goto(
                f"https://www.douyin.com/search/{quote(KEYWORD)}",
                wait_until="domcontentloaded",
                timeout=60_000,
            )
            page.wait_for_timeout(8_000)
            search_input = page.locator('[data-e2e="searchbar-input"]')
            search_button = page.locator('[data-e2e="searchbar-button"]')
            if search_input.count() and search_button.count():
                search_input.fill(KEYWORD)
                search_button.click()
                page.wait_for_timeout(10_000)
            for _ in range(2):
                page.mouse.wheel(0, 2_000)
                page.wait_for_timeout(2_000)
            dom_summary = page.evaluate(
                """() => ({
                    links: [...document.querySelectorAll('a[href]')].slice(0, 200).map(a => ({
                        href: a.getAttribute('href'),
                        text: (a.innerText || '').trim().slice(0, 120),
                        e2e: a.getAttribute('data-e2e')
                    })),
                    e2e: [...new Set([...document.querySelectorAll('[data-e2e]')]
                        .map(el => el.getAttribute('data-e2e')))].slice(0, 200)
                })"""
            )
            result = {
                "keyword": KEYWORD,
                "page_title": page.title(),
                "final_url": page.url,
                "response_summaries": responses,
                "unique_items_found": len(items),
                "item_samples": list(items.values())[:20],
                "dom_summary": dom_summary,
            }
            OUTPUT.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            print(
                json.dumps(
                    {
                        "keyword": KEYWORD,
                        "page_title": result["page_title"],
                        "unique_items_found": result["unique_items_found"],
                        "output": str(OUTPUT),
                    },
                    ensure_ascii=False,
                )
            )
            context.close()
    except PlaywrightError as exc:
        print(f"运行失败：{exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

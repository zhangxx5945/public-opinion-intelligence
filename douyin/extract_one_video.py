"""提取单个公开抖音视频页面的基础信息。"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from playwright.sync_api import Page, sync_playwright


DEFAULT_TIMEOUT_MS = 30_000
DEFAULT_WAIT_MS = 5_000

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def get_meta(page: Page, selector: str) -> str | None:
    element = page.locator(selector).first
    if element.count() == 0:
        return None
    value = element.get_attribute("content")
    return value.strip() if value else None


def extract_video_id(url: str) -> str | None:
    parsed = urlparse(url)
    modal_ids = parse_qs(parsed.query).get("modal_id")
    if modal_ids:
        return modal_ids[0]
    match = re.search(r"/video/(\d+)", parsed.path)
    return match.group(1) if match else None


def timestamp_to_text(raw_value: str) -> str | None:
    try:
        timestamp = int(raw_value)
        if timestamp > 10_000_000_000:
            timestamp /= 1_000
        value = datetime.fromtimestamp(timestamp, tz=timezone.utc).astimezone()
        return value.isoformat(timespec="seconds")
    except (ValueError, TypeError, OverflowError, OSError):
        return None


def extract_publish_time(html: str) -> tuple[str | None, str | None, str | None]:
    timestamp_patterns = (
        r'"createTime"\s*:\s*"?(\d{10,13})"?',
        r'"create_time"\s*:\s*"?(\d{10,13})"?',
        r'"publishTime"\s*:\s*"?(\d{10,13})"?',
        r'"publish_time"\s*:\s*"?(\d{10,13})"?',
    )
    for pattern in timestamp_patterns:
        match = re.search(pattern, html)
        if match:
            raw_value = match.group(1)
            return raw_value, timestamp_to_text(raw_value), "second"

    iso_patterns = (
        r'"datePublished"\s*:\s*"([^"]+)"',
        r'"uploadDate"\s*:\s*"([^"]+)"',
    )
    for pattern in iso_patterns:
        match = re.search(pattern, html)
        if match:
            raw_value = match.group(1)
            return raw_value, raw_value, "source_provided"

    return None, None, None


def extract_publish_date_from_text(text: str) -> tuple[str | None, str | None]:
    match = re.search(
        r"于\s*((?:19|20)\d{2})(\d{2})(\d{2})\s*发布(?:在抖音)?",
        text,
    )
    if not match:
        return None, None

    raw_date = "".join(match.groups())
    try:
        parsed_date = datetime.strptime(raw_date, "%Y%m%d")
    except ValueError:
        return None, None
    return raw_date, parsed_date.strftime("%Y-%m-%d")


def clean_title(title: str | None) -> str | None:
    if not title:
        return None
    title = re.sub(r"\s+", " ", title).strip()

    publish_match = re.search(r"于(?:19|20)\d{6}发布(?:在抖音)?", title)
    if publish_match:
        prefix = title[: publish_match.start()]
        if " - " in prefix:
            prefix = prefix.rsplit(" - ", 1)[0]
        title = prefix.strip()

    for suffix in (" - 抖音", "_抖音"):
        if title.endswith(suffix):
            title = title[: -len(suffix)].strip()
    return title or None


def extract_video(
    url: str,
    *,
    channel: str = "chrome",
    headed: bool = False,
    timeout_ms: int = DEFAULT_TIMEOUT_MS,
    wait_ms: int = DEFAULT_WAIT_MS,
) -> dict[str, Any]:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel=channel, headless=not headed)
        try:
            page = browser.new_page(viewport={"width": 1400, "height": 900})
            response = page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=timeout_ms,
            )
            page.wait_for_timeout(wait_ms)

            final_url = page.url
            html = page.content()
            metadata = {
                "page_title": page.title(),
                "og_title": get_meta(page, 'meta[property="og:title"]'),
                "og_description": get_meta(page, 'meta[property="og:description"]'),
                "description": get_meta(page, 'meta[name="description"]'),
            }
            raw_title = next((value for value in metadata.values() if value), None)

            published_raw, published_at, time_precision = extract_publish_time(html)
            if not published_at:
                searchable_text = " ".join(value for value in metadata.values() if value)
                published_raw, published_at = extract_publish_date_from_text(searchable_text)
                if published_at:
                    time_precision = "date"

            return {
                "video_id": extract_video_id(final_url) or extract_video_id(url),
                "title": clean_title(raw_title),
                "published_at": published_at,
                "published_raw": published_raw,
                "time_precision": time_precision,
                "original_url": url,
                "final_url": final_url,
                "http_status": response.status if response else None,
            }
        finally:
            browser.close()


def append_result(output_path: Path, result: dict[str, Any]) -> tuple[bool, int]:
    """追加抓取结果；相同视频 ID（或网址）不会重复写入。"""
    if output_path.exists():
        saved_data = json.loads(output_path.read_text(encoding="utf-8"))
        results = saved_data if isinstance(saved_data, list) else [saved_data]
    else:
        results = []

    result_key = result.get("video_id") or result.get("final_url")
    existing_keys = {
        item.get("video_id") or item.get("final_url")
        for item in results
        if isinstance(item, dict)
    }
    if result_key in existing_keys:
        return False, len(results)

    results.append(result)
    temp_path = output_path.with_suffix(output_path.suffix + ".tmp")
    temp_path.write_text(
        json.dumps(results, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temp_path.replace(output_path)
    return True, len(results)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url", help="抖音视频页面网址")
    parser.add_argument("--channel", default="chrome", choices=("chrome", "msedge"))
    parser.add_argument("--headed", action="store_true", help="显示浏览器窗口")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_MS)
    parser.add_argument("--wait", type=int, default=DEFAULT_WAIT_MS)
    parser.add_argument("--output", type=Path, help="追加保存到指定 JSON 文件")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        result = extract_video(
            args.url,
            channel=args.channel,
            headed=args.headed,
            timeout_ms=args.timeout,
            wait_ms=args.wait,
        )
        if args.output:
            append_result(args.output, result)
    except Exception as exc:
        error = {"error": type(exc).__name__, "message": str(exc)}
        print(json.dumps(error, ensure_ascii=False, indent=2), file=sys.stderr)
        return 1

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

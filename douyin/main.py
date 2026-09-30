"""按关键词和日期范围采集公开抖音搜索结果并导出表格。"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import parse_qs, quote, urljoin, urlparse

import pandas as pd
from bs4 import BeautifulSoup
from dateutil import parser as date_parser
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page, Response, sync_playwright

from extract_one_video import (
    clean_title,
    extract_publish_date_from_text,
    extract_publish_time,
    extract_video_id,
    get_meta,
)


RESULT_COLUMNS = [
    "keyword",
    "search_rank",
    "content_id",
    "content_type",
    "title",
    "published_at",
    "published_raw",
    "time_precision",
    "in_date_range",
    "original_url",
    "final_url",
    "http_status",
    "crawled_at",
    "status",
    "error",
]
BLOCK_MARKERS = (
    "验证中间页",
    "安全验证",
    "请完成验证",
    "完成验证后继续",
    "拖动滑块",
    "滑块验证",
    "验证码",
    "点击验证",
    "拖动拼图",
    "访问过于频繁",
    "访问频繁",
    "请求过于频繁",
    "当前访问存在异常",
    "操作过于频繁",
)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


class AccessBlockedError(RuntimeError):
    """平台要求验证或限制访问时停止采集。"""


def configure_logging(log_path: Path) -> logging.Logger:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("douyin_crawler")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    formatter = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s")
    file_handler = logging.FileHandler(log_path, mode="w", encoding="utf-8")
    stream_handler = logging.StreamHandler(sys.stdout)
    file_handler.setFormatter(formatter)
    stream_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    logger.addHandler(stream_handler)
    return logger


def parse_cli_date(value: str) -> date:
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as exc:
        raise argparse.ArgumentTypeError("日期必须是 YYYY-MM-DD 格式") from exc


def content_identity(url: str) -> tuple[str | None, str | None]:
    parsed = urlparse(url)
    modal_id = parse_qs(parsed.query).get("modal_id")
    if modal_id:
        return modal_id[0], "video"
    match = re.search(r"/(video|note)/(\d+)", parsed.path)
    if match:
        return match.group(2), match.group(1)
    return extract_video_id(url), "video"


def normalize_candidate_url(raw_url: str) -> str | None:
    raw_url = raw_url.replace("\\/", "/").replace("&amp;", "&").strip()
    if not raw_url or raw_url.startswith(("javascript:", "data:")):
        return None
    absolute = urljoin("https://www.douyin.com", raw_url)
    content_id, content_type = content_identity(absolute)
    if not content_id:
        return None
    if "modal_id=" in absolute:
        return f"https://www.douyin.com/jingxuan?modal_id={content_id}"
    return f"https://www.douyin.com/{content_type}/{content_id}"


def collect_candidate_urls(page: Page) -> list[str]:
    raw_urls: list[str] = page.eval_on_selector_all(
        "a[href]", "elements => elements.map(element => element.getAttribute('href'))"
    )
    html = page.content().replace("\\/", "/")
    raw_urls.extend(re.findall(r'https?://www\.douyin\.com/(?:video|note)/\d+[^"\s<]*', html))
    raw_urls.extend(re.findall(r'/(?:video|note)/\d+', html))
    raw_urls.extend(
        f"https://www.douyin.com/jingxuan?modal_id={value}"
        for value in re.findall(r'modal_id(?:=|%3D|\\?"?\s*:\s*"?)(\d+)', html)
    )

    results: list[str] = []
    seen: set[str] = set()
    for raw_url in raw_urls:
        normalized = normalize_candidate_url(raw_url)
        if normalized and normalized not in seen:
            seen.add(normalized)
            results.append(normalized)
    return results


def decode_json_documents(raw_text: str) -> list[Any]:
    """解析普通 JSON，以及抖音搜索接口返回的多段流式 JSON。"""
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


def search_response_items(response: Response) -> list[dict[str, Any]]:
    """从浏览器正常搜索响应中提取公开作品 ID，不读取请求头或 Cookie。"""
    if urlparse(response.url).path != "/aweme/v1/web/general/search/stream/":
        return []
    try:
        documents = decode_json_documents(response.text())
    except (PlaywrightError, ValueError):
        return []

    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for document in documents:
        for obj in iter_json_objects(document):
            item = obj.get("aweme_info") if isinstance(obj.get("aweme_info"), dict) else obj
            content_id = item.get("aweme_id")
            if not content_id or str(content_id) in seen:
                continue
            seen.add(str(content_id))
            create_time = item.get("create_time")
            response_published_at = None
            if isinstance(create_time, (int, float)) and create_time > 0:
                try:
                    response_published_at = datetime.fromtimestamp(
                        create_time
                    ).astimezone().date().isoformat()
                except (OSError, OverflowError, ValueError):
                    pass
            items.append(
                {
                    "content_id": str(content_id),
                    "content_type": "video",
                    "url": f"https://www.douyin.com/video/{content_id}",
                    "response_title": str(item.get("desc") or "").strip() or None,
                    "response_published_at": response_published_at,
                }
            )
    return items


def page_text_for_block_check(page: Page) -> str:
    try:
        return f"{page.title()} {page.locator('body').inner_text(timeout=2_000)[:5_000]}".lower()
    except Exception:
        return page.title().lower()


def ensure_not_blocked(page: Page, http_status: int | None) -> None:
    if http_status in (403, 429):
        raise AccessBlockedError(f"HTTP {http_status}，平台拒绝或限制访问")
    text = page_text_for_block_check(page)
    marker = next((item for item in BLOCK_MARKERS if item.lower() in text), None)
    if marker:
        raise AccessBlockedError(f"页面出现“{marker}”，需要人工验证或降低访问频率")


def ensure_access_with_manual_pause(
    page: Page,
    http_status: int | None,
    manual_verify_timeout: float,
    headless: bool,
    logger: logging.Logger,
) -> None:
    """可见模式下发现验证页时暂停，等待用户在真实浏览器窗口中完成验证。"""
    try:
        ensure_not_blocked(page, http_status)
        return
    except AccessBlockedError as exc:
        if headless or manual_verify_timeout <= 0:
            raise
        logger.warning(
            "%s；已暂停自动操作，请在 %.0f 秒内用鼠标完成页面验证",
            exc,
            manual_verify_timeout,
        )
        try:
            page.bring_to_front()
        except PlaywrightError:
            pass

    deadline = time.monotonic() + manual_verify_timeout
    while time.monotonic() < deadline:
        page.wait_for_timeout(1_000)
        try:
            # 原 HTTP 状态只属于验证前的导航；此处以当前可见页面为准。
            ensure_not_blocked(page, None)
        except AccessBlockedError:
            continue
        logger.info("检测到验证页已解除，恢复自动采集")
        return
    raise AccessBlockedError(
        f"人工验证等待超时（{manual_verify_timeout:.0f} 秒），已保留检查点"
    )


def discover_candidates(
    page: Page,
    keyword: str,
    max_results: int,
    max_scrolls: int,
    delay_seconds: float,
    timeout_ms: int,
    initial_wait_seconds: float,
    manual_verify_timeout: float,
    headless: bool,
    logger: logging.Logger,
) -> list[dict[str, Any]]:
    response_candidates: dict[str, dict[str, Any]] = {}

    def handle_search_response(response: Response) -> None:
        for item in search_response_items(response):
            response_candidates.setdefault(item["content_id"], item)

    page.on("response", handle_search_response)
    search_url = f"https://www.douyin.com/search/{quote(keyword)}"
    logger.info("打开搜索页：%s", search_url)
    response = page.goto(search_url, wait_until="domcontentloaded", timeout=timeout_ms)
    logger.info("等待 %.0f 秒，首次运行可在浏览器中正常登录", initial_wait_seconds)
    page.wait_for_timeout(int(initial_wait_seconds * 1_000))
    ensure_access_with_manual_pause(
        page,
        response.status if response else None,
        manual_verify_timeout,
        headless,
        logger,
    )

    search_input = page.locator('[data-e2e="searchbar-input"]')
    search_button = page.locator('[data-e2e="searchbar-button"]')
    if search_input.count() and search_button.count():
        logger.info("登录等待结束，重新提交关键词搜索：%s", keyword)
        search_input.fill(keyword)
        search_button.click()
        page.wait_for_timeout(8_000)
        ensure_access_with_manual_pause(
            page, None, manual_verify_timeout, headless, logger
        )
    else:
        logger.warning("未找到搜索框或搜索按钮，将直接检查当前页面结果")

    urls: list[str] = []
    seen: set[str] = set()
    for scroll_index in range(max_scrolls + 1):
        for item in response_candidates.values():
            if item["content_id"] not in seen:
                seen.add(item["content_id"])
                urls.append(item["url"])
                if len(urls) >= max_results:
                    break
        for url in collect_candidate_urls(page):
            content_id, _ = content_identity(url)
            if content_id and content_id not in seen:
                seen.add(content_id)
                urls.append(url)
                if len(urls) >= max_results:
                    break
        logger.info("搜索滚动 %s/%s，已发现 %s 条候选", scroll_index, max_scrolls, len(urls))
        if len(urls) >= max_results or scroll_index == max_scrolls:
            break
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_timeout(int(delay_seconds * 1_000))
        ensure_access_with_manual_pause(
            page, None, manual_verify_timeout, headless, logger
        )

    if not urls:
        title = page.title().strip() or "无标题"
        raise AccessBlockedError(
            f"搜索页和公开搜索响应均未返回候选内容（页面标题：{title}）。"
            "若页面要求验证，请先正常完成验证；否则该次搜索可能被平台限制或确实无公开结果"
        )

    candidates = []
    for rank, url in enumerate(urls[:max_results], start=1):
        content_id, content_type = content_identity(url)
        candidate = {
            "keyword": keyword,
            "search_rank": rank,
            "content_id": content_id,
            "content_type": content_type,
            "url": url,
            "crawled_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        }
        if content_id and content_id in response_candidates:
            candidate.update(response_candidates[content_id])
        candidates.append(candidate)
    return candidates


def iter_json_objects(value: Any) -> Iterable[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from iter_json_objects(child)
    elif isinstance(value, list):
        for child in value:
            yield from iter_json_objects(child)


def extract_json_ld(html: str) -> list[dict[str, Any]]:
    soup = BeautifulSoup(html, "html.parser")
    objects: list[dict[str, Any]] = []
    for script in soup.select('script[type="application/ld+json"]'):
        if not script.string:
            continue
        try:
            parsed = json.loads(script.string)
        except (json.JSONDecodeError, TypeError):
            continue
        objects.extend(iter_json_objects(parsed))
    return objects


def first_json_ld_value(objects: list[dict[str, Any]], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        for item in objects:
            value = item.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def normalize_published_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date_parser.parse(value).date()
    except (ValueError, TypeError, OverflowError):
        return None


def extract_detail(
    page: Page,
    candidate: dict[str, Any],
    start_date: date,
    end_date: date,
    timeout_ms: int,
    manual_verify_timeout: float,
    headless: bool,
    logger: logging.Logger,
) -> dict[str, Any]:
    url = candidate["url"]
    response = page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
    page.wait_for_timeout(4_000)
    status_code = response.status if response else None
    ensure_access_with_manual_pause(
        page, status_code, manual_verify_timeout, headless, logger
    )

    html = page.content()
    json_ld = extract_json_ld(html)
    metadata = {
        "og_title": get_meta(page, 'meta[property="og:title"]'),
        "json_ld_title": first_json_ld_value(
            json_ld, ("headline", "name", "description", "caption")
        ),
        "og_description": get_meta(page, 'meta[property="og:description"]'),
        "description": get_meta(page, 'meta[name="description"]'),
        "page_title": page.title(),
    }
    raw_title = next((value for value in metadata.values() if value), None)
    cleaned_title = clean_title(raw_title)
    if not cleaned_title or cleaned_title.strip().lower() in {"抖音", "douyin"}:
        raw_title = candidate.get("response_title") or raw_title

    published_raw = first_json_ld_value(
        json_ld, ("datePublished", "uploadDate", "dateCreated")
    )
    published_at = published_raw
    time_precision = "source_provided" if published_raw else None
    if not published_at:
        published_raw, published_at, time_precision = extract_publish_time(html)
    if not published_at:
        searchable_text = " ".join(value for value in metadata.values() if value)
        published_raw, published_at = extract_publish_date_from_text(searchable_text)
        if published_at:
            time_precision = "date"
    if not published_at and candidate.get("response_published_at"):
        published_raw = candidate["response_published_at"]
        published_at = candidate["response_published_at"]
        time_precision = "search_response_date"

    published_date = normalize_published_date(published_at)
    in_date_range = bool(published_date and start_date <= published_date <= end_date)
    final_url = page.url
    content_id, content_type = content_identity(final_url)
    return {
        "keyword": candidate["keyword"],
        "search_rank": candidate["search_rank"],
        "content_id": content_id or candidate["content_id"],
        "content_type": content_type or candidate["content_type"],
        "title": clean_title(raw_title),
        "published_at": published_at,
        "published_raw": published_raw,
        "time_precision": time_precision,
        "in_date_range": in_date_range,
        "original_url": url,
        "final_url": final_url,
        "http_status": status_code,
        "crawled_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "status": "ok",
        "error": None,
    }


def error_row(candidate: dict[str, Any], exc: Exception, status: str = "error") -> dict[str, Any]:
    return {
        "keyword": candidate["keyword"],
        "search_rank": candidate["search_rank"],
        "content_id": candidate["content_id"],
        "content_type": candidate["content_type"],
        "title": None,
        "published_at": None,
        "published_raw": None,
        "time_precision": None,
        "in_date_range": False,
        "original_url": candidate["url"],
        "final_url": None,
        "http_status": None,
        "crawled_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "status": status,
        "error": f"{type(exc).__name__}: {exc}",
    }


def write_checkpoint(rows: list[dict[str, Any]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows, columns=RESULT_COLUMNS).to_csv(path, index=False, encoding="utf-8-sig")


def export_results(
    rows: list[dict[str, Any]],
    output_path: Path,
    summary: dict[str, Any],
    csv_only: bool = False,
) -> tuple[Path | None, Path]:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    all_results = pd.DataFrame(rows, columns=RESULT_COLUMNS)
    filtered = all_results[all_results["in_date_range"] == True]  # noqa: E712
    excel_path: Path | None = None
    if csv_only:
        csv_path = output_path if output_path.suffix.lower() == ".csv" else output_path.with_suffix(".csv")
    else:
        excel_path = output_path
        summary_frame = pd.DataFrame(
            [{"项目": key, "值": value} for key, value in summary.items()]
        )
        with pd.ExcelWriter(output_path, engine="openpyxl") as writer:
            filtered.to_excel(writer, sheet_name="日期范围内结果", index=False)
            all_results.to_excel(writer, sheet_name="全部候选", index=False)
            summary_frame.to_excel(writer, sheet_name="运行摘要", index=False)
        csv_path = output_path.with_suffix(".csv")
    all_results.to_csv(csv_path, index=False, encoding="utf-8-sig")
    return excel_path, csv_path


def run_crawler(args: argparse.Namespace, logger: logging.Logger) -> dict[str, Any]:
    started_at = datetime.now().astimezone()
    rows: list[dict[str, Any]] = []
    stop_reason = "正常完成"
    checkpoint_path = Path(args.checkpoint)

    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            user_data_dir=str(Path(args.profile_dir).resolve()),
            channel=args.channel,
            headless=args.headless,
            viewport={"width": 1400, "height": 900},
        )
        try:
            search_page = context.pages[0] if context.pages else context.new_page()
            candidates = discover_candidates(
                search_page,
                args.keyword,
                args.max_results,
                args.max_scrolls,
                args.delay,
                args.timeout,
                args.initial_wait,
                args.manual_verify_timeout,
                args.headless,
                logger,
            )
            logger.info("链接发现完成，共 %s 条", len(candidates))
            detail_page = context.new_page()
            for index, candidate in enumerate(candidates, start=1):
                logger.info("处理详情 %s/%s：%s", index, len(candidates), candidate["url"])
                try:
                    row = extract_detail(
                        detail_page,
                        candidate,
                        args.start_date,
                        args.end_date,
                        args.timeout,
                        args.manual_verify_timeout,
                        args.headless,
                        logger,
                    )
                except AccessBlockedError as exc:
                    row = error_row(candidate, exc, status="stopped")
                    rows.append(row)
                    write_checkpoint(rows, checkpoint_path)
                    stop_reason = str(exc)
                    logger.error("停止采集：%s", exc)
                    break
                except Exception as exc:
                    row = error_row(candidate, exc)
                    logger.exception("详情处理失败：%s", candidate["url"])
                rows.append(row)
                write_checkpoint(rows, checkpoint_path)
                if index < len(candidates):
                    time.sleep(args.delay)
        except AccessBlockedError as exc:
            candidates = []
            stop_reason = str(exc)
            logger.error("停止采集：%s", exc)
        finally:
            context.close()

    finished_at = datetime.now().astimezone()
    write_checkpoint(rows, checkpoint_path)
    summary = {
        "关键词": args.keyword,
        "开始日期": args.start_date.isoformat(),
        "结束日期": args.end_date.isoformat(),
        "候选数量": len(candidates),
        "成功数量": sum(row["status"] == "ok" for row in rows),
        "日期范围内数量": sum(bool(row["in_date_range"]) for row in rows),
        "失败或停止数量": sum(row["status"] != "ok" for row in rows),
        "停止原因": stop_reason,
        "开始时间": started_at.isoformat(timespec="seconds"),
        "结束时间": finished_at.isoformat(timespec="seconds"),
    }
    excel_path, csv_path = export_results(rows, Path(args.output), summary, args.csv_only)
    if excel_path is not None:
        logger.info("Excel 已保存：%s", excel_path.resolve())
    logger.info("CSV 已保存：%s", csv_path.resolve())
    logger.info("Checkpoint：%s", checkpoint_path.resolve())
    return summary


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("必须是大于 0 的整数")
    return parsed


def nonnegative_float(value: str) -> float:
    parsed = float(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("不能小于 0")
    return parsed


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--keyword", required=True, help="搜索关键词")
    parser.add_argument("--start-date", required=True, type=parse_cli_date)
    parser.add_argument("--end-date", required=True, type=parse_cli_date)
    parser.add_argument("--max-results", type=positive_int, default=20)
    parser.add_argument("--max-scrolls", type=positive_int, default=8)
    parser.add_argument("--delay", type=nonnegative_float, default=3.0)
    parser.add_argument("--timeout", type=positive_int, default=30_000, help="页面超时毫秒")
    parser.add_argument("--output", type=Path, default=Path("douyin_results.xlsx"))
    parser.add_argument("--csv-only", action="store_true", help="采集阶段只生成CSV，跳过单关键词Excel")
    parser.add_argument("--checkpoint", type=Path, default=Path("checkpoint.csv"))
    parser.add_argument("--log", type=Path, default=Path("crawler.log"))
    parser.add_argument("--profile-dir", type=Path, default=Path(".douyin_profile"))
    parser.add_argument(
        "--initial-wait",
        type=nonnegative_float,
        default=30.0,
        help="搜索页打开后的等待秒数，用于首次正常登录",
    )
    parser.add_argument("--channel", choices=("msedge", "chrome"), default="msedge")
    parser.add_argument("--headless", action="store_true", help="不显示浏览器窗口")
    parser.add_argument(
        "--manual-verify-timeout",
        type=nonnegative_float,
        default=300.0,
        help="可见模式遇到验证页时等待人工验证的秒数；0 表示不等待",
    )
    args = parser.parse_args()
    if args.start_date > args.end_date:
        parser.error("开始日期不能晚于结束日期")
    return args


def main() -> int:
    args = parse_args()
    logger = configure_logging(args.log)
    try:
        summary = run_crawler(args, logger)
    except Exception as exc:
        logger.exception("程序异常终止")
        print(f"运行失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if summary.get("停止原因") == "正常完成" else 2


if __name__ == "__main__":
    raise SystemExit(main())

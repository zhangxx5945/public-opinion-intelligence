"""按搜索排名深度拟合累积发现曲线，粗略估计2025年标题严格匹配的笔记数量。"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import math
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

from xiaohongshu_keyword_estimator import configure_logging, read_keywords, write_csv, write_json
from xiaohongshu_login import EDGE_PATHS, PROFILE_DIR


PROJECT_DIR = Path(__file__).resolve().parent
DATA_DIR = PROJECT_DIR / "data"
LOG_DIR = PROJECT_DIR / "logs"
YEAR_PATTERN = re.compile(r"^2025-\d{2}-\d{2}$")
TITLE_TOPIC_PROFILES = {
    "养老服务": ("养老",),
    "智慧养老": ("智慧养老", "智能养老", "养老科技"),
    "居家养老": ("居家养老", "养老"),
    "老年大学": ("老年大学", "老年教育", "老年学堂"),
    "公共文化": ("公共文化", "文化"),
    "群众文化": ("群众文化", "文化"),
    "文化活动": ("文化活动", "文化", "文艺活动"),
    "大剧院": ("大剧院", "剧院", "剧场"),
    "政务服务": ("政务服务", "政务"),
    "网上办事": ("网上办事", "办事", "线上办理"),
    "税务局": ("税务局", "税务"),
    "劳动保障": ("劳动保障", "劳动权益", "社保"),
}
COLUMNS = (
    "platform",
    "keyword",
    "year",
    "observed_relevant_2025",
    "estimated_relevant_2025",
    "lower_bound",
    "upper_bound",
    "unique_cards_scanned",
    "scroll_rounds",
    "convergence_status",
    "title_rule",
    "interpretation",
)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def keyword_terms(keyword: str) -> list[str]:
    return [term.casefold() for term in re.split(r"\s+", keyword.strip()) if term]


def title_matches(title: str, keyword: str) -> bool:
    normalized = re.sub(r"\s+", "", title).casefold()
    terms = keyword_terms(keyword)
    location = "天津"
    if location not in normalized:
        return False
    topic = "".join(term for term in terms if term != location)
    alternatives = TITLE_TOPIC_PROFILES.get(topic, (topic,))
    return any(re.sub(r"\s+", "", term).casefold() in normalized for term in alternatives)


def first_nonempty_text(locator: Any) -> str:
    for index in range(locator.count()):
        value = " ".join(locator.nth(index).inner_text().split())
        if value:
            return value
    return ""


def scan_cards(page: Any) -> list[dict[str, str]]:
    cards = page.locator("section.note-item")
    values: list[dict[str, str]] = []
    for index in range(cards.count()):
        card = cards.nth(index)
        explore_links = card.locator("a[href*='/explore/']")
        search_links = card.locator("a[href*='/search_result/']")
        href = ""
        if explore_links.count():
            href = explore_links.first.get_attribute("href") or ""
        if not href and search_links.count():
            href = search_links.first.get_attribute("href") or ""
        note_match = re.search(r"/(?:explore|search_result)/([0-9a-f]{24})", href)
        if not note_match:
            continue
        title = first_nonempty_text(card.locator("a.title span, .title span"))
        if not title:
            title = first_nonempty_text(card.locator("a[href*='/search_result/'] span"))
        date_value = first_nonempty_text(card.locator(".time"))
        values.append({"note_id": note_match.group(1), "title": title, "date": date_value})
    return values


def ensure_search_page_available(page: Any, keyword: str) -> None:
    """拒绝把登录页、安全限制页或未知空白页当成零结果。"""
    current_url = page.url
    title = " ".join(page.title().split())
    body = " ".join(page.locator("body").inner_text().split())[:2_000]
    combined = f"{title} {body}"
    if "/website-login/error" in current_url or any(
        marker in combined for marker in ("安全限制", "IP存在风险", "error_code=300012")
    ):
        raise RuntimeError("小红书返回安全限制（300012）；请使用正常网络环境后重试")
    if any(marker in combined for marker in ("扫码登录", "手机号登录", "登录后查看更多")):
        raise RuntimeError("小红书会话已退出，请重新运行 xiaohongshu_login.py 正常登录")
    if page.locator("section.note-item").count() == 0 and not any(
        marker in combined for marker in ("暂无搜索结果", "没有找到相关结果", "换个词试试")
    ):
        raise RuntimeError(
            f"小红书搜索页未加载结果卡片（关键词：{keyword}，页面标题：{title or '无'}）"
        )


def fit_saturation_curve(points: list[tuple[int, int]]) -> tuple[int, int | None, str]:
    observed = points[-1][1]
    distinct_points: list[tuple[int, int]] = []
    for point in points:
        if not distinct_points or point[0] != distinct_points[-1][0]:
            distinct_points.append(point)
        else:
            distinct_points[-1] = point
    if observed == 0:
        return 0, None, "no_2025_match_in_sample"
    if len(distinct_points) < 3 or distinct_points[-1][0] < distinct_points[0][0] * 1.25:
        return observed, None, "insufficient_rank_depth"

    max_n = max(observed + 100, observed * 8)
    candidates: list[tuple[float, int]] = []
    for total in range(observed, max_n + 1):
        best_error = float("inf")
        for step in range(1, 161):
            rate = 0.00025 * (1.04**step)
            error = sum(
                (count - total * (1.0 - math.exp(-rate * depth))) ** 2
                for depth, count in distinct_points
            )
            best_error = min(best_error, error)
        candidates.append((best_error, total))
    candidates.sort()
    best_error, estimate = candidates[0]
    tolerance = best_error + max(1.0, 0.1 * sum(count * count for _, count in distinct_points))
    plausible = [total for error, total in candidates if error <= tolerance]
    upper = max(plausible) if plausible else estimate
    tail = [points[index][1] - points[index - 1][1] for index in range(1, len(points))][-3:]
    status = "converged" if tail and sum(tail) == 0 else "model_extrapolation"
    return max(observed, estimate), max(observed, upper), status


def observe_keyword(page: Any, keyword: str, args: argparse.Namespace, logger: logging.Logger) -> dict[str, Any]:
    url = (
        "https://www.xiaohongshu.com/search_result?"
        f"keyword={quote(keyword)}&source=web_search_result_notes&type=51"
    )
    page.goto(url, wait_until="commit", timeout=args.timeout * 1_000)
    page.wait_for_timeout(args.initial_wait * 1_000)
    ensure_search_page_available(page, keyword)
    seen: set[str] = set()
    relevant_2025: set[str] = set()
    curve: list[tuple[int, int]] = []
    stable_rounds = 0

    for round_number in range(args.max_scrolls + 1):
        before = len(seen)
        for card in scan_cards(page):
            note_id = card["note_id"]
            seen.add(note_id)
            keyword_match = args.match_mode == "search_returned" or title_matches(
                card["title"], keyword
            )
            if YEAR_PATTERN.fullmatch(card["date"]) and keyword_match:
                relevant_2025.add(note_id)
        new_cards = len(seen) - before
        curve.append((len(seen), len(relevant_2025)))
        logger.info(
            "关键词=%s 深度=%s 唯一卡片=%s 2025标题匹配=%s 新卡片=%s",
            keyword,
            round_number,
            len(seen),
            len(relevant_2025),
            new_cards,
        )
        if round_number == args.max_scrolls:
            break
        stable_rounds = stable_rounds + 1 if new_cards == 0 else 0
        if stable_rounds >= args.stable_rounds:
            break
        page.mouse.wheel(0, args.wheel_distance)
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_timeout(args.scroll_wait * 1_000)

    estimate, upper, status = fit_saturation_curve(curve)
    observed = len(relevant_2025)
    return {
        "platform": "小红书",
        "keyword": keyword,
        "year": 2025,
        "observed_relevant_2025": observed,
        "estimated_relevant_2025": estimate if status != "insufficient_rank_depth" else f">={observed}",
        "lower_bound": observed,
        "upper_bound": upper if upper is not None else "",
        "unique_cards_scanned": len(seen),
        "scroll_rounds": len(curve) - 1,
        "convergence_status": status,
        "title_rule": (
            "不做标题二次过滤，以平台搜索返回为相关"
            if args.match_mode == "search_returned"
            else "标题包含天津及主题核心词或近义词"
        ),
        "interpretation": "2025年公开搜索返回内容规模粗估，非平台全量",
        "curve": [{"rank_depth": depth, "cumulative_2025": count} for depth, count in curve],
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--keywords-file", type=Path, default=PROJECT_DIR / "keywords_2025.txt")
    parser.add_argument("--keyword")
    parser.add_argument(
        "--match-mode",
        choices=("title", "search_returned"),
        default="title",
        help="title为标题规则；search_returned表示平台返回即视为相关",
    )
    parser.add_argument("--max-scrolls", type=int, default=15)
    parser.add_argument("--stable-rounds", type=int, default=3)
    parser.add_argument("--initial-wait", type=float, default=4.0)
    parser.add_argument("--scroll-wait", type=float, default=1.2)
    parser.add_argument("--wheel-distance", type=int, default=2200)
    parser.add_argument("--between-keywords", type=float, default=2.0)
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument("--headed", action="store_true")
    parser.add_argument("--profile-dir", type=Path, default=PROFILE_DIR)
    parser.add_argument("--output", type=Path, default=DATA_DIR / "xiaohongshu_rank_curve_estimates_2025.csv")
    parser.add_argument("--progress", type=Path, default=DATA_DIR / "xiaohongshu_rank_curve_estimates_2025_progress.json")
    parser.add_argument("--log", type=Path, default=LOG_DIR / "xiaohongshu_rank_curve_estimates_2025.log")
    args = parser.parse_args()
    args.keywords = [" ".join(args.keyword.replace("+", " ").split())] if args.keyword else read_keywords(args.keywords_file)
    return args


def main() -> int:
    args = parse_args()
    logger = configure_logging(args.log)
    edge_path = next((path for path in EDGE_PATHS if path.exists()), None)
    if edge_path is None:
        logger.error("未找到 Microsoft Edge")
        return 1
    rows: list[dict[str, Any]] = []
    progress: dict[str, Any] = {"keywords": args.keywords, "completed": [], "results": {}, "updated_at": ""}
    try:
        with sync_playwright() as playwright:
            context = playwright.chromium.launch_persistent_context(
                user_data_dir=str(args.profile_dir),
                executable_path=str(edge_path),
                headless=not args.headed,
                viewport={"width": 1440, "height": 1000},
            )
            page = context.pages[0] if context.pages else context.new_page()
            for index, keyword in enumerate(args.keywords):
                result = observe_keyword(page, keyword, args, logger)
                rows.append({key: value for key, value in result.items() if key != "curve"})
                progress["completed"].append(keyword)
                progress["results"][keyword] = result
                progress["updated_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
                write_csv(args.output, COLUMNS, rows)
                write_json(args.progress, progress)
                if index < len(args.keywords) - 1 and args.between_keywords:
                    time.sleep(args.between_keywords)
            context.close()
    except (PlaywrightError, RuntimeError) as exc:
        logger.exception("排名曲线估计停止：%s", exc)
        return 1
    logger.info("排名曲线估计完成：%s", args.output.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

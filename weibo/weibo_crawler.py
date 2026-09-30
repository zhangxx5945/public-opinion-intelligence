"""按关键词和日期范围采集新浪微博公开搜索结果，支持时间分片与断点续跑。"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import re
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import quote

from scrapling.fetchers import DynamicSession
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

try:
    from .weibo_keyword_test import EDGE_PATHS, BASE_URL, blocked_reason, parse_card
except ImportError:  # 直接执行本文件时使用脚本目录导入
    from weibo_keyword_test import EDGE_PATHS, BASE_URL, blocked_reason, parse_card


PROJECT_DIR = Path(__file__).resolve().parent
DATA_DIR = PROJECT_DIR / "data"
LOG_DIR = PROJECT_DIR / "logs"

COLUMNS = [
    "keyword",
    "window_date",
    "page_number",
    "search_rank",
    "post_id",
    "author",
    "content",
    "published_at",
    "published_at_raw",
    "reposts",
    "reposts_raw",
    "comments",
    "comments_raw",
    "likes",
    "likes_raw",
    "matched_topic_terms",
    "relevance_level",
    "quality_status",
    "rejection_reasons",
    "url",
    "http_status",
    "crawled_at",
]

STRICT_TOPIC_TERMS = ("养老服务",)
EXPANDED_TOPIC_TERMS = (
    "养老服务",
    "养老院",
    "养老机构",
    "居家养老",
    "社区养老",
    "老年服务",
    "助老服务",
    "养老",
)

TOPIC_PROFILES = {
    "剧院": {
        "strict": ("剧院",),
        "expanded": (
            "剧院",
            "大剧院",
            "剧场",
            "戏院",
            "音乐厅",
            "演艺中心",
        ),
        "missing_reason": "missing_theater_topic",
    },
    "文化活动": {
        "strict": ("文化活动",),
        "expanded": (
            "文化活动",
            "文艺活动",
            "群众文化",
            "文化惠民",
            "公共文化",
            "文化演出",
            "文体活动",
        ),
        "missing_reason": "missing_cultural_activity_topic",
    },
    "老年大学": {
        "strict": ("老年大学",),
        "expanded": ("老年大学", "老年教育", "老年学堂", "老年学校", "银龄课堂"),
        "missing_reason": "missing_senior_university_topic",
    },
    "养老服务": {
        "strict": STRICT_TOPIC_TERMS,
        "expanded": EXPANDED_TOPIC_TERMS,
        "missing_reason": "missing_eldercare_topic",
    },
}

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def configure_logging(path: Path) -> logging.Logger:
    path.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("weibo_crawler")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    formatter = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s")
    for handler in (
        logging.FileHandler(path, mode="a", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ):
        handler.setFormatter(formatter)
        logger.addHandler(handler)
    return logger


def parse_date(value: str) -> date:
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as exc:
        raise argparse.ArgumentTypeError("日期必须是 YYYY-MM-DD") from exc


def iter_date_windows(
    start: date,
    end: date,
    newest_first: bool,
    window_days: int = 1,
    sample_day_of_month: int = 0,
) -> Iterable[tuple[date, date]]:
    if sample_day_of_month:
        days = [
            start + timedelta(days=offset)
            for offset in range((end - start).days + 1)
            if (start + timedelta(days=offset)).day == sample_day_of_month
        ]
        windows = [(value, value) for value in days]
    else:
        windows = []
        window_start = start
        while window_start <= end:
            window_end = min(window_start + timedelta(days=window_days - 1), end)
            windows.append((window_start, window_end))
            window_start = window_end + timedelta(days=1)
    return reversed(windows) if newest_first else windows


def normalize_published_at(raw: str, window_date: date) -> str:
    value = re.sub(r"\s+", " ", raw).strip()
    now = datetime.now().astimezone()
    formats = (
        (r"^(\d{4})年(\d{1,2})月(\d{1,2})日\s+(\d{1,2}):(\d{2})$", True),
        (r"^(\d{1,2})月(\d{1,2})日\s+(\d{1,2}):(\d{2})$", False),
    )
    for pattern, has_year in formats:
        match = re.match(pattern, value)
        if not match:
            continue
        parts = [int(part) for part in match.groups()]
        if has_year:
            year, month, day, hour, minute = parts
        else:
            month, day, hour, minute = parts
            year = window_date.year
        return datetime(year, month, day, hour, minute).astimezone().isoformat(timespec="minutes")

    today_match = re.match(r"^今天\s*(\d{1,2}):(\d{2})$", value)
    if today_match:
        hour, minute = (int(part) for part in today_match.groups())
        return now.replace(hour=hour, minute=minute, second=0, microsecond=0).isoformat(timespec="minutes")

    minutes_match = re.match(r"^(\d+)分钟前$", value)
    if minutes_match:
        return (now - timedelta(minutes=int(minutes_match.group(1)))).isoformat(timespec="minutes")
    if value in {"刚刚", "刚才"}:
        return now.isoformat(timespec="minutes")
    return ""


def build_search_url(
    keyword: str, window_start: date, window_end: date, page_number: int
) -> str:
    start_day = window_start.isoformat()
    end_day = window_end.isoformat()
    timescope = quote(f"custom:{start_day}-0:{end_day}-23", safe="")
    return f"{BASE_URL}/weibo?q={quote(keyword)}&timescope={timescope}&page={page_number}"


def topic_profile(keyword: str) -> tuple[tuple[str, ...], tuple[str, ...], str]:
    for topic, profile in TOPIC_PROFILES.items():
        if topic in keyword:
            return profile["strict"], profile["expanded"], profile["missing_reason"]
    keyword_terms = tuple(term for term in re.split(r"\s+", keyword.strip()) if term and term != "天津")
    return keyword_terms, keyword_terms, "missing_keyword_topic"


def validate_row(
    row: dict[str, Any], window_start: date, window_end: date, keyword: str
) -> tuple[list[str], list[str], str]:
    reasons: list[str] = []
    content = str(row.get("content") or "")
    published_at = str(row.get("published_at") or "")
    published_day = published_at[:10]
    if not (
        window_start.isoformat() <= published_day <= window_end.isoformat()
    ):
        reasons.append("date_mismatch")
    if "天津" not in content:
        reasons.append("missing_tianjin")
    strict_terms, expanded_terms, missing_reason = topic_profile(keyword)
    matched_terms = [term for term in expanded_terms if term in content]
    if not matched_terms:
        reasons.append(missing_reason)
    if any(term in content for term in strict_terms):
        relevance_level = "strict"
    elif matched_terms:
        relevance_level = "expanded"
    else:
        relevance_level = "rejected"
    return reasons, matched_terms, relevance_level


def has_next_page(response: Any) -> bool:
    for link in response.css("a.next"):
        href = str(link.attrib.get("href") or "")
        text = str(link.get_all_text(separator=" ", strip=True))
        if href and ("下一页" in text or "page=" in href):
            return True
    return False


def read_csv(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as file:
        return list(csv.DictReader(file))


def write_csv(rows: list[dict[str, Any]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows({column: row.get(column) for column in COLUMNS} for row in rows)
    temporary.replace(path)


def load_progress(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"completed_dates": []}
    return json.loads(path.read_text(encoding="utf-8"))


def write_progress(path: Path, progress: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(progress, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def build_quality_report(rows: list[dict[str, Any]], rejected: list[dict[str, Any]]) -> dict[str, Any]:
    def rate(numerator: int, denominator: int) -> float:
        return round(numerator / denominator, 4) if denominator else 1.0

    def populated(value: Any) -> bool:
        return value is not None and str(value).strip() != ""

    def published_matches_window(row: dict[str, Any]) -> bool:
        published_day = str(row.get("published_at") or "")[:10]
        window = str(row.get("window_date") or "")
        if ".." in window:
            start_day, end_day = window.split("..", 1)
        else:
            start_day = end_day = window
        return bool(published_day) and start_day <= published_day <= end_day

    total = len(rows)
    required = ("post_id", "author", "content", "published_at", "url")
    completeness = {
        field: {
            "present": sum(bool(str(row.get(field) or "").strip()) for row in rows),
            "rate": rate(sum(bool(str(row.get(field) or "").strip()) for row in rows), total),
        }
        for field in required
    }
    interaction_fields = ("reposts", "comments", "likes")
    interaction_population = {
        field: {
            "present": sum(populated(row.get(field)) for row in rows),
            "rate": rate(sum(populated(row.get(field)) for row in rows), total),
        }
        for field in interaction_fields
    }
    rejection_counts: dict[str, int] = {}
    for row in rejected:
        for reason in str(row.get("rejection_reasons") or "").split("|"):
            if reason:
                rejection_counts[reason] = rejection_counts.get(reason, 0) + 1
    unique_ids = len({str(row.get("post_id")) for row in rows})
    date_matches = sum(published_matches_window(row) for row in rows)
    return {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "accepted_count": total,
        "rejected_count": len(rejected),
        "unique_post_ids": unique_ids,
        "duplicate_post_ids": total - unique_ids,
        "date_match_rate": rate(date_matches, total),
        "quality_status_rate": rate(sum(row.get("quality_status") == "accepted" for row in rows), total),
        "core_completeness": completeness,
        "interaction_population": interaction_population,
        "rejection_reasons": rejection_counts,
    }


def validate_progress(progress: dict[str, Any], keyword: str, start: date, end: date) -> None:
    expected = {"keyword": keyword, "start_date": start.isoformat()}
    existing = {key: progress.get(key) for key in expected if progress.get(key) is not None}
    conflicts = {key: (existing[key], expected[key]) for key in existing if existing[key] != expected[key]}
    existing_end = progress.get("end_date")
    if existing_end and end < date.fromisoformat(str(existing_end)):
        conflicts["end_date"] = (existing_end, end.isoformat())
    if conflicts:
        raise RuntimeError(f"进度文件属于另一项任务：{conflicts}；请更换输出前缀或使用 --fresh")


def create_session(args: argparse.Namespace) -> DynamicSession:
    edge_path = next((path for path in EDGE_PATHS if path.exists()), None)
    options: dict[str, Any] = {
        "headless": not args.headed,
        "network_idle": False,
        # 微博页面的非必要资源偶尔会长时间挂起 load 事件。
        # 不等待全部 JS 完成，改用下方 wait 给搜索卡片留出渲染时间。
        "load_dom": False,
        "wait": args.page_wait * 1_000,
        "timeout": args.timeout * 1_000,
        "user_data_dir": str(args.profile_dir.resolve()),
        "disable_resources": True,
        "max_pages": 1,
        "retries": 1,
    }
    if edge_path:
        options["executable_path"] = str(edge_path)
    return DynamicSession(**options)


def crawl(args: argparse.Namespace, logger: logging.Logger) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    progress = {"completed_dates": []} if args.fresh else load_progress(args.progress)
    validate_progress(progress, args.keyword, args.start_date, args.end_date)
    progress.update(
        {
            "keyword": args.keyword,
            "start_date": args.start_date.isoformat(),
            "end_date": args.end_date.isoformat(),
            "window_days": args.window_days,
        }
    )
    completed_dates = set(progress.get("completed_dates", []))
    failed_dates: dict[str, Any] = dict(progress.get("failed_dates", {}))
    rows = [] if args.fresh else read_csv(args.checkpoint)
    rejected = [] if args.fresh else read_csv(args.rejected)
    seen = {str(row.get("post_id")) for row in rows if row.get("post_id")}
    seen_rejected = {
        (str(row.get("post_id")), str(row.get("window_date")))
        for row in rejected
        if row.get("post_id")
    }
    logger.info(
        "恢复已有 %s 条有效数据、%s 条拒绝数据，已完成 %s 个日期",
        len(rows),
        len(rejected),
        len(completed_dates),
    )

    first_request = True
    with create_session(args) as session:
        for window_start, window_end in iter_date_windows(
            args.start_date,
            args.end_date,
            args.newest_first,
            args.window_days,
            args.sample_day_of_month,
        ):
            window_key = (
                window_start.isoformat()
                if window_start == window_end
                else f"{window_start.isoformat()}..{window_end.isoformat()}"
            )
            if window_key in completed_dates:
                continue
            logger.info("开始时间窗 %s", window_key)
            page_number = 1
            invalid_pages = 0
            day_complete = True

            while True:
                if args.max_pages_per_day and page_number > args.max_pages_per_day:
                    logger.warning("时间窗 %s 达到测试页数限制 %s", window_key, args.max_pages_per_day)
                    day_complete = False
                    break
                url = build_search_url(args.keyword, window_start, window_end, page_number)
                wait_seconds = args.initial_wait if first_request else args.page_wait
                first_request = False
                response = None
                for attempt in range(1, args.page_retries + 1):
                    try:
                        response = session.fetch(url, wait=wait_seconds * 1_000)
                        break
                    except PlaywrightTimeoutError as exc:
                        logger.warning(
                            "日期 %s 第 %s 页超时，重试 %s/%s：%s",
                            window_key,
                            page_number,
                            attempt,
                            args.page_retries,
                            exc,
                        )
                        if attempt < args.page_retries and args.retry_delay:
                            time.sleep(args.retry_delay)
                if response is None:
                    failed_dates[window_key] = {
                        "page_number": page_number,
                        "reason": "page_timeout",
                        "attempts": args.page_retries,
                        "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                    }
                    logger.error("时间窗 %s 连续超时，记录失败并继续下一窗口", window_key)
                    day_complete = False
                    break
                status = int(response.status)
                if status >= 400:
                    raise RuntimeError(f"{day} 第 {page_number} 页 HTTP {status}")

                cards = response.css('div.card-wrap[action-type="feed_list_item"]')
                if not cards:
                    reason = blocked_reason(response)
                    if reason:
                        raise RuntimeError(f"{day} 第 {page_number} 页被停止：{reason}")
                    logger.info("时间窗 %s 第 %s 页无结果，结束该窗口", window_key, page_number)
                    break

                added = 0
                rejected_added = 0
                for card in cards:
                    row = parse_card(card, args.keyword, len(rows) + len(rejected) + 1, status)
                    post_id = str(row["post_id"])
                    if post_id in seen:
                        continue
                    raw_date = str(row.pop("published_at", ""))
                    row.update(
                        {
                            "window_date": window_key,
                            "page_number": page_number,
                            "published_at": normalize_published_at(raw_date, window_start),
                            "published_at_raw": raw_date,
                        }
                    )
                    reasons, matched_terms, relevance_level = validate_row(
                        row, window_start, window_end, args.keyword
                    )
                    row.update(
                        {
                            "matched_topic_terms": "|".join(matched_terms),
                            "relevance_level": relevance_level,
                            "quality_status": "rejected" if reasons else "accepted",
                            "rejection_reasons": "|".join(reasons),
                        }
                    )
                    if reasons and not args.accept_all_search_results:
                        rejection_key = (post_id, window_key)
                        if rejection_key not in seen_rejected:
                            seen_rejected.add(rejection_key)
                            rejected.append(row)
                            rejected_added += 1
                        continue
                    if reasons:
                        row["quality_status"] = "accepted_unfiltered"
                    seen.add(post_id)
                    rows.append(row)
                    added += 1

                write_csv(rows, args.checkpoint)
                write_csv(rejected, args.rejected)
                logger.info(
                    "日期 %s 第 %s 页：有效新增 %s、拒绝新增 %s；累计有效 %s、拒绝 %s",
                    window_key,
                    page_number,
                    added,
                    rejected_added,
                    len(rows),
                    len(rejected),
                )
                invalid_pages = invalid_pages + 1 if added == 0 else 0
                if not has_next_page(response):
                    logger.info("时间窗 %s 第 %s 页没有真实下一页，结束该窗口", window_key, page_number)
                    break
                if invalid_pages >= 2:
                    logger.info("时间窗 %s 连续两页无有效数据，结束该窗口", window_key)
                    break
                page_number += 1
                if args.delay:
                    time.sleep(args.delay)

            if day_complete:
                completed_dates.add(window_key)
                failed_dates.pop(window_key, None)
            progress["completed_dates"] = sorted(completed_dates)
            progress["failed_dates"] = failed_dates
            progress["row_count"] = len(rows)
            progress["rejected_count"] = len(rejected)
            progress["updated_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
            write_progress(args.progress, progress)
            if args.date_delay:
                time.sleep(args.date_delay)

    return rows, rejected


def parse_args() -> argparse.Namespace:
    today = date.today()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--keyword", default="天津 养老服务")
    parser.add_argument("--start-date", type=parse_date, default=date(2026, 1, 1))
    parser.add_argument("--end-date", type=parse_date, default=today)
    parser.add_argument("--delay", type=float, default=2.0, help="同一天翻页间隔秒数")
    parser.add_argument("--date-delay", type=float, default=1.0, help="日期之间间隔秒数")
    parser.add_argument("--page-wait", type=float, default=2.0, help="每页加载后的等待秒数")
    parser.add_argument("--initial-wait", type=float, default=5.0, help="首个页面等待秒数")
    parser.add_argument("--timeout", type=float, default=30.0, help="页面超时秒数")
    parser.add_argument("--page-retries", type=int, default=3, help="单页超时重试次数")
    parser.add_argument("--retry-delay", type=float, default=5.0, help="超时重试间隔秒数")
    parser.add_argument("--max-pages-per-day", type=int, default=0, help="0 表示不主动限制")
    parser.add_argument(
        "--window-days",
        type=int,
        default=1,
        help="每个搜索时间窗的天数；1 表示逐日，7 表示按周",
    )
    parser.add_argument(
        "--sample-day-of-month",
        type=int,
        default=0,
        help="1-28 表示每月只测试该日；0 表示逐日运行",
    )
    parser.add_argument("--newest-first", action="store_true", help="从最近日期向年初采集")
    parser.add_argument(
        "--accept-all-search-results",
        action="store_true",
        help="保留搜索页返回的全部帖子，仅记录筛选标记，不移入 rejected",
    )
    parser.add_argument("--headed", action="store_true")
    parser.add_argument("--fresh", action="store_true", help="忽略已有检查点并重新开始")
    parser.add_argument("--profile-dir", type=Path, default=PROJECT_DIR / ".weibo_profile")
    parser.add_argument("--output", type=Path, default=DATA_DIR / "weibo_tianjin_elderly_results.csv")
    parser.add_argument("--checkpoint", type=Path, default=DATA_DIR / "weibo_tianjin_elderly_checkpoint.csv")
    parser.add_argument("--rejected", type=Path, default=DATA_DIR / "weibo_tianjin_elderly_rejected.csv")
    parser.add_argument("--progress", type=Path, default=DATA_DIR / "weibo_tianjin_elderly_progress.json")
    parser.add_argument("--quality", type=Path, default=DATA_DIR / "weibo_tianjin_elderly_quality.json")
    parser.add_argument("--log", type=Path, default=LOG_DIR / "weibo_tianjin_elderly.log")
    args = parser.parse_args()
    if args.start_date > args.end_date:
        parser.error("开始日期不能晚于结束日期")
    if args.end_date > today:
        parser.error("结束日期不能晚于今天")
    numeric_values = (
        args.delay,
        args.date_delay,
        args.page_wait,
        args.initial_wait,
        args.timeout,
        args.max_pages_per_day,
        args.page_retries,
        args.retry_delay,
        args.sample_day_of_month,
        args.window_days,
    )
    if any(value < 0 for value in numeric_values):
        parser.error("数值参数不能小于 0")
    if args.page_retries == 0:
        parser.error("page-retries 必须大于 0")
    if args.window_days == 0:
        parser.error("window-days 必须大于 0")
    if args.sample_day_of_month > 28:
        parser.error("sample-day-of-month 必须为 0-28")
    return args


def main() -> int:
    args = parse_args()
    logger = configure_logging(args.log)
    started_at = datetime.now().astimezone()
    try:
        rows, rejected = crawl(args, logger)
        write_csv(rows, args.output)
        write_csv(rejected, args.rejected)
        quality = build_quality_report(rows, rejected)
        write_progress(args.quality, quality)
    except Exception as exc:
        logger.exception("采集停止")
        print(f"运行失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    summary = {
        "keyword": args.keyword,
        "start_date": args.start_date.isoformat(),
        "end_date": args.end_date.isoformat(),
        "count": len(rows),
        "unique_post_ids": len({row["post_id"] for row in rows}),
        "rejected_count": len(rejected),
        "started_at": started_at.isoformat(timespec="seconds"),
        "finished_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "output": str(args.output.resolve()),
        "rejected": str(args.rejected.resolve()),
        "quality": str(args.quality.resolve()),
    }
    logger.info("采集完成：%s", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

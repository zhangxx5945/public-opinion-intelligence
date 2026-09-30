"""对小红书公开搜索结果做固定深度重复抽样，估计2025年可见笔记数量。"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import re
import statistics
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

from xiaohongshu_login import EDGE_PATHS, PROFILE_DIR


PROJECT_DIR = Path(__file__).resolve().parent
DATA_DIR = PROJECT_DIR / "data"
LOG_DIR = PROJECT_DIR / "logs"
YEAR_PATTERN = re.compile(r"^2025-\d{2}-\d{2}$")
RUN_COLUMNS = (
    "keyword",
    "run_number",
    "observed_2025_count",
    "sample_cards",
    "dated_cards",
    "scroll_rounds",
    "stop_reason",
    "observed_at",
)
SUMMARY_COLUMNS = (
    "platform",
    "keyword",
    "year",
    "estimated_visible_count",
    "minimum_observed",
    "maximum_observed",
    "mean_observed",
    "runs_completed",
    "median_sample_cards",
    "method",
    "interpretation",
)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def read_keywords(path: Path) -> list[str]:
    values: list[str] = []
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        value = " ".join(line.strip().replace("+", " ").split())
        if value and not value.startswith("#") and value not in values:
            values.append(value)
    return values


def configure_logging(path: Path) -> logging.Logger:
    path.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("xiaohongshu_estimator")
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


def write_csv(path: Path, columns: tuple[str, ...], rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=columns)
        writer.writeheader()
        writer.writerows({column: row.get(column, "") for column in columns} for row in rows)
    temporary.replace(path)


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def build_summary(keywords: list[str], run_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    summary: list[dict[str, Any]] = []
    for keyword in keywords:
        rows = [row for row in run_rows if row["keyword"] == keyword]
        counts = [int(row["observed_2025_count"]) for row in rows]
        samples = [int(row["sample_cards"]) for row in rows]
        summary.append(
            {
                "platform": "小红书",
                "keyword": keyword,
                "year": 2025,
                "estimated_visible_count": round(statistics.median(counts)) if counts else "",
                "minimum_observed": min(counts) if counts else "",
                "maximum_observed": max(counts) if counts else "",
                "mean_observed": round(statistics.mean(counts), 2) if counts else "",
                "runs_completed": len(rows),
                "median_sample_cards": round(statistics.median(samples)) if samples else "",
                "method": f"固定滚动深度搜索，实际完成{len(rows)}次观察取中位数",
                "interpretation": "2025年公开搜索可见结果观察估计，非平台全量",
            }
        )
    return summary


def observe_once(page: Any, keyword: str, args: argparse.Namespace) -> dict[str, Any]:
    url = (
        "https://www.xiaohongshu.com/search_result?"
        f"keyword={quote(keyword)}&source=web_search_result_notes&type=51"
    )
    page.goto(url, wait_until="domcontentloaded", timeout=args.timeout * 1_000)
    page.wait_for_timeout(args.initial_wait * 1_000)
    stable_rounds = 0
    previous_count = -1
    scroll_rounds = 0
    stop_reason = "max_scrolls"

    while scroll_rounds < args.max_scrolls:
        card_count = page.locator("section.note-item").count()
        if card_count >= args.target_cards:
            stop_reason = "target_cards"
            break
        stable_rounds = stable_rounds + 1 if card_count == previous_count else 0
        if stable_rounds >= args.stable_rounds:
            stop_reason = "no_new_cards"
            break
        previous_count = card_count
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_timeout(args.scroll_wait * 1_000)
        scroll_rounds += 1

    cards = page.locator("section.note-item")
    card_count = cards.count()
    date_texts = [" ".join(value.split()) for value in cards.locator(".time").all_inner_texts()]
    observed_2025 = sum(bool(YEAR_PATTERN.fullmatch(value)) for value in date_texts)
    body_start = " ".join(page.locator("body").inner_text().split())[:2_000]
    if card_count == 0:
        title = " ".join(page.title().split())
        current_url = page.url
        combined = f"{title} {body_start}"
        if "/website-login/error" in current_url or any(
            marker in combined for marker in ("安全限制", "IP存在风险", "error_code=300012")
        ):
            raise RuntimeError("小红书返回安全限制（300012）；请使用正常网络环境后重试")
        if any(marker in combined for marker in ("扫码登录", "手机号登录", "登录后")):
            raise RuntimeError("小红书会话已退出，请重新运行登录助手")
        if not any(marker in combined for marker in ("暂无搜索结果", "没有找到相关结果", "换个词试试")):
            raise RuntimeError(f"小红书搜索页未加载结果卡片（页面标题：{title or '无'}）")
    return {
        "observed_2025_count": observed_2025,
        "sample_cards": card_count,
        "dated_cards": len(date_texts),
        "scroll_rounds": scroll_rounds,
        "stop_reason": stop_reason,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--keywords-file", type=Path, default=PROJECT_DIR / "keywords_2025.txt")
    parser.add_argument("--keyword", help="只测试一个关键词")
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--target-cards", type=int, default=240)
    parser.add_argument("--max-scrolls", type=int, default=16)
    parser.add_argument("--stable-rounds", type=int, default=3)
    parser.add_argument("--initial-wait", type=float, default=5.0)
    parser.add_argument("--scroll-wait", type=float, default=1.5)
    parser.add_argument("--between-runs", type=float, default=3.0)
    parser.add_argument("--between-keywords", type=float, default=5.0)
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument("--headed", action="store_true")
    parser.add_argument("--fresh", action="store_true")
    parser.add_argument("--profile-dir", type=Path, default=PROFILE_DIR)
    parser.add_argument("--output", type=Path, default=DATA_DIR / "xiaohongshu_keyword_estimates_2025.csv")
    parser.add_argument("--runs-output", type=Path, default=DATA_DIR / "xiaohongshu_keyword_sample_runs_2025.csv")
    parser.add_argument("--progress", type=Path, default=DATA_DIR / "xiaohongshu_keyword_estimates_2025_progress.json")
    parser.add_argument("--log", type=Path, default=LOG_DIR / "xiaohongshu_keyword_estimates_2025.log")
    args = parser.parse_args()
    if args.runs < 1 or args.target_cards < 1 or args.max_scrolls < 1:
        parser.error("runs、target-cards、max-scrolls 必须大于0")
    args.keywords = [" ".join(args.keyword.replace("+", " ").split())] if args.keyword else read_keywords(args.keywords_file)
    return args


def main() -> int:
    args = parse_args()
    logger = configure_logging(args.log)
    edge_path = next((path for path in EDGE_PATHS if path.exists()), None)
    if edge_path is None:
        logger.error("未找到 Microsoft Edge")
        return 1
    run_rows: list[dict[str, Any]] = []
    if not args.fresh and args.runs_output.exists():
        with args.runs_output.open("r", encoding="utf-8-sig", newline="") as file:
            run_rows = list(csv.DictReader(file))

    progress = {
        "platform": "小红书",
        "year": 2025,
        "keywords": args.keywords,
        "runs_per_keyword": args.runs,
        "target_cards": args.target_cards,
        "completed_runs": {},
        "updated_at": "",
    }

    try:
        with sync_playwright() as playwright:
            context = playwright.chromium.launch_persistent_context(
                user_data_dir=str(args.profile_dir),
                executable_path=str(edge_path),
                headless=not args.headed,
                viewport={"width": 1440, "height": 1000},
            )
            page = context.pages[0] if context.pages else context.new_page()
            for keyword_index, keyword in enumerate(args.keywords):
                existing_runs = {
                    int(row["run_number"])
                    for row in run_rows
                    if row["keyword"] == keyword
                }
                for run_number in range(1, args.runs + 1):
                    if run_number in existing_runs:
                        continue
                    metrics = observe_once(page, keyword, args)
                    row = {
                        "keyword": keyword,
                        "run_number": run_number,
                        **metrics,
                        "observed_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                    }
                    run_rows.append(row)
                    write_csv(args.runs_output, RUN_COLUMNS, run_rows)
                    write_csv(args.output, SUMMARY_COLUMNS, build_summary(args.keywords, run_rows))
                    progress["completed_runs"] = {
                        item: sum(row["keyword"] == item for row in run_rows)
                        for item in args.keywords
                    }
                    progress["updated_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
                    write_json(args.progress, progress)
                    logger.info("关键词=%s 轮次=%s 指标=%s", keyword, run_number, metrics)
                    if run_number < args.runs and args.between_runs:
                        time.sleep(args.between_runs)
                if keyword_index < len(args.keywords) - 1 and args.between_keywords:
                    time.sleep(args.between_keywords)
            context.close()
    except (PlaywrightError, RuntimeError) as exc:
        logger.exception("抽样估计停止：%s", exc)
        return 1

    logger.info("全部关键词抽样完成：%s", args.output.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Fast sequential batch for remaining Douyin 2025 keyword categories."""

from __future__ import annotations

import argparse
import csv
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = PROJECT_ROOT / "tencent_news" / "keywords"
DEFAULT_OUTPUT_ROOT = Path(os.getenv("CRAWLER_OUTPUT_ROOT", PROJECT_ROOT / "outputs")) / "douyin"
CATEGORY_CONFIGS = {
    "政务服务": CONFIG_DIR / "government_service_2025.json",
    "公共文化": CONFIG_DIR / "public_culture_2025.json",
    "互联网销售": CONFIG_DIR / "internet_sales_2025.json",
    # 养老服务放在末尾：先续完上面三类，再用快速模式补跑原来的20个词。
    "养老服务": CONFIG_DIR / "elderly_service_2025.json",
}

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def read_json(path: Path) -> dict:
    # utf-8-sig also accepts ordinary UTF-8 and tolerates BOM written by
    # Windows PowerShell when a runtime progress file is repaired manually.
    return json.loads(path.read_text(encoding="utf-8-sig")) if path.exists() else {}


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def read_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as file:
        return list(csv.DictReader(file))


def merge(source: Path, keyword: str, combined: Path) -> tuple[int, int]:
    incoming = read_rows(source)
    current = [row for row in read_rows(combined) if row.get("keyword") != keyword]
    seen: set[tuple[str, str]] = set()
    merged = []
    for row in [*current, *incoming]:
        key = (row.get("keyword", ""), row.get("content_id", ""))
        if key in seen:
            continue
        seen.add(key)
        merged.append(row)
    columns = list(dict.fromkeys(key for row in merged for key in row))
    combined.parent.mkdir(parents=True, exist_ok=True)
    temp = combined.with_suffix(combined.suffix + ".tmp")
    with temp.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=columns)
        writer.writeheader()
        writer.writerows(merged)
    temp.replace(combined)
    return len(incoming), len(merged)


def safe_stem(index: int, keyword: str) -> str:
    return f"{index:02d}_" + "".join("_" if value in '<>:"/\\|?*' else value for value in keyword)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--max-scrolls", type=int, default=30)
    parser.add_argument("--delay", type=float, default=2.0)
    parser.add_argument("--manual-verify-timeout", type=float, default=900.0)
    parser.add_argument("--keyword-delay", type=float, default=20.0)
    parser.add_argument("--rate-limit-cooldown", type=float, default=1800.0)
    parser.add_argument("--rate-limit-retries", type=int, default=48)
    parser.add_argument("--headed", action="store_true", help="使用可见Edge运行（可最小化到后台）")
    args = parser.parse_args()
    crawler = Path(__file__).resolve().parent / "douyin_fast_crawler.py"

    for category, config in CATEGORY_CONFIGS.items():
        keywords = json.loads(config.read_text(encoding="utf-8"))
        combined = args.output_root / "数据" / f"抖音—{category}_快速.csv"
        state_dir = args.output_root / "运行状态" / f"{category}_2025_快速"
        progress_path = args.output_root / "运行状态" / f"抖音—{category}_快速进度.json"
        progress = read_json(progress_path) or {"completed": {}, "failed": {}}
        for index, keyword in enumerate(keywords, start=1):
            keyword = " ".join(keyword.replace("+", " ").split())
            if keyword in progress.get("completed", {}):
                print(f"[{category} {index}/20] 跳过：{keyword}", flush=True)
                continue
            result = state_dir / f"{safe_stem(index, keyword)}_result.csv"
            command = [
                sys.executable, str(crawler), "--keyword", keyword,
                "--target-year", "2025", "--max-scrolls", str(args.max_scrolls),
                "--delay", str(args.delay), "--manual-verify-timeout", str(args.manual_verify_timeout),
                "--output", str(result),
            ]
            if not args.headed:
                command.append("--headless")
            print(f"[{category} {index}/20] 快速开始：{keyword}", flush=True)
            rate_attempt = 0
            while True:
                completed = subprocess.run(command, cwd=PROJECT_ROOT, check=False)
                if completed.returncode == 2 and not args.headed:
                    print(f"[{category} {index}/20] 后台检测到真实验证，正在弹出可见 Edge", flush=True)
                    visible_command = [value for value in command if value != "--headless"]
                    completed = subprocess.run(visible_command, cwd=PROJECT_ROOT, check=False)
                if completed.returncode != 3:
                    break
                rate_attempt += 1
                if rate_attempt > args.rate_limit_retries:
                    print(
                        f"[{category} {index}/20] 过度搜索持续存在，已达到 "
                        f"{args.rate_limit_retries} 次冷却重试上限",
                        flush=True,
                    )
                    break
                print(
                    f"[{category} {index}/20] 检测到过度搜索，第 {rate_attempt}/"
                    f"{args.rate_limit_retries} 次冷却 {args.rate_limit_cooldown:.0f} 秒后自动重试",
                    flush=True,
                )
                time.sleep(args.rate_limit_cooldown)
            if completed.returncode != 0:
                progress.setdefault("failed", {})[keyword] = {"returncode": completed.returncode, "result": str(result)}
                progress.update({"category": category, "updated_at": datetime.now().astimezone().isoformat(timespec="seconds")})
                write_json(progress_path, progress)
                print("验证或访问限制未解除，停止批处理并保留快速结果", flush=True)
                return completed.returncode
            rows, total = merge(result, keyword, combined)
            progress.setdefault("completed", {})[keyword] = {"rows_2025": rows, "combined_rows": total}
            progress.setdefault("failed", {}).pop(keyword, None)
            progress.update({"category": category, "updated_at": datetime.now().astimezone().isoformat(timespec="seconds")})
            write_json(progress_path, progress)
            result.unlink(missing_ok=True)
            print(f"[{category} {index}/20] 完成：2025年 {rows} 条；汇总 {total} 条", flush=True)
            time.sleep(args.keyword_delay)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

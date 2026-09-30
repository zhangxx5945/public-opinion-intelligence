"""Filter merged Weibo search results for analysis-ready output."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd


DEFAULT_MAX_ROWS_PER_KEYWORD = 1000
ENGAGEMENT_COLUMNS = ("likes", "comments", "reposts")


def _matched_keywords(row: pd.Series) -> set[str]:
    values: set[str] = set()
    keyword = str(row.get("keyword") or "").strip()
    if keyword:
        values.add(keyword)
    existing = str(row.get("matched_keywords") or "").strip()
    if existing and existing.lower() != "nan":
        values.update(value.strip() for value in existing.split("；") if value.strip())
    return values


def filter_combined_frame(
    frame: pd.DataFrame,
    max_rows_per_keyword: int = DEFAULT_MAX_ROWS_PER_KEYWORD,
) -> tuple[pd.DataFrame, dict[str, object]]:
    """Keep strict matches, cap each keyword, then deduplicate posts globally."""
    required = {"keyword", "post_id", "content", "quality_status"}
    missing = sorted(required.difference(frame.columns))
    if missing:
        raise RuntimeError(f"Missing required columns: {', '.join(missing)}")

    source_rows = len(frame)
    working = frame.copy()
    working["keyword"] = working["keyword"].fillna("").astype(str).str.strip()
    working["content"] = working["content"].fillna("").astype(str)
    working = working[
        working["quality_status"].eq("accepted")
        & working["content"].str.contains("天津", regex=False)
        & working["keyword"].ne("")
        & working["post_id"].notna()
    ].copy()
    strict_rows = len(working)

    for column in ENGAGEMENT_COLUMNS:
        if column not in working.columns:
            working[column] = 0
        working[column] = pd.to_numeric(working[column], errors="coerce").fillna(0)
    working["_engagement_score"] = working[list(ENGAGEMENT_COLUMNS)].sum(axis=1)
    if "published_at" not in working.columns:
        working["published_at"] = ""

    working = working.sort_values(
        ["keyword", "_engagement_score", "published_at"],
        ascending=[True, False, False],
        kind="stable",
    )
    working = working.drop_duplicates(subset=["keyword", "post_id"], keep="first")
    before_cap_by_keyword = {
        str(key): int(value) for key, value in working.groupby("keyword").size().items()
    }
    if max_rows_per_keyword > 0:
        working = working.groupby("keyword", sort=False, group_keys=False).head(
            max_rows_per_keyword
        )
    after_cap_by_keyword = {
        str(key): int(value) for key, value in working.groupby("keyword").size().items()
    }
    rows_after_cap = len(working)

    labels_by_post: dict[str, set[str]] = {}
    for _, row in working.iterrows():
        key = str(row["post_id"])
        labels_by_post.setdefault(key, set()).update(_matched_keywords(row))

    working = working.sort_values(
        ["_engagement_score", "published_at"],
        ascending=[False, False],
        kind="stable",
    ).drop_duplicates(subset=["post_id"], keep="first")
    working["matched_keywords"] = working["post_id"].map(
        lambda value: "；".join(sorted(labels_by_post.get(str(value), set())))
    )
    working["matched_keyword_count"] = working["post_id"].map(
        lambda value: len(labels_by_post.get(str(value), set()))
    )
    working = working.drop(columns=["_engagement_score"])

    leading = ["keyword", "matched_keywords", "matched_keyword_count"]
    remaining = [column for column in working.columns if column not in leading]
    working = working[leading + remaining].reset_index(drop=True)
    report: dict[str, object] = {
        "source_rows": source_rows,
        "strict_rows": strict_rows,
        "removed_non_strict": source_rows - strict_rows,
        "before_cap_by_keyword": before_cap_by_keyword,
        "after_cap_by_keyword": after_cap_by_keyword,
        "rows_after_cap": rows_after_cap,
        "rows_after_global_dedupe": len(working),
        "removed_cross_keyword_duplicates": rows_after_cap - len(working),
        "max_rows_per_keyword": max_rows_per_keyword,
    }
    return working, report


def write_filtered_csv(frame: pd.DataFrame, output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    frame.to_csv(temporary, index=False, encoding="utf-8-sig")
    temporary.replace(output)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--max-rows-per-keyword", type=int, default=1000)
    parser.add_argument("--report", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    source = pd.read_csv(args.input)
    filtered, report = filter_combined_frame(source, args.max_rows_per_keyword)
    write_filtered_csv(filtered, args.output)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

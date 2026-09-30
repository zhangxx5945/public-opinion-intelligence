"""Batch sentiment inference for CSV/XLSX crawler results.

The command preserves the source columns and writes predictions to a new file.
It intentionally keeps model download/cache management outside the repository.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Iterable

import pandas as pd
import torch
from tqdm import tqdm
from transformers import AutoModelForSequenceClassification, AutoTokenizer


DEFAULT_MODEL = "tabularisai/multilingual-sentiment-analysis"
DEFAULT_TEXT_COLUMNS = (
    "title",
    "content",
    "summary",
    "description",
    "desc",
    "text",
    "标题",
    "正文",
    "文本内容",
    "摘要",
    "内容",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="为爬虫结果批量添加正面/中立/负面预测")
    parser.add_argument("--input", type=Path, required=True, help="输入 CSV 或 XLSX")
    parser.add_argument("--output", type=Path, required=True, help="输出 CSV 或 XLSX；不得与输入相同")
    parser.add_argument("--sheet", default=0, help="XLSX 输入工作表名或序号，默认第一张")
    parser.add_argument("--text-columns", nargs="+", help="用于预测的文本列；默认自动识别")
    parser.add_argument("--model", default=DEFAULT_MODEL, help="Hugging Face 模型名或本地目录")
    parser.add_argument("--batch-size", type=int, default=32)
    parser.add_argument("--max-length", type=int, default=256)
    parser.add_argument("--review-threshold", type=float, default=0.60)
    parser.add_argument("--offline", action="store_true", help="仅使用本地缓存/本地模型")
    return parser.parse_args()


def read_frame(path: Path, sheet: str | int) -> pd.DataFrame:
    suffix = path.suffix.lower()
    if suffix == ".csv":
        try:
            return pd.read_csv(path, encoding="utf-8-sig")
        except UnicodeDecodeError:
            return pd.read_csv(path, encoding="gb18030")
    if suffix in {".xlsx", ".xls"}:
        if isinstance(sheet, str) and sheet.isdigit():
            sheet = int(sheet)
        return pd.read_excel(path, sheet_name=sheet)
    raise ValueError(f"不支持的输入格式：{path.suffix}")


def select_columns(frame: pd.DataFrame, requested: Iterable[str] | None) -> list[str]:
    if requested:
        missing = [name for name in requested if name not in frame.columns]
        if missing:
            raise KeyError(f"输入文件缺少文本列：{missing}")
        return list(requested)
    found = [name for name in DEFAULT_TEXT_COLUMNS if name in frame.columns]
    if not found:
        raise KeyError(
            "未找到可用文本列。请用 --text-columns 明确指定；"
            f"现有列：{list(frame.columns)}"
        )
    return found


def compose_texts(frame: pd.DataFrame, columns: list[str]) -> list[str]:
    def compose(row: pd.Series) -> str:
        values: list[str] = []
        for column in columns:
            value = row[column]
            if pd.notna(value):
                text = str(value).strip()
                if text and text not in values:
                    values.append(text)
        return "。".join(values)

    return frame.apply(compose, axis=1).tolist()


def normalize_sentiment(raw_label: str) -> str:
    label = raw_label.strip().lower().replace("_", " ")
    explicit = {
        "very negative": "负面",
        "negative": "负面",
        "neutral": "中立",
        "positive": "正面",
        "very positive": "正面",
        "负面": "负面",
        "中立": "中立",
        "正面": "正面",
    }
    if label in explicit:
        return explicit[label]
    if "negative" in label or "负" in label:
        return "负面"
    if "positive" in label or "正" in label:
        return "正面"
    if "neutral" in label or "中" in label:
        return "中立"
    return "无法判断"


def predict(
    texts: list[str],
    model_name: str,
    batch_size: int,
    max_length: int,
    offline: bool,
) -> tuple[list[str | None], list[float | None]]:
    tokenizer = AutoTokenizer.from_pretrained(model_name, local_files_only=offline)
    model = AutoModelForSequenceClassification.from_pretrained(
        model_name,
        local_files_only=offline,
        trust_remote_code=False,
    )
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    model.to(device)
    model.eval()
    id2label = {int(key): str(value) for key, value in model.config.id2label.items()}

    labels: list[str | None] = [None] * len(texts)
    scores: list[float | None] = [None] * len(texts)
    valid_positions = [index for index, text in enumerate(texts) if text.strip()]
    ranges = range(0, len(valid_positions), batch_size)
    for start in tqdm(ranges, desc="predict", unit="batch"):
        positions = valid_positions[start : start + batch_size]
        batch = [texts[index] for index in positions]
        encoded = tokenizer(
            batch,
            padding=True,
            truncation=True,
            max_length=max_length,
            return_tensors="pt",
        )
        encoded = {name: value.to(device) for name, value in encoded.items()}
        with torch.inference_mode():
            probabilities = torch.softmax(model(**encoded).logits, dim=-1)
        confidence, label_ids = probabilities.max(dim=-1)
        for position, label_id, score in zip(
            positions,
            label_ids.cpu().tolist(),
            confidence.cpu().tolist(),
        ):
            labels[position] = id2label[int(label_id)]
            scores[position] = float(score)
    return labels, scores


def write_frame(frame: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.suffix.lower() == ".csv":
        frame.to_csv(path, index=False, encoding="utf-8-sig")
    elif path.suffix.lower() == ".xlsx":
        frame.to_excel(path, index=False, sheet_name="情感预测")
    else:
        raise ValueError(f"不支持的输出格式：{path.suffix}")


def main() -> int:
    args = parse_args()
    source = args.input.resolve()
    destination = args.output.resolve()
    if source == destination:
        raise ValueError("输出路径不能与输入路径相同，避免覆盖原始数据")
    if args.batch_size < 1 or args.max_length < 8:
        raise ValueError("batch-size 必须大于 0，max-length 必须至少为 8")
    if not 0 <= args.review_threshold <= 1:
        raise ValueError("review-threshold 必须位于 0 到 1 之间")

    frame = read_frame(source, args.sheet)
    columns = select_columns(frame, args.text_columns)
    texts = compose_texts(frame, columns)
    raw_labels, scores = predict(
        texts,
        args.model,
        args.batch_size,
        args.max_length,
        args.offline,
    )
    frame["sentiment_raw_label"] = raw_labels
    frame["sentiment"] = [
        normalize_sentiment(label) if label is not None else "无法判断"
        for label in raw_labels
    ]
    frame["sentiment_score"] = scores
    frame["sentiment_needs_review"] = [
        score is None or score < args.review_threshold for score in scores
    ]
    frame["sentiment_model"] = args.model
    write_frame(frame, destination)
    print(f"完成：{len(frame)} 条 -> {destination}")
    print(frame["sentiment"].value_counts(dropna=False).to_dict())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


# 情感倾向预测

`predict_sentiment.py` 使用 Hugging Face 文本分类模型，把标题、正文或摘要合并后预测为“正面 / 中立 / 负面”。它适合初步统计，不应代替人工舆情研判。

## 安装

```powershell
python -m pip install -r requirements-models.txt
```

## 示例

```powershell
python analysis/predict_sentiment.py `
  --input "data/final/网销/微博—互联网销售.csv" `
  --output "outputs/微博—互联网销售_情感预测.xlsx"
```

脚本默认使用 `tabularisai/multilingual-sentiment-analysis`。首次运行会从 Hugging Face 下载模型；也可以通过 `--model` 指定本地目录或其他兼容的文本分类模型。

常用参数：

- `--text-columns`：指定文本列，可传多个列名；未指定时自动寻找标题、正文、摘要等列。
- `--batch-size`：推理批量大小，默认 32。
- `--review-threshold`：置信度低于此值时标记为需要复核，默认 0.60。
- `--offline`：只使用本地已有模型，不联网下载。

输出会保留全部原始列，并新增 `sentiment`、`sentiment_score`、`sentiment_raw_label`、`sentiment_model` 和 `sentiment_needs_review`。原始数据不会被覆盖。


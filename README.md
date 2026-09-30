# 多平台公开舆情采集与分析项目

这是一个面向公开搜索结果的多平台数据采集、整理和初步情感分析项目。当前包含 B站、微博、抖音、小红书、今日头条和腾讯新闻六个平台，覆盖 2025 年天津地区四类主题：养老服务、政务服务、公共文化和互联网销售。

> 本项目仅用于采集公开可搜索信息。请遵守目标网站的服务条款、robots 规则、账号规范以及适用法律；不要绕过验证码、访问控制或其他安全措施。站点页面和接口会变化，采集结果不代表平台全量数据。

## 项目内容

```text
.
├─ bilibili/          B站采集脚本
├─ weibo/             微博采集脚本
├─ douyin/            抖音采集脚本与 Notebook
├─ xiaohongshu/       小红书采集与估算脚本
├─ toutiao/           今日头条采集脚本
├─ tencent_news/      腾讯新闻采集脚本
├─ analysis/          情感倾向预测模块
├─ configs/           原始关键词工作簿
├─ data/final/        6 平台 × 4 主题，共 24 份最终数据
├─ data/analysis-results/  已生成的清洗、预测和主题分析结果
└─ docs/reports/      项目报告文档
```

## 环境

- Python 3.10 或更新版本
- Windows 10/11（浏览器自动化脚本主要在 Windows + Edge 环境验证）
- 已安装 Microsoft Edge

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m playwright install chromium
```

如需运行情感预测，再安装：

```powershell
python -m pip install -r requirements-models.txt
```

## 配置输出位置

脚本默认把新结果写到项目根目录的 `outputs/<platform>`。如需统一写到其他磁盘，请设置环境变量：

```powershell
$env:CRAWLER_OUTPUT_ROOT = "D:\crawler-output"
```

仓库不包含登录 Cookie、浏览器会话或个人电脑绝对路径。需要登录的平台会创建本地浏览器配置目录，这些目录已由 `.gitignore` 排除，禁止提交。

## 运行入口

各平台参数并不完全相同，先查看帮助：

```powershell
python bilibili/bilibili_crawler.py --help
python weibo/weibo_crawler.py --help
python douyin/douyin_fast_crawler.py --help
python xiaohongshu/xiaohongshu_search_probe.py --help
python toutiao/toutiao_crawler.py --help
python tencent_news/tencent_news_crawler.py --help
```

批量任务的关键词配置位于各平台目录或 `*/keywords/`。运行前建议先用 1 个关键词、1 页或少量滚动做连通性测试，再启动批量任务。已有脚本支持的断点、限速和人工验证流程以各平台 README 与命令行帮助为准。

## 情感预测

```powershell
python analysis/predict_sentiment.py `
  --input "data/final/网销/微博—互联网销售.csv" `
  --output "outputs/微博—互联网销售_情感预测.xlsx"
```

预测模块使用已训练的 Hugging Face 文本分类模型，输出正面、中立、负面及置信度。它可以直接推理，无需先微调；但跨平台文本、反讽、政策新闻和短标题容易误判，低置信度结果应人工复核。若后续有人工标注样本，再做领域微调会更可靠。

## 数据说明

- `data/final`：24 份采集最终数据，保持原文件内容不变。
- `data/analysis-results`：基于最终数据生成的去重、情感预测、议题分类和图表结果。
- 不同平台可公开获得的字段不同，空值不一定代表采集失败。
- 数据具有采集时点性、搜索排序偏差和平台展示上限，不应表述为平台“全量”。

## 安全与隐私

- 不在代码中保存 Cookie、Token、手机号、账号密码或个人绝对路径。
- `.env`、浏览器 Profile、日志和临时进度文件不会被 Git 跟踪。
- 若本地脚本需要认证，登录状态只应保存在本机被忽略的 Profile 目录。
- 对外共享数据前，请自行确认数据使用授权与个人信息处理要求。


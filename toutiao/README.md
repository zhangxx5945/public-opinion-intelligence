# 今日头条公开搜索测试版

本目录用于低频采集今日头条网页端公开搜索结果。它不会调用私有接口、绕过登录、验证码或访问限制。请仅在得到适当许可并符合网站规则的范围内使用。

## 四类关键词

- 养老服务
- 政务服务
- 公共文化
- 互联网销售

每类包含 20 个代表性关键词，配置位于 `toutiao/keywords/`。

## 登录或验证

当前默认使用已登录的桌面搜索页和分页结果。如果网站要求登录或验证，采集器会打开项目专用的可见 Edge 窗口。请在窗口中手动扫码或完成验证，然后关闭整个 Edge 窗口，脚本会使用保存的会话重试。

也可以主动打开登录窗口：

```powershell
.\.venv\Scripts\python.exe .\toutiao\toutiao_login.py
```

## 单关键词测试

```powershell
.\.venv\Scripts\python.exe .\toutiao\toutiao_crawler.py `
  --keyword "天津 养老服务" `
  --target-year 2025 `
  --max-pages 10 `
  --output .\toutiao\data\test.csv `
  --checkpoint .\toutiao\data\test_checkpoint.csv `
  --log .\toutiao\data\test.log
```

脚本默认逐页读取桌面版“综合”搜索结果，最多 10 页；只保留页面中能够明确识别为 2025 年的结果。可通过 `--max-pages` 调整每个关键词的页数上限。

## 按类别批量运行

```powershell
.\.venv\Scripts\python.exe .\toutiao\toutiao_batch_2025.py --category "养老服务"
```

可选类别：`养老服务`、`政务服务`、`公共文化`、`互联网销售`、`全部`。

先测试一个关键词：

```powershell
.\.venv\Scripts\python.exe .\toutiao\toutiao_batch_2025.py `
  --category "养老服务" `
  --max-keywords 1
```

默认输出目录为：

```text
outputs/toutiao
```

每个类别生成一张 CSV，并包含 `keyword` 列；运行状态和日志分别写入对应子目录。批处理支持断点续跑，默认不会覆盖已完成关键词。

## 当前限制

- 今日头条公开搜索页按相关性排序，没有可靠的网页端全量年份筛选。
- 测试版只采集搜索页公开返回且日期能明确判断为 2025 年的结果，不能宣称是平台全量数据。
- 搜索页可能随时改版；脚本会在无法识别结果、登录或验证时明确停止或弹出人工处理窗口。

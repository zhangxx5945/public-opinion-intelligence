# 新浪微博公开内容采集项目

此目录用于新浪微博公开内容采集器开发。

## 登录助手

批量运行前如果搜索页提示登录，先在项目根目录运行：

```powershell
.\.venv\Scripts\python.exe .\weibo\weibo_login.py
```

在项目专用 Edge 窗口中正常登录，确认微博搜索页可用后关闭整个窗口。
脚本会继续复用 `weibo/.weibo_profile`，不读取或导出 Cookie。

## 当前测试脚本

`weibo_keyword_test.py` 使用 Scrapling 和本机 Microsoft Edge 请求新浪微博公开关键词搜索页，提取少量公开帖子并导出 CSV。浏览器模式会保存独立的 `.weibo_profile` 访客会话。

在项目根目录运行：

```powershell
.\.venv\Scripts\python.exe .\weibo\weibo_keyword_test.py `
  --keyword "人工智能" `
  --max-pages 1 `
  --max-results 10
```

首次需要正常登录时运行：

```powershell
.\.venv\Scripts\python.exe .\weibo\weibo_keyword_test.py `
  --keyword "人工智能" `
  --max-pages 1 `
  --max-results 5 `
  --headed `
  --initial-wait 60
```

请在脚本打开的浏览器窗口中正常登录，不要关闭窗口。登录状态保存在 `weibo/.weibo_profile`，后续运行会复用；脚本不绕过登录或验证码。

## 正式采集脚本

`weibo_crawler.py` 默认采集关键词“天津 养老服务”，时间范围为 `2026-01-01` 到运行当天。脚本按天拆分搜索区间，每天自动翻页，不设置总结果数量上限，并提供全局去重、检查点、断点续跑和日志。

```powershell
.\.venv\Scripts\python.exe .\weibo\weibo_crawler.py
```

默认输出：

- `data/weibo_tianjin_elderly_results.csv`：完成后的正式结果
- `data/weibo_tianjin_elderly_checkpoint.csv`：每页更新的检查点
- `data/weibo_tianjin_elderly_rejected.csv`：日期或主题不合格的数据及拒绝原因
- `data/weibo_tianjin_elderly_progress.json`：已完成日期
- `data/weibo_tianjin_elderly_quality.json`：完整率、重复率和日期一致性质量报告
- `logs/weibo_tianjin_elderly.log`：运行日志

正式结果必须同时满足：发布日期属于当前搜索日期、正文包含“天津”、正文包含养老服务或养老相关词。脚本只跟随页面真实提供的下一页；推荐内容不会作为搜索结果继续翻页。

中断后执行相同命令会跳过已完成日期并继续。只有确实需要从头重跑时才添加 `--fresh`。

单个页面超时会自动重试3次；仍失败的日期会记录到进度文件的 `failed_dates`，然后继续后面的日期，不会再因一个页面超时终止整个任务。

小范围验证示例：

```powershell
.\.venv\Scripts\python.exe .\weibo\weibo_crawler.py `
  --start-date "2026-07-15" `
  --end-date "2026-07-15" `
  --max-pages-per-day 2 `
  --output ".\weibo\data\weibo_formal_test.csv" `
  --checkpoint ".\weibo\data\weibo_formal_test_checkpoint.csv" `
  --progress ".\weibo\data\weibo_formal_test_progress.json" `
  --log ".\weibo\logs\weibo_formal_test.log"
```

输出文件：`weibo_keyword_test.csv`

测试版使用普通公开 HTTP 页面。如果页面要求登录、显示验证码或不返回公开帖子，脚本会明确停止，不会绕过访问限制。

计划包含：

- 关键词与日期配置
- 公开微博内容采集或官方 API 接入
- 内容 ID 去重
- 断点续跑和运行日志
- CSV/Excel 导出

只采集公开可见且获准使用的数据，不绕过登录、验证码或访问权限。

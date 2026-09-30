# 腾讯新闻公开搜索采集器

本目录采集腾讯新闻网页端公开搜索返回的数据。默认筛选发布时间明确属于 2025 年的结果，不绕过登录、验证码或访问限制。

## 数据范围

- 四类：养老服务、政务服务、公共文化、互联网销售。
- 每类 20 个关键词。
- 每类生成一张 CSV，所有关键词放在同一张表，通过 `keyword` 列区分。
- 保留标题、摘要、来源、发布时间、内容类型、互动量和链接等公开字段。

腾讯新闻的 PC 搜索结果接口目前无需登录；网页上的未登录提示只针对个性化广告设置。若以后平台改变访问规则，应停止任务并重新检查，而不是绕过验证。

## 单关键词测试

```powershell
.\.venv\Scripts\python.exe .\tencent_news\tencent_news_crawler.py `
  --keyword "天津 养老服务" `
  --target-year 2025 `
  --max-pages 3 `
  --output .\tencent_news\data\test.csv `
  --checkpoint .\tencent_news\data\test_checkpoint.csv `
  --progress .\tencent_news\data\test_progress.json `
  --log .\tencent_news\data\test.log `
  --fresh
```

`--max-pages 0` 表示按照平台报告的公开结果总页数继续读取；测试时建议设为 `3`。

## 四类批量运行

```powershell
.\.venv\Scripts\python.exe .\tencent_news\tencent_news_batch_2025.py --category "全部"
```

默认输出到：

```text
outputs/tencent_news
```

批处理支持断点续跑；重新执行同一命令时会跳过已经完成的关键词。

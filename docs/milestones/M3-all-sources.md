# M3 — 全信息源接入

> 前置依赖：[M2-filter-dedup.md](./M2-filter-dedup.md)
> 状态：未开始
> 关联文档：[04-roadmap.md](../04-roadmap.md) §2.1 / §2.2（本里程碑的权威规则来源）· §4 M3

## 目标

接入剩余 13 个源，实现四种 `fetch_method`（rss/api/webfetch/script）的完整版本。

## Scope（范围内）

- 补全源注册表全部 14 条配置
- api 方式：限流礼仪 + 日期回退策略
- webfetch 方式：日期优先级规则 + 绝对日期合理性校验 + 相对路径补全
- script 方式：诊断统计字段透传
- 统一错误处理
- 源健康检查状态机（alive/degraded/dead）

## Out of scope（明确不做）

- Enrich / Aggregate 阶段规则（留 M4/M5）

## 任务清单

- [ ] 补全源注册表全部 14 条配置（04 §2.1 表格）：
  - T1：`openai-rss`（rss）/ `deepmind-rss`（rss）/ `arxiv-api`（api，专用脚本封装限流 3 秒/次，cs.AI/cs.LG）/ `huggingface-daily-papers`（api，社区点赞策展）
  - T2：`import-ai`（rss，政策/治理深度周评）/ `interconnects`（rss，训练/RLHF 硬核评论，部分付费墙仅摘要）/ `qbitai`（rss，中文高频源，需过滤融资/PR软文）/ `air-street-press`（rss，投资层里少见的干净结构化源）
  - T3：`anthropic-news`（webfetch，无官方RSS）/ `meta-ai-blog`（webfetch，degraded，更新极慢）/ `the-batch`（webfetch，吴恩达周评质量高）/ `jiqizhixin`（script，微信公众号镜像）/ `a16z-news-content`（script，投资+VC bias）/ `state-of-ai`（webfetch，年度报告锚点，平日基本无新条目）
- [ ] api 方式限流礼仪：`arxiv-api` 固定间隔 3 秒/次
- [ ] api 方式日期回退策略：今天无数据尝试昨天，再无返回空且不算错误
- [ ] webfetch 方式日期优先级规则：同时存在相对日期（"3天前"）和绝对日期时优先用相对日期换算
- [ ] webfetch 方式绝对日期合理性校验：与当前时间差过大 → 低置信度
- [ ] webfetch 方式相对路径 URL 补全为绝对路径
- [ ] script 方式 `jiqizhixin`：从 `content:encoded` 提取 mp 直链
- [ ] script 方式 `a16z-news-content`：列表页缺日期，需详情页抓 `datePublished`
- [ ] script 方式诊断统计字段透传：脚本自带的成功数/降级数等字段必须原样透传，不能丢弃（旧系统教训：丢弃这类字段导致过一次异常事后无法确诊）
- [ ] 统一错误处理：任何一种方式失败都要产出"空条目数组+错误原因"，不能完全不产出；0 条结果不算错误
- [ ] 源健康检查状态机：`last_verified` 超 30 天告警；连续失败先标 `degraded`（仍抓，过滤阶段降权），再连续失败才考虑 `dead`/拉黑

## 验收标准

- [ ] 一次跑能处理全部 14 源
- [ ] `reliability` 状态机（alive/degraded/dead）正常工作

## 备注 / 风险

- `low_confidence` 判定条件（04 §2.2）在四种抓取方式里都可能触发：标题/URL 歧义、摘要严重缺失、发布日期无法解析、URL 非文章直链、发布日期与抓取时间差异常——接入每个新源时都要过一遍这条判定，不要只在 webfetch 类源上检查。

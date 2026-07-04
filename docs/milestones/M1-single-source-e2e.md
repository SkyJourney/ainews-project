# M1 — 单源端到端最小管道

> 前置依赖：[M0-skeleton.md](./M0-skeleton.md)
> 状态：未开始
> 关联文档：[04-roadmap.md](../04-roadmap.md) §2.1 / §2.2 / §2.3 / §2.4 / §2.5 / §2.6（各阶段最简版）· §4 M1

## 目标

选 1 个最简单源（`openai-rss`，纯 RSS 无特殊兜底需求），把 Fetch → Filter → Enrich → Aggregate → Write 全链路用最简版本跑通一次，验证"管道形状"本身是对的。

## Scope（范围内）

- 最简版 `preflight_activity`（源健康检查）
- `fetch_activity`（仅 rss 方式）
- `filter_activity`（先只做同批次去重 + 14 天时效过滤；跨日去重 / 信噪过滤放 M2）
- `enrich_activity`（单条：抓原文 + 翻译判断，元数据抽取先做最简版）
- `aggregate_activity`（最简单版本，不做 `is_new` 判断，先固定当已有 topic 处理）
- `write_activity`（写入 `documents`，先只支持 zettel 类型）

## Out of scope（明确不做）

- 多源接入（留 M3）
- 完整 filter 规则：跨日去重、信噪比过滤表、跨源论文去重（留 M2）
- 完整 enrich 三态与 Fallback A/B、翻译完整性机械校验、配图分级渲染（留 M4）
- 完整 aggregate 聚类规则、`is_new` 判定、Zettel 复用三级判断、Daily/Topic/Digest 写作规则（留 M5）
- daily / topic / digest / original 四种文档类型（留 M4/M5）

## 任务清单

- [ ] 源注册表雏形：录入 `openai-rss` 一条配置记录（字段见 04 §2.1：`name`/`tier`/`perspective`/`fetch_method`/`reliability`/`last_verified`）
- [ ] `preflight_activity` 最简实现：检查 `last_verified` 是否超 30 天
- [ ] `fetch_activity` 的 rss 分支：直接提取条目列表，输出统一 entry schema `{title, url, published, raw_summary, low_confidence, extra}`（04 §2.2）
- [ ] 核查 fetch 阶段铁律：确认未做任何时间窗口过滤，全量返回（04 §2.2 明确写"抓取阶段不做任何时间窗口过滤"，这是旧系统教训，不要在这一步埋雷）
- [ ] `filter_activity` 最简版：同批次去重先只做 4 级优先中的前两级（① URL 完全相同 ② URL host+path 相同，忽略 query；③④留 M2）+ 14 天时效过滤（发布日期距今 >14 天丢弃，标 `stale:Nd_old`）
- [ ] `enrich_activity` 单条链路：抓原文先只做"主抓取成功"这一态（三态与 Fallback A/B 留 M4）+ 翻译判断（非中文则翻译）+ 元数据抽取最简版（先只做一段话摘要 `gist`）
- [ ] `aggregate_activity` 最简版：不做 topic 聚类 / `is_new` 判断，固定归入一个占位 topic
- [ ] `write_activity`：写入 `documents` 表，仅支持 `doc_type='zettel'`，frontmatter 用最小字段集

## 验收标准

- [ ] 定时或手动触发一次，从 `openai-rss` 这 1 个真实 RSS 源抓到内容，走完整链路
- [ ] `documents` 表里能查到结构完整的 zettel 记录（frontmatter / body_md 齐全）

## 备注 / 风险

- 本里程碑刻意省略大量规则细节，目的是先验证管道骨架，不要在这一步提前实现 M2/M4/M5 的规则——提前做了后续里程碑反而不好验收谁贡献了什么。

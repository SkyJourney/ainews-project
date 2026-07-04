# M1 — 单源端到端最小管道

> 前置依赖：[M0-skeleton.md](./M0-skeleton.md)
> 状态：**已完成**（2026-07-04）
> 关联文档：[04-roadmap.md](../04-roadmap.md) §2.1 / §2.2 / §2.3 / §2.4 / §2.5 / §2.6（各阶段最简版）· §4 M1
> 后续：M2-M4 从「逐个里程碑独立验收」调整为「合并成一个连续阶段推进」，理由与调整记录见 `.claude/memory/decisions.md`「M1 单独验收，M2-M4 合并成一个连续阶段推进」。

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
- 完整 enrich 三态与 Fallback A/B（状态③ WebFetch 等价物 + 占位正文）、翻译完整性机械校验、配图分级渲染（留 M4）
- 完整 aggregate 聚类规则、`is_new` 判定、Zettel 复用三级判断、Daily/Topic/Digest 写作规则（留 M5）
- daily / topic / digest / original 四种文档类型（留 M4/M5）

## 任务清单（全部完成，落地方式见下）

- [x] 源注册表雏形：`backend/config/sources.yaml` 录入 `openai-rss`（`url: https://openai.com/news/rss.xml`，WebFetch 实测验证过是有效 feed）
- [x] `preflight_activity`（`backend/worker/fetch.py`）：检查 `last_verified` 是否超 30 天，超则 `activity.logger.warning`
- [x] `fetch_activity` 的 rss 分支（`backend/worker/fetch.py`）：`httpx` 下载 + `feedparser` 解析，输出统一 `Entry` pydantic schema（`backend/worker/schemas.py`）；真实跑一次抓到 1028 条（openai-rss 是全量历史 feed，不是只有近期条目）
- [x] 核查 fetch 阶段铁律：`fetch_activity` 不做任何时间窗口过滤，全量返回，验证方式见下方"关键实测发现"
- [x] `filter_activity`（`backend/worker/filter.py`）：同批次去重①URL完全相同②host+path相同（忽略query）+ 14 天时效过滤；真实数据验证 1028→16 条，去重与时效丢弃逻辑符合预期
- [x] `enrich_activity`（`backend/worker/enrich.py` + `EnrichArticleWorkflow` in `backend/worker/workflows.py`）：抓原文（httpx+trafilatura，状态①）+ **状态②** Jina Reader 兜底（04 §2.4，见下方"关键实测发现"）+ 翻译判断（CJK 字符占比启发式）+ 分块翻译（用户明确要求的基础版，按段落贪心分块）+ 一段话摘要
- [x] `aggregate_activity`（`backend/worker/aggregate.py`）：不做 topic 聚类/`is_new` 判断，固定 `topic="uncategorized"` 占位；12 位分钟时间戳 ID + 默认 slug 方案（标题字母数字词转 kebab-case）+ 同 HHMM 冲突顺延
- [x] `write_activity`（`backend/worker/write.py`）：upsert 进 `documents` 表，`doc_type='zettel'`，frontmatter 最小字段集（`title`/`doc_type`/`source_name`/`source_url`/`gist`/`topic`/`fallback_notice`）；`tags`/`links` 表本阶段不写（四轴打标策略要到 M5 才有意义实现）

## 验收标准（全部通过）

- [x] 手动触发一次真实端到端跑（经真实 Temporal Server + Worker，非直接函数调用）：`{'batch_id': 'e2e-test-1', 'fetched': 1028, 'kept': 16, 'enrich_failed': 0, 'written': 16}`——16 篇全部成功，0 失败
- [x] `documents` 表查到 16 条结构完整的 zettel 记录，frontmatter/body_md 齐全，`fallback_notice` 字段正确反映 Jina 兜底状态

## 关键实测发现（会影响后续里程碑，务必读）

M1 的"先用 1 个源验证管道骨架"这个设计目标充分体现了价值——过程中暴露了三个非预期问题，全部已修复并记录进 `.claude/memory/decisions.md`：

1. **openai.com 文章页挂 Cloudflare 反爬挑战**：`fetch_original_activity` 直连 httpx 得到 403（`cf-mitigated: challenge`），换浏览器 UA 也无效。参考旧项目 `fetch-with-assets.py`/`news-originalizer.md` 已验证的设计，提前把 04 §2.4 状态②（Jina Reader 兜底）实现到 M1（状态③ Fallback A/B 仍留 M4）。触发条件（400/403/429/503/超时）与旧系统完全对齐。`articles` 表新增 `fetch_channel`/`published_at` 两列（migrations 0002/0003）支撑这个逻辑。
2. **Instructor 的 `Mode.TOOLS` 会忽略显式传入的 `tool_choice="auto"`**：M0 结论"`tool_choice` 统一用 auto"只在裸 `openai` SDK 层面成立，经 Instructor 时必须改用 `Mode.JSON`（不涉及 tool_choice）。
3. **`deepseek-v4-flash` 偶发把翻译结果整个写进 `reasoning_content`、`content` 字段留空**：这是 DeepSeek 官方文档承认的已知问题，不是随机抽风也不是 token 预算问题；`max_retries` 从 0 改为 1（让 Instructor 把校验错误反馈给模型重新生成）后，此前稳定复现失败的多个分块（含一段基因组学数据表格）反复测试均 100% 成功，16 篇真实文章端到端 0 失败验证了这个修复的可靠性。

## 备注 / 风险

- 本里程碑刻意省略大量规则细节，目的是先验证管道骨架；除了上述两个"不做就跑不通"的必要修复外，没有提前实现 M2/M4/M5 的完整规则。

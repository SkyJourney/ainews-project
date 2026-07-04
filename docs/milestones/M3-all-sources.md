# M3 — 全信息源接入

> 前置依赖：[M2-filter-dedup.md](./M2-filter-dedup.md)
> 状态：**已完成**（2026-07-04，作为 M2+M3+M4 合并阶段的 Stage B）
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

## 任务清单（全部完成，落地方式见下）

- [x] 补全源注册表全部 14 条配置（`backend/config/sources.yaml`），每条 URL 都用真实 HTTP 请求核实过有效
- [x] api 方式限流礼仪：`arxiv-api` 查询 cs.AI/cs.LG 两个分类，间隔 3 秒/次
- [x] api 方式日期回退策略：`huggingface-daily-papers` 查询今天日期无数据（真实撞到过 0 条）时自动退查昨天
- [x] webfetch 方式：httpx 抓列表页 HTML → 经 LiteLLM 用 `PageListing` schema（04 §2.2 唯一需要 LLM 参与 Fetch 阶段的场景）抽取条目列表；相对日期（"N天前"/"昨天"）优先于绝对日期换算；绝对日期用 `dateutil` 解析 + 合理性校验（与当前时间差 >3650 天视为不可信）；相对路径 URL 用 `urljoin` 补全绝对路径
- [x] script 方式 `jiqizhixin`：从 anyfeeder 镜像 RSS 的 `content:encoded` 提取 mp.weixin.qq.com 直链（实测发现该镜像当前版本的 `content:encoded` 已不含 mp 直链，与旧系统当年的观察不同——正确走了兜底回退到搜狗中间页 + 标 `low_confidence=true`，这是设计好的降级行为不是 bug）
- [x] script 方式 `a16z-news-content`：列表页抓 title/url/category，详情页串行抓 `datePublished`（0.5s 礼仪间隔）
- [x] script 方式诊断统计字段透传：`jiqizhixin`（with_mp_link/fallback_link 计数）、`a16z`（detail_fetched/detail_failed 计数）都用 `activity.logger.info` 记录——04 §2.2 明确的旧系统教训（丢弃这类字段导致一次异常无法确诊），这次改用 Temporal 原生日志承担这层可观测性，不再需要单独的统计字段传递机制
- [x] 统一错误处理：0 条结果不算错误（`state-of-ai` 平日常态就是 0 条，符合 04 §2.1 对该源的备注）
- [x] 源健康检查状态机：新增 Postgres 表 `source_health`（migration 0005），`preflight_activity` 读取运行时状态（首次见到某源时用 `sources.yaml` 静态值播种）；`fetch_activity` 结束后由 workflow 记录成功/失败，连续失败按阈值升级 degraded→dead；`degraded` 源的产出条目自动标 `low_confidence=true`（"过滤阶段降权"的具体实现）

## 验收标准

- [x] 一次跑能处理全部 14 源——真实测试：14 源全部成功，0 sources_failed
- [x] `reliability` 状态机（alive/degraded/dead）正常工作——`source_health` 表验证过成功清零计数、失败按阈值升级两条路径

## 落地方式说明

- **架构改造**：`fetch_activity` 从 M1 的"单源单次调用"改造成"按活跃源 fan-out"——`AInewsPipelineWorkflow` 新增 `list_active_sources_activity` 读取 sources.yaml 的活跃源列表，用 `asyncio.gather` 并发跑每个源的 preflight+fetch，合并全部 `Entry` 后统一交给 filter_activity 一次处理（`PipelineParams` 相应去掉了 `source_name` 字段）。详见 `.claude/memory/decisions.md`「fetch_activity 从"单源单次调用"改为"按活跃源 fan-out，一个 batch 覆盖全部源"」
- 真实批量验证：14 源一次跑，1376-1385 条原始条目（数量随时间波动，因为部分源如 huggingface-daily-papers 按日期滚动）

## 备注 / 风险

- `low_confidence` 判定条件在四种抓取方式里都验证过会触发：webfetch 的日期解析失败、api 的字段缺失、script 的降级回退（jiqizhixin 搜狗中间页兜底）都命中过真实数据。

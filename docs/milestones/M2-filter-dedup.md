# M2 — 过滤/去重规则完整化

> 前置依赖：[M1-single-source-e2e.md](./M1-single-source-e2e.md)
> 状态：**已完成**（2026-07-04，作为 M2+M3+M4 合并阶段的 Stage A+C）
> 关联文档：[04-roadmap.md](../04-roadmap.md) §2.3（本里程碑的权威规则来源）· §4 M2

## 目标

把 `filter_activity` 从 M1 的最简版补全为 04 §2.3 定义的完整规则集。

## Scope（范围内）

完整实现 04 §2.3 全部规则：执行顺序、同批次去重四级优先、跨源同论文去重、时效过滤、信噪比过滤、模糊地带兜底、跨日去重 30 天索引、统计自检。

## Out of scope（明确不做）

- Enrich / Aggregate 阶段的规则（留 M4/M5）

## 任务清单（全部完成，落地方式见下）

- [x] 执行顺序：同批次去重 → 跨源同论文去重 → 时效过滤 → 信噪比过滤 → 跨日去重（`backend/worker/filter.py` `filter_activity`）
- [x] 同批次去重补全 4 级优先：①URL 完全相同 ②host+path 相同 ③标题 Jaccard 相似度≥0.85 ④摘要前 100 字 Jaccard 重叠≥0.9（命中即判重复，保留信息最完整一条——完整度按"有发布日期/摘要更长/非 low_confidence"打分）
- [x] 跨源同论文去重：范围限 `arxiv-api`/`huggingface-daily-papers`，标题归一化（小写去标点）后精确相等即合并，保留 arxiv.org 规范链接源版本，其余并入 `also_reported_by`（Stage C 修正：同一个源自己内部撞车不算跨源，不计入 `also_reported_by`）
- [x] 时效过滤：单一阈值，发布日期距今 >14 天丢弃，不分来源等级
- [x] 信噪比过滤表：丢弃类别（融资 PR/招聘/活动/广告/VC 软文/二手编译正则）+ 保留信号（arxiv.org/github.com/huggingface.co 域名 + benchmark/SOTA/开源/政策/安全对齐关键词）覆盖所有丢弃规则
- [x] 模糊地带兜底：沿用 low_confidence 字段承载，过滤阶段不主动新增判断，交聚类阶段
- [x] 跨日去重 30 天滚动窗口索引：新增 Postgres 表 `url_index`（migration 0004，替代旧系统的 JSON 文件方案）；URL 归一化（小写/去 scheme/去 www/去 query 锚点/去尾斜杠）
- [x] 跨日去重命中规则：≤7 天默认丢弃，除非 Jaccard 重叠≤0.6（标 `re_coverage=true`）；任一方摘要为空时不豁免（严格按 04 §2.3 原文，实测发现过一次因空摘要被错误豁免的 bug 并修复）；>7 天视为已淡出正常保留
- [x] Jaccard 重叠算法：中文按字切分去停用词，英文按空格切分小写去停用词
- [x] 索引维护：`first_seen_date`/`first_seen_run`/`title`/`source_name`/`kept_in_daily`/`zettel_id`/`raw_summary_excerpt` 字段齐全；`filter_cleanup_url_index` 清理超 30 天旧条目
- [x] 索引写权限约束：`backend/worker/db.py` 里 `filter_*` 系列函数（Filter 专用，全权限）vs `write_backfill_zettel_id`（Write 专用，仅回填 zettel_id）两组不同前缀的函数体现边界，不做数据库层面权限控制
- [x] 统计自检：`entries_after_dedup ≥ entries_after_filter ≥ 最终保留数` + 丢弃计数之和与总数对账，任一不成立直接 `raise RuntimeError`

## 验收标准

- [x] 统计自检（单调递减关系）跑通——真实 14 源批量数据反复验证无异常
- [ ] 能验证"复盘"条目正确复用旧 `zettel_id` 而不是重复创建——**这条依赖 M5 的 Zettel 复用三级判断**，M2 只负责把 `zettel_id` 正确回填进索引，"复用"这个动作本身是 M5 aggregate 阶段的职责，留到 M5 验收

## 落地方式说明

- 真实 14 源数据验证：1376 条原始条目 → 159 条保留，跨源论文去重命中 6 篇真实重复论文（arxiv-api + huggingface-daily-papers），信噪比过滤正确剔除 qbitai 的真实融资通稿
- 详细决策记录见 `.claude/memory/decisions.md`「跨日去重索引落地为 Postgres 表 url_index，访问边界靠函数命名而非数据库权限」

## 备注 / 风险

- 这一阶段的规则全部是"批量、纯规则、必须在 fan-out 前完成"——04 §2.3 强调去重本质是跨条目比较，不能拆到独立子线程里做，实现时没有为了并行化而破坏这个约束。

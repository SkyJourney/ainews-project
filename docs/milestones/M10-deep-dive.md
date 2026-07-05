# M10 — Deep Dive（跨天聚合深度解读，明确延后）

> 前置依赖：`documents` 表 `doc_type='digest'` 积累足够天数的历史（见下方"触发条件"）
> 状态：延后
> 关联文档：[04-roadmap.md](../04-roadmap.md) §4 M10；旧系统设计参考：`/Volumes/Projects/AInews` 的 `40-Deep-Dives/`、`SCHEMA.md`、`.claude/skills/ai-news/references/vault-schema.md`

## 背景

旧系统设计过"Deep Dive"（`40-Deep-Dives/`）——对 Digest 层做跨天/跨周二次聚合，识别"跨日延续主题、热门 topic 趋势线"，产出周报/月报形态的第五种内容类型。**这个功能在旧系统里从未真正实现过**：目录自建库以来只有一个空 `.gitkeep`，`git log` 无任何改动记录；对应的 `news-weekly-digester` subagent 从未创建；前端页面是"筹备中"空态。旧系统设计文档写的触发门槛是"**积累 ≥7 天 `30-Digests/` 历史**"——核实旧系统 `30-Digests/` 实际已有 8 天数据，说明当初卡住的不是数据不够，是功能本身没写完。

M7 生产化收尾讨论时用户提出"这个后续做成独立 Temporal 工作流是否合适"，确认这是一个值得追踪但尚不能开工的待办，故新增本里程碑文件占位，避免下次会话遗忘。

## 触发条件

**新系统 `documents` 表里 `doc_type='digest'` 积累足够天数历史后才启动**（沿用旧系统"≥7 天"这个设计门槛作为参考起点，实际数字到时候可以重新评估）。核实时点（2026-07-05）新系统只有 **1 天** Digest 历史（`digest-2026-07-05`），距离门槛还早。

## 范围（到时候再细化，当前只占位）

- 对 `documents WHERE doc_type='digest'` 做跨天/跨周二次聚合，不是重新解析 `articles`/cluster 原始数据
- 识别"跨日延续主题、热门 topic 趋势线"，产出新的文档类型（沿用 `digest` 扩展字段，还是新增独立 `doc_type` 如 `deep_dive`，留到设计阶段决定）
- 触发方式可以自然复用 M7 刚建立的 Temporal Schedule 模式（`ensure_pipeline_schedule` 的姊妹实现，新增一个周/月粒度的 Schedule，action 指向新的 workflow）

## 不做（当前阶段）

- 不参照旧系统的任何"已验证规则"——因为旧系统这个功能从未跑通过，没有可迁移的业务逻辑，是净新设计，不是迁移
- 不在数据门槛达成前预研聚合算法细节，避免像 M8 一样分散注意力

## 备注 / 风险

- 这是 M0-M7 里第一个"旧系统没有可迁移先例"的里程碑，启动时需要先做设计对齐（聚合窗口/判定规则/输出 schema/触发方式），走 CLAUDE.md 的抽象设计确认流程，不能直接照抄 04-roadmap.md 里其他里程碑"从老系统提炼规则"的做法
- 与 M8/M9 同级、互不阻塞，但触发条件不同（M8 依赖 M7 验收，M9 依赖明确的检索需求，M10 依赖 Digest 历史天数）

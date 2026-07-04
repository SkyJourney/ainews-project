# M7 — 生产化收尾

> 前置依赖：[M6-frontend.md](./M6-frontend.md)
> 状态：未开始
> 关联文档：[04-roadmap.md](../04-roadmap.md) §2.8 · §4 M7 · [03-architecture-proposal.md](../03-architecture-proposal.md) §7 待决问题（备份节奏等）

## 目标

补齐运维能力，让新系统能独立连续运行，具备退役旧 AInews Desktop Scheduled Task 的条件。

## Scope（范围内）

- Postgres 备份策略落地
- Celery Beat 对接真实定时任务
- 按需决定是否接入 Langfuse / `git_export`

## Out of scope（明确不做）

- 旧数据迁移（M8）
- 向量化/RAG（M9）

## 任务清单

- [ ] Postgres 备份策略落地：`pg_dump` 定期快照；是否需要 WAL 归档做 PITR，参照 03 §7 问题 7 的结论执行
- [ ] Celery Beat 对接真实定时任务，替代旧系统对 Desktop Scheduled Task + "始终保持电脑唤醒"的依赖
- [ ] （可选）按需决定是否接入 Langfuse，经 LiteLLM callback 零代码接入
- [ ] （可选）按需决定是否启用独立 `git_export` 任务（供 Obsidian 浏览/审阅历史，非权威、非关键路径）
- [ ] 编写运维手册：故障排查入口（Temporal Web UI / Postgres / LiteLLM 网关日志）

## 验收标准

- [ ] 新系统能独立连续运行至少 7 天不需要人工干预
- [ ] 此时可以考虑退役旧 AInews 的 Desktop Scheduled Task

## 备注 / 风险

- 这是"生产化收尾"里程碑，也是 00-overview.md 问题陈述里提到的"无法真正脱离 Claude Code 会话运行"这一旧痛点的最终验证点——7 天连续运行不是形式指标，要真的观察期间是否有需要人工介入补跑的情况。

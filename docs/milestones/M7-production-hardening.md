# M7 — 生产化收尾

> 前置依赖：[M6-frontend.md](./M6-frontend.md)
> 状态：**开发工作已完成（2026-07-05），进入 7 天连续运行观察期**（观察窗口预计到 2026-07-12，观察期内无需人工介入补跑才算真正验收通过，见下方"验收标准"）
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

## 任务清单（全部完成/决策，落地方式见下）

- [x] Postgres 备份策略落地：`pg_dump` 定期快照；是否需要 WAL 归档做 PITR，参照 03 §7 问题 7 的结论执行——**决策：仅 pg_dump 快照，不上 WAL/PITR**
- [x] 定时触发对接真实定时任务，替代旧系统对 Desktop Scheduled Task + "始终保持电脑唤醒"的依赖——**架构调整：改用 Temporal 原生 Schedule，不是原计划的 Celery Beat**（见下方落地方式说明）
- [x] （可选）按需决定是否接入 Langfuse，经 LiteLLM callback 零代码接入——**决策：暂不接入**
- [x] （可选）按需决定是否启用独立 `git_export` 任务（供 Obsidian 浏览/审阅历史，非权威、非关键路径）——**决策：暂不启用**
- [x] 编写运维手册：故障排查入口（Temporal Web UI / Postgres / LiteLLM 网关日志）——`docs/06-runbook.md`

## 验收标准

- [ ] 新系统能独立连续运行至少 7 天不需要人工干预（观察期进行中，开始日期 2026-07-05）
- [ ] 此时可以考虑退役旧 AInews 的 Desktop Scheduled Task

## 备注 / 风险

- 这是"生产化收尾"里程碑，也是 00-overview.md 问题陈述里提到的"无法真正脱离 Claude Code 会话运行"这一旧痛点的最终验证点——7 天连续运行不是形式指标，要真的观察期间是否有需要人工介入补跑的情况。
- 观察重点见 `docs/06-runbook.md` §6：每日 09:00 的 Schedule 是否按时触发、`written` 记录数是否稳定、`postgres-backup` 是否每天成功。

## 落地方式说明

### 架构级调整：定时触发从"Celery Beat 薄触发器"改为"Temporal 原生 Schedule"

立项时的既定方案是 Celery Beat（M0/M1 就已经接线，一直稳定运行到 M6），但用户在 M7 讨论中主动提出"Temporal 自带定时器，是不是可以取代 Celery Beat"。核实后确认这是纯粹的减法：

- Redis 在全代码库里唯一的用途就是 Celery broker（`grep` 全仓库确认零其他依赖）
- 已安装的 `temporalio==1.30.0` 原生支持 Schedule Client API（`create_schedule`/`ScheduleSpec`），Temporal Web UI 本身就能查看/暂停/手动触发/回填 schedule，可观测性比 Celery Beat 只能看容器日志更好

落地：`backend/worker/worker.py` 新增 `ensure_pipeline_schedule`，worker 启动时幂等 `create_schedule`（`ScheduleAlreadyRunningError` 则跳过）；`backend/beat/` 整个模块删除，`celery`/`redis` 依赖从 `requirements.txt` 移除，两份 docker-compose 的 `celery-worker`/`celery-beat`/`redis` 三个服务一并删除（真实部署用 `docker compose up -d --remove-orphans` 清理）。

唯一需要处理的细节：`PipelineParams.batch_id`（写进 `articles` 表参与跨日去重）原先由 Celery task 在 workflow 外部用 `datetime.now(timezone.utc)` 生成（当时的确定性约束要求"workflow 内部不允许自己取当前时间，必须外部传入"）。改用 Schedule 后不再有这层外部触发代码——`batch_id` 改为可选字段，workflow 内部用 `workflow.info().start_time`（该时间戳在 workflow 第一次启动时被记录进 Temporal 历史，之后任何 replay 都直接读取历史值，不会重新计算，满足同样的确定性约束）兜底生成。

**真实批次验证**：对着真实持久化部署用 `handle.trigger()` 手动触发一次完整批次，确认 `next_action_times` 正确换算成 UTC（09:00 Asia/Shanghai = 01:00 UTC），触发的 workflow 跑到底：`batch_id='2026-07-05-0503'`、`sources_attempted=14`/`sources_failed=0`、`enrich_failed=0`、`written=100`，`articles` 表按新 `batch_id` 正确分组，证明新路径与原 Celery 路径行为等价。详见 `.claude/memory/decisions.md`。

### Postgres 备份：pg_dump 定期快照，不上 WAL/PITR

新增 `infra/scripts/pg_backup.sh` + 两份 docker-compose 各自的 `postgres-backup` 服务（复用 `postgres:16-alpine` 镜像自带的 `pg_dump`，与 `db` 服务同版本）。默认每日 03:00 Asia/Shanghai（脚本内部用 UTC epoch 秒数算术实现，不依赖 GNU date 扩展语法，可移植）执行一次全量 `pg_dump -Fc` 快照，容器启动时也会立即跑一次；按 `BACKUP_RETENTION_DAYS`（默认 14 天）用 `find -mtime` 清理旧备份。决策依据（仅快照 vs WAL/PITR）见 `.claude/memory/decisions.md`。

**真实验证**：对着真实持久化部署跑通首次备份（生成 1.1M 的 `.dump` 文件，`pg_restore --list` 确认是合法的 34-entry custom format 归档）；用一份构造出 20 天前 mtime 的假文件验证保留策略清理逻辑正确触发、且不会误删当天的真实备份。恢复流程写进 `docs/06-runbook.md` §3，未做真实恢复演练（当前无需要恢复的场景）。

### 运维手册：`docs/06-runbook.md`

新增文档覆盖：服务清单与日常操作、Temporal 排查（Schedule 查询/手动触发/workflow 失败定位）、Postgres 直连与备份恢复、LiteLLM 网关排查入口、常见故障 checklist、退役旧 AInews 的判断依据。

### Langfuse / git_export：均明确决策暂缓

两者都是 03-architecture-proposal.md §7 待决问题里的可选项，用户在 M7 讨论时明确选择暂不做（Langfuse 先用 LiteLLM 自带日志；git_export 纯 Obsidian 浏览偏好，不影响系统运行），不是本次收尾遗漏，决策记录见 `.claude/memory/decisions.md`。

### 待观察：7 天连续运行

开发工作全部完成，但验收标准的"连续运行 7 天不需要人工干预"是观察期而非开发任务，观察起始日期 2026-07-05。观察期内需要留意：每日 09:00 Schedule 是否按时触发、`written` 记录数是否稳定在合理区间、`postgres-backup` 是否每天成功——满足后再考虑退役旧 AInews 的 Desktop Scheduled Task 并把本里程碑状态改为"已完成"。

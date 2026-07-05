# 运维手册

> M7 生产化收尾新增，供故障排查/日常运维使用。业务规则/架构设计仍以 [`04-roadmap.md`](./04-roadmap.md)、[`03-architecture-proposal.md`](./03-architecture-proposal.md) 为准，本文档只关心"东西没跑起来/跑错了怎么查"。

## 1. 服务清单与日常操作

真正长期运行的编排是 `/Volumes/Docker/compose/ainews-service/docker-compose.yml`（本机部署版）；仓库内 `infra/docker-compose.yml` 是可移植通用版，两份需手动同步（见 `CLAUDE.md`）。

```bash
cd /Volumes/Docker/compose/ainews-service

docker compose ps                    # 查看全部服务状态
docker compose logs -f temporal-worker   # 跟踪 worker 日志（Ctrl+C 退出，不影响服务运行）
docker compose restart temporal-worker   # 重启单个服务
docker compose up -d --remove-orphans    # 应用 compose 变更后同步（清理已删除的服务容器）
```

当前服务：`db`（Postgres）、`temporal`、`temporal-ui`、`temporal-worker`、`postgres-backup`、`web`。M7 起不再有 `celery-worker`/`celery-beat`/`redis`——定时触发已改用 Temporal 原生 Schedule（见 §2）。

## 2. Temporal：workflow 失败排查与手动重跑

**Web UI**：`http://localhost:8088`

- **看今天的 pipeline 有没有跑**：Web UI 左侧 Workflows，按 `AInewsPipelineWorkflow` 类型筛选，或 Schedules 页面找 `ainews-pipeline-daily`，查看 `Recent Runs`/`Next Run`。
- **看 Schedule 本身健不健康**（是否被暂停、下次触发时间对不对）：

  ```bash
  ~/miniconda3/envs/ainews-service/bin/python3 - <<'EOF'
  import asyncio
  from temporalio.client import Client

  async def main():
      client = await Client.connect("localhost:7233")
      desc = await client.get_schedule_handle("ainews-pipeline-daily").describe()
      print("paused:", desc.schedule.state.paused)
      print("next runs:", desc.info.next_action_times[:3])
      print("recent runs:", desc.info.recent_actions[-3:])

  asyncio.run(main())
  EOF
  ```

- **手动立即触发一次**（不用等到明天 09:00，比如验证一个刚部署的改动）：

  ```bash
  ~/miniconda3/envs/ainews-service/bin/python3 - <<'EOF'
  import asyncio
  from temporalio.client import Client

  async def main():
      client = await Client.connect("localhost:7233")
      await client.get_schedule_handle("ainews-pipeline-daily").trigger()

  asyncio.run(main())
  EOF
  ```

- **某个 workflow 失败了**：Web UI 打开对应 workflow，看 History 里最后一个失败的 activity（通常是 `enrich_activity`/`fetch_activity` 某个 activity 耗尽重试）。`AInewsPipelineWorkflow` 的设计原则是"单个源/单篇文章失败不阻塞其余部分"（见 04 §2.1/§2.4），所以大多数单点失败不需要人工干预，观察 `sources_failed`/`enrich_failed` 这两个返回字段的比例即可；只有 `filter_activity`/`aggregate_activity`/`write_activity`（批量、非 fan-out 的步骤）失败才会导致整个批次没有产出，需要看具体报错信息（常见原因见下方"常见故障"）。
- **不确定 worker 有没有正常连上 Temporal**：`docker compose logs temporal-worker`，正常启动无输出（没有异常 traceback 即为正常，activity 注册失败/Schedule 创建失败都会在启动时直接抛异常并让容器重启）。

## 3. Postgres：直连、备份、恢复

**直连**（本机排查用，应用本身走容器网络内的 `db:5432`）：

```bash
docker exec -it ainews-service-db psql -U ainews -d ainews_content
# 或本机装了 psql 客户端：psql -h localhost -p 15432 -U ainews -d ainews_content
```

**常用排查查询**：

```sql
-- 最近几次批次的规模
SELECT batch_id, count(*) FROM articles GROUP BY batch_id ORDER BY batch_id DESC LIMIT 5;

-- documents 表按 doc_type 分布
SELECT doc_type, count(*) FROM documents GROUP BY doc_type;
```

**备份**（`postgres-backup` 容器，M7 新增，见 `infra/scripts/pg_backup.sh`）：

- 每日 03:00 Asia/Shanghai（=19:00 UTC，避开 09:00 的 pipeline 批次）自动执行一次 `pg_dump`，容器启动时也会立即跑一次；输出到 `/Volumes/Docker/data/ainews-service/backups/`，按 `BACKUP_RETENTION_DAYS`（默认 14 天）清理旧备份。
- 只做 `pg_dump` 快照，不做 WAL 归档/PITR——纯资讯聚合内容可接受"最多丢失一天"的 RPO（决策依据见 `.claude/memory/decisions.md`）。
- 查看备份日志：`docker logs ainews-service-postgres-backup`
- 手动立即跑一次备份（不等下个定时点）：

  ```bash
  docker exec ainews-service-postgres-backup pg_dump -h db -U ainews -d ainews_content -Fc \
    -f /backups/ainews_content-manual-$(date -u +%Y%m%dT%H%M%SZ).dump
  ```

**恢复**（灾难恢复场景，会覆盖现有数据，执行前确认目标库确实要被覆盖）：

```bash
# 1. 选一份备份文件（自定义格式 .dump，用 pg_restore 而不是 psql）
ls -la /Volumes/Docker/data/ainews-service/backups/

# 2. 恢复到 ainews_content 库（--clean 先清空已有对象，--if-exists 避免不存在报错中断）
docker exec ainews-service-postgres-backup pg_restore -h db -U ainews -d ainews_content \
  --clean --if-exists /backups/<选中的文件名>.dump

# 3. 恢复后重启 temporal-worker/web，确保应用侧连接池不持有旧连接状态
docker compose restart temporal-worker web
```

## 4. LiteLLM 网关排查

网关是独立自建系统（不在本项目的 docker-compose 里），地址与鉴权信息见 `.claude/memory/reference.md`。

- 排查"模型调用报错/超时"：先确认 `LITELLM_BASE_URL` 带 `/openai/v1` 后缀（裸 `/v1` 是另一套注册表，见 `.claude/memory/decisions.md`）
- 查某个模型是否支持强制 tool_choice / 当前配置：`curl {LITELLM_BASE_URL}/model/info`
- 网关自身的请求日志/花费统计目前是唯一的模型调用可观测性来源（M7 决定暂不接入 Langfuse，见 `.claude/memory/decisions.md`）——网关侧的日志查看方式需要登录网关自身的管理面板，不在本项目的排查范围内

## 5. 常见故障 checklist

| 现象 | 排查方向 |
|---|---|
| 某个信息源连续多次 fetch 失败 | 查 `worker.fetch.record_source_health_activity` 写入的健康状态；先看是否是源本身挂了（RSS/API 端点不可达）还是反爬拦截（`fetch_original_activity` 的三级兜底，见 04 §2.4）；单源失败不阻塞其余源，不需要立即处理，持续失败再考虑在 `sources.yaml` 里标记降级 |
| 某篇文章 enrich 持续失败 | 查该 `EnrichArticleWorkflow` 的 History，常见原因是原文抓取三级兜底全部失败（含 Playwright 渲染超时）；单篇失败不阻塞其余文章 |
| `aggregate_activity`/`write_activity` 报超时 | 检查 `workflows.py` 里的 `start_to_close_timeout` 是否因为批次规模显著增长（当前 180s/90s，M5 时基于 140+ 篇校准）而不够用，见 `.claude/memory/decisions.md` |
| 前端页面 500/查不到数据 | 确认 `web` 容器能连上 `db`（`docker compose logs web`）；确认对应 `documents` 记录确实存在（`doc_type`/`id` 是否匹配路由预期） |
| 图片显示不出来 | 确认 `images` 卷两边（`temporal-worker`、`web`）都挂载正确；`web` 容器是只读挂载，只应该读不应该写 |
| Schedule 显示 paused 或 next run 时间不对 | 见 §2 手动查询命令；如果 schedule 完全不存在，检查 `temporal-worker` 启动日志是否在 `ensure_pipeline_schedule` 处报错（比如误删了 `id=` 参数会导致 `ValueError: ID required`，worker 会持续崩溃重启） |
| Postgres 备份连续失败 | `docker logs ainews-service-postgres-backup` 看具体报错；常见原因是 `PGPASSWORD` 未正确从 `.env` 传入，或 `db` 服务当时不健康（`depends_on: condition: service_healthy` 一般能规避这个问题） |

## 6. 退役旧 AInews 判断依据

M7 验收标准要求"新系统能独立连续运行至少 7 天不需要人工干预"——观察期内如果本文档 §5 的常见故障需要频繁人工介入（而不是设计上"预期内、不影响整体产出"的单点失败），说明还不到退役旧 AInews Desktop Scheduled Task 的时候。观察重点：每日 09:00 的 Schedule 是否按时触发、`written` 记录数是否稳定在合理区间、`postgres-backup` 是否每天都成功。

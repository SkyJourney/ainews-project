# M0 — 项目骨架跑通

> 前置依赖：无（起点）
> 状态：已完成
> 关联文档：[00-overview.md](../00-overview.md) §4 架构图 · [04-roadmap.md](../04-roadmap.md) §4 M0 · [03-architecture-proposal.md](../03-architecture-proposal.md) §3 数据模型 / §5 部署拓扑

## 目标

把 Postgres / Temporal / Celery Beat / LiteLLM 四个组件在本地跑通并互相连通，验证"编排骨架"本身可用——不涉及任何真实业务逻辑。

## Scope（范围内）

- 仓库结构初始化
- Postgres schema 落地：`articles` / `documents` / `links` / `tags`（见 03 §3 SQL）
- Temporal Server + Worker 能跑一个 hello-world workflow
- Celery Beat 能触发一次 `start_workflow`
- LiteLLM 网关连通性测试（跑通一次强制 `tool_choice` 的结构化调用）

## Out of scope（明确不做）

- 任何实际抓取 / 过滤 / 聚类业务逻辑
- 真实信息源接入
- 前端

## 任务清单

- [x] 初始化仓库目录结构
  **落地方式**：`backend/`（Python，worker + beat + migrations）/ `infra/`（仓库内通用 docker-compose）/ `frontend/`（仅 `.gitkeep` 占位，M6 再填充）。backend 下按职责分包：`worker/`（Temporal workflow+activity+启动入口）、`beat/`（Celery app+task）、`migrations/`（Alembic）。
- [x] 部署本地 Postgres，`ainews_content` 库落地
  **落地方式**：单个 `db` 容器（`postgres:16-alpine`），`POSTGRES_DB=ainews_content` 启动即建库；**没有**手动建一个独立的 `temporal` 库——Temporal 自己的 `temporal`/`temporal_visibility` 两个库由 `temporalio/auto-setup` 镜像首次启动时自动创建，同一个 Postgres 实例、天然隔离，不需要额外初始化脚本。宿主机映射端口改成 `15432`（`5432` 已被本机另一套编排占用）。
- [x] 建 `articles`/`documents`/`links`/`tags` 四张表
  **落地方式**：改用 **Alembic** 做版本管理（而非手写 init SQL），`backend/migrations/versions/0001_initial_schema.py` 用 `op.execute()` 原样落 03 §3 的 DDL（保留 GENERATED ALWAYS AS STORED / GIN 索引等 Postgres 特性）。`articles.embedding`（pgvector）按文档原文注明暂不建。执行方式：`docker compose run --rm temporal-worker alembic upgrade head`。已验证：`\dt` 可见四张表 + `alembic_version`。
- [x] 部署 Temporal Server + 最小 Python Worker，注册 hello-world workflow
  **落地方式**：`temporalio/auto-setup:1.24` + `temporalio/ui:2.31.2`（UI 端口 `8088`，gRPC 端口 `7233`，均为避开本机占用端口后重新选取）。**踩坑**：compose 里最初给 auto-setup 配了 `DYNAMIC_CONFIG_FILE_PATH: config/dynamicconfig/development-sql.yaml`，该路径在这个镜像版本里不存在，导致容器崩溃重启循环；移除该环境变量后恢复正常——不是所有 Temporal 部署教程里的环境变量都适用于所有镜像版本，遇到崩溃循环先看日志里的具体报错，不要照抄。Worker 代码：`backend/worker/{activities,workflows,worker}.py`，`HelloWorldWorkflow` 调 `say_hello` activity。
- [x] 部署 Redis + Celery Beat，能触发一次 `start_workflow`
  **落地方式**：`backend/beat/celery_app.py` 定义 `beat_schedule`，`backend/beat/tasks.py` 里的 task 用 Temporal Python Client 直接 `start_workflow`。M0 占位排期是每 5 分钟触发一次（仅验证链路，M1/M3 设计真实抓取排期后需替换）。`celery-worker`/`celery-beat` 两个服务共用 `temporal-worker` 的同一个镜像，只是 `command` 不同。
- [x] 部署 / 接入自建 LiteLLM 网关，跑通一次强制 `tool_choice` 的结构化调用
  **落地方式**：真实 `LITELLM_BASE_URL`/`LITELLM_API_KEY` 已填入 `/Volumes/Docker/compose/ainews-service/.env`。**`base_url` 必须是 `https://.../openai/v1`**——这个网关在 `/openai/v1` 和裸 `/v1` 下挂的是**两套完全独立的模型注册表**（用 `/model/info` 与 `/openai/v1/model/info` 分别查证实：同一个模型名在两边指向不同的 `litellm_provider`/`api_base`，裸 `/v1` 那套走各厂商的 Anthropic 兼容端点，`/openai/v1` 那套才是走各厂商真正的原生 OpenAI 兼容端点，如 `deepseek-v4-flash` → `https://api.deepseek.com/v1`）；排查连通性问题时，先确认查的是哪套注册表，不要拿裸 `/model/info` 的结果去解释 `/openai/v1` 调用的行为，这是两码事——本次踩过这个坑，多绕了一圈。新增 `backend/config/models.yaml` 记录网关当前支持的 17 个模型（配置数据，非代码，和 04 §2.1 信息源注册表同一处理方式）。验证方式：`docker compose run --rm temporal-worker python -c "..."`（走 `/openai/v1`），`openai` SDK 强制 `tool_choice={"type":"function","function":{"name":"report_status"}}`，`glm-5-turbo`/`kimi-k2.5` 均返回完全正确的结构化 JSON，验证通过。
  **重要发现与最终策略（详情见 `backend/config/models.yaml` 的 `tool_choice_forced` 字段，权威规则见 04-roadmap.md §2.8）**：不同模型对"强制 tool_choice"的支持程度不一致——`deepseek-v4-flash`/`deepseek-v4-pro` 在这台网关的任何调用路径（OpenAI 协议原生端点 / Anthropic 协议裸端点，传或不传各种 thinking 相关参数）下都不支持强制 tool_choice，报 `"Thinking mode does not support this tool_choice"`；Qwen 系列（`qwen3.6-flash`/`qwen3.6-plus`/`qwen3.7-plus`/`qwen3.7-max`）默认同样拒绝，但加 `extra_body={"enable_thinking": False}` 后可以支持；`glm-5-turbo`/`kimi-k2.5` 不需要任何额外参数直接支持。**排查过程踩过两个坑**：① `base_url` 少了 `/openai/v1` 前缀会落到网关另一套走 Anthropic 兼容端点的模型注册表，报错和真实原因无关；② 一度怀疑是 Anthropic 协议本身的"thinking 模式禁止强制 tool_choice"限制传导过来的，但用 Anthropic 裸端点+不传 thinking 参数实测后证明和协议选型无关——DeepSeek v4 就是恒定 thinking 开启，两种协议表现一致。
  **最终决定：不再为追求强制模式的确定性做按模型分支的调用逻辑，统一用 `tool_choice="auto"`**（每个场景只提供一个 tool + prompt 明确要求调用，正确性交给 Instructor 校验 + Temporal 重试兜底）。实测 `auto` 模式下 DeepSeek v4 的 reasoning 开销很小（`reasoning_tokens` 约 27-31，`reasoning_content` 只有 60-70 字符），响应稳定、参数正确，不构成性能顾虑，也不是"将就的降级方案"。`models.yaml` 里的 `tool_choice_forced` 字段作为参考保留，不是本项目实际采用的调用策略。
- [x] 编写启动说明文档
  **落地方式**：见本文件顶部关联文档 + `docs/05-process.md`；两份 docker-compose.yml 各自在文件头部注释里写明用途和相互关系，不再单独开一份 README。

## 与设计阶段不同的落地调整

- **两份独立维护的 docker-compose.yml，不做符号链接**：`infra/docker-compose.yml`（仓库内，具名 volume `pgdata`/`redisdata`，可移植）+ `/Volumes/Docker/compose/ainews-service/docker-compose.yml`（本机部署，硬编码 `/Volumes/Docker/data/ainews-service/{postgres,redis}` bind mount）。两份定义目前一致，后续如有差异需手动同步。
- **项目在 `/Volumes/Docker/{compose,data}` 下的命名是 `ainews-service`**，不是 `ainews`——旧系统的静态站点已经占用了 `ainews` 这个名字（`ainews-web` 容器，端口 8801，仍在跑，不受本次改动影响）。
- **端口映射**：postgres `15432:5432`、redis `16379:6379`、temporal gRPC `7233:7233`、temporal-ui `8088:8080`——均因宿主机默认端口已被本机其他编排占用而重新选取，postgres/redis 按要求对外暴露宿主机端口，方便后续排查/联调直连。
- **backend 镜像带版本 tag**：`docker-compose.yml` 里 `temporal-worker` 同时声明 `build` 和 `image: ainews-service/backend:${BACKEND_IMAGE_TAG:-0.1.0}`，`celery-worker`/`celery-beat` 复用同一 tag，不各自重复 build。本地 `docker images` 可见这个带版本号的镜像，升级时改 `BACKEND_IMAGE_TAG` 即可。
- **Python 依赖管理用 conda + requirements.txt**（不是 pyproject.toml）：`backend/.conda_env` 沿用 AInews 项目同款格式（env_name/python_version/python_bin/create_cmd/verify_cmd），环境名 `ainews-service`，Python 3.13.12；`backend/requirements.txt` 一份文件同时供本机 conda 环境和 Docker 镜像构建使用。
- **数据库版本管理引入 Alembic**，替代最初设计里的手写 init SQL 脚本。
- **`requirements.txt` 补了 LLM 客户端三件套并从版本范围改成精确 pin**：`openai`（LiteLLM 网关是 OpenAI 兼容协议，直接用这个 SDK 换 `base_url` 指向自建网关，不引入额外框架）+ `instructor`（04 §2.8 要求的结构化输出校验，包一层 `openai` 客户端）+ `pydantic`（tool schema 的载体，`instructor` 强依赖 v2）。版本探查方式：在实际创建的 `ainews-service` conda 环境里不加版本号直接 `pip install`，让 pip 解析器算出互相兼容的最新组合，`pip check` 验证无冲突后精确锁定（`temporalio==1.30.0` / `celery==5.6.3` / `redis==8.0.1` / `sqlalchemy==2.0.51` / `alembic==1.18.5` / `psycopg[binary]==3.3.4` / `python-dotenv==1.2.2` / `httpx==0.28.1` / `openai==2.44.0` / `instructor==1.15.4` / `pydantic==2.13.4`），backend 镜像同步重建验证六容器仍正常运行。

## 验收标准

- [x] 手动触发一次空 workflow，Temporal Web UI 能看到完整执行历史
  验证方式：`docker compose run --rm temporal-worker python -c "..."` 手动 `execute_workflow`，workflow id `m0-verify-1`；Temporal UI API 确认 `status: WORKFLOW_EXECUTION_STATUS_COMPLETED`，`historyLength: 11`。
- [x] Postgres 里能查到一条测试写入记录
  验证方式：`INSERT INTO documents (...) VALUES ('zettel/m0-verify', ...)`，`SELECT` 确认可查到。

## 备注 / 风险

- Postgres 与 Temporal 共用同一实例但独立 database 是既定结论（见 00-overview.md §3），不要在本里程碑重新讨论选型。
- 不引入任何厂商专属 Agent 框架，LiteLLM 网关连通性测试也应遵循这一点（直接走 OpenAI 兼容协议）。
- 六个容器（db/redis/temporal/temporal-ui/temporal-worker/celery-worker/celery-beat）目前处于**持续运行状态**，供 M1 开发直接复用，不是跑完验收就关掉。`celery-beat` 的 5 分钟占位排期会持续触发 hello-world workflow，Temporal UI 里会看到周期性执行记录——这是预期行为，M1/M3 定义真实排期后替换掉即可，不代表系统有问题。

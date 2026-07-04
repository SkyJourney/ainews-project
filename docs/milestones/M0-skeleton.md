# M0 — 项目骨架跑通

> 前置依赖：无（起点）
> 状态：未开始
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

- [ ] 初始化仓库目录结构（workflow worker / activities / astro frontend 等服务划分，具体拆分方式在本里程碑内确定）
- [ ] 部署本地 Postgres（Docker Compose，见 03 §5），建两个独立 database：`temporal`（Temporal 状态存储）与 `ainews_content`（唯一权威内容存储）
- [ ] 在 `ainews_content` 库中按 03 §3 SQL 建 `articles` / `documents` / `links` / `tags` 四张表及相应索引（`articles_batch_status_idx` / `documents_frontmatter_gin` / `documents_body_tsv_gin` / `documents_doc_type_idx` / `tags_tag_idx`）
- [ ] 部署 Temporal Server（Docker Compose）+ 编写最小 Python Worker，注册一个 hello-world workflow
- [ ] 部署 Redis + Celery Beat，配置一个最小 cron schedule，行为仅为调用 `client.start_workflow(...)`（薄触发器，见 04 §2.8）
- [ ] 部署 / 接入自建 LiteLLM 网关，跑通一次强制 `tool_choice` 指向自定义 tool schema 的结构化输出调用（只验证网关本身可用，不涉及业务 prompt）
- [ ] 编写启动说明文档，记录本地起服务的完整步骤

## 验收标准

- [ ] 手动触发一次空 workflow，Temporal Web UI 能看到完整执行历史
- [ ] Postgres 里能查到一条测试写入记录（不要求走完整业务链路，只验证"服务能写库"这条通路）

## 备注 / 风险

- Postgres 与 Temporal 共用同一实例但独立 database 是既定结论（见 00-overview.md §3），不要在本里程碑重新讨论选型。
- 不引入任何厂商专属 Agent 框架，LiteLLM 网关连通性测试也应遵循这一点（直接走 OpenAI 兼容协议）。

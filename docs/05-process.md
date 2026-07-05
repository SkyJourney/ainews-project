# 文档体系与推进流程

> 本文档说明 `docs/` 下各文件的定位与相互关系，以及新会话该如何使用这套文档推进开发。同时承担"进展日志"的角色——见 §4，每次有实质性进展都要在这里追加一行记录。

## 1. 文档体系全景

| 文档 | 定位 | 什么时候读 |
|---|---|---|
| [`00-overview.md`](./00-overview.md) | 起点文档：问题陈述、目标、结论摘要表、架构总图 | 任何新会话的第一份必读 |
| [`01-document-database-research.md`](./01-document-database-research.md) | 专项调研：文档数据库选型完整推理过程 | 需要理解"为什么是 Postgres 不是 MongoDB/SQLite"时才展开读 |
| [`02-pipeline-orchestration-research.md`](./02-pipeline-orchestration-research.md) | 专项调研：编排框架选型完整推理过程 | 需要理解"为什么是 Temporal 不是纯 Celery"时才展开读 |
| [`03-architecture-proposal.md`](./03-architecture-proposal.md) | 整体架构方案：数据模型 SQL、Phase 映射表、部署拓扑、迁移路径、待决问题 | 需要具体 schema / 部署细节 / 待决问题时查阅 |
| [`04-roadmap.md`](./04-roadmap.md) | **新会话的唯一权威入口**：不耦合旧 AInews 目录的功能清单（业务规则完整提炼，§2）+ 里程碑设计（§4） | 任何新会话的第二份必读；开始某个里程碑前必读对应 §2 小节 |
| `05-process.md`（本文档） | 文档体系导航 + 推进流程 + 进展日志 | 想知道"现在推进到哪了 / 这套文档该怎么用"时查阅 |
| [`milestones/`](./milestones/) | 每个里程碑的执行细化版：从 04 §4 展开成可勾选任务清单 + 验收标准，**随开发推进持续补充真实实现细节** | 具体动手做某个里程碑时，逐项对照执行 |

关系一句话总结：00 是门面，01/02/03 是调研证据，04 是权威规格，05（本文档）是使用说明书 + 时间线，`milestones/` 是把 04 的规格拆解成可执行、可勾选、会随实现不断补充细节的活文档。

## 2. 新会话该怎么用这套文档

1. 先读 `00-overview.md`，建立整体认知（问题是什么、目标是什么、架构长什么样）。
2. 再读 `04-roadmap.md` §1-§3（核心原则 + 推进顺序），了解当前处于服务化重构的哪个阶段。
3. 查 `milestones/README.md` 的状态表，确认当前实际推进到哪个里程碑（不要假设，以表格状态为准；状态表和各里程碑文件顶部的"状态"字段应保持一致）。
4. 打开对应里程碑文件，同时对照 `04-roadmap.md` §2 中该里程碑覆盖的功能清单小节，逐项推进任务清单。
5. 遇到 checklist 没覆盖到的边界情况，回 `04-roadmap.md` §2 找权威表述；`01`/`02`/`03` 用于查具体选型理由或 schema 细节。

## 3. 文档维护规则（所有会话都要遵守）

### 3.1 里程碑文档需要跟随实现同步更新

`docs/milestones/*.md` 当前只是从 04 提炼的**框架性**任务清单（因为项目尚未开始写代码），不包含任何真实实现细节。**从有实质性代码/配置产出的那一刻起**：

- 完成一项任务清单条目时，把 `- [ ]` 改成 `- [x]`，并在该条目下追加一小段"落地方式"说明——具体用了什么库/表结构/关键决策/踩过的坑，而不是只打勾了事。目标是下一个会话看一眼里程碑文件就知道"这件事具体是怎么做的"，不需要再翻代码或翻聊天记录。
- 里程碑文件顶部的"状态"字段（未开始/进行中/已完成）随实际进度更新，`milestones/README.md` 的状态表格必须同步更新，保持两处一致，不能一处更新一处遗漏。
- 验收标准全部勾选通过、且经过实际验证后，才允许把状态改为"已完成"并进入下一个里程碑——04 §3 的既定原则："前一个不达标不进入下一个"，milestones 文档的状态字段就是这条原则的具体落点。

### 3.2 本文档需要维护"进展日志"（§4）

每次有实质性进展（不要求是整个里程碑完成，某个任务清单条目完成、某个关键实现决策定下来都算），在 §4 追加一行记录。日志只记"发生了什么、为什么"这种时间线信息，具体怎么实现的细节写进对应里程碑文件，不要在这里重复展开。

## 4. 进展日志

| 日期 | 里程碑 | 事件 |
|---|---|---|
| 2026-07-04 | - | 完成 00-04 架构调研与 roadmap；创建 `docs/milestones/` 全部 10 份里程碑框架文档 + 索引；初始化项目级 `CLAUDE.md`；新建本文档 |
| 2026-07-04 | M0 | backend 骨架落地（conda_env+requirements.txt / Dockerfile / Alembic 迁移 / Temporal worker / Celery beat），两份 docker-compose.yml（仓库通用版 `infra/` + 本机部署版 `/Volumes/Docker/compose/ainews-service/`，项目在本机命名为 `ainews-service` 以区分旧系统的 `ainews`）。六容器（db/redis/temporal/temporal-ui/temporal-worker/celery-worker/celery-beat）已实际起服务并持续运行，Alembic 建表验证通过，手动触发 hello-world workflow 验证 Temporal Web UI 执行历史 + Postgres 写入均正常。唯一未完成项：LiteLLM 网关连通性测试，待真实 endpoint/key 填入 `/Volumes/Docker/compose/ainews-service/.env` 后补跑，不阻塞后续里程碑。 |
| 2026-07-04 | M0 | 补充 LLM 客户端依赖（`openai`+`instructor`+`pydantic`，此前 requirements.txt 遗漏，M0 的 LiteLLM 连通性测试其实卡在缺客户端库这一步）；实际创建本机 `ainews-service` conda 环境，用 pip 解析器探查全部依赖的兼容最新版本并精确 pin（不再用范围），`pip check` 验证无冲突；backend 镜像重建、六容器重启验证仍正常。 |
| 2026-07-04 | M0 | **M0 收尾，状态改为已完成**。真实 LiteLLM 网关凭据填入 `/Volumes/Docker/compose/ainews-service/.env`（`base_url` 需带 `/openai/v1` 协议前缀，裸 `/v1` 会落到网关另一套走 Anthropic 兼容端点的模型注册表，排查时曾误判为网关路由问题，实际是 base_url 少了协议前缀）；新增 `backend/config/models.yaml` 记录网关支持的 17 个模型（配置数据）。 |
| 2026-07-04 | M0 | **架构决策：`tool_choice` 统一用 `auto`，放弃强制模式**（更新 04-roadmap.md §2.8）。背景：强制 tool_choice 在这台网关上因模型而异——GLM/Kimi 部分型号直接支持，Qwen 商业系列加 `extra_body={"enable_thinking": False}` 后支持，DeepSeek v4（pro/flash）两种协议路径 + 传或不传各种 thinking 参数均不支持（穷尽测试后确认是模型恒定 thinking 开启，与协议选型无关，参考了 `/Volumes/Projects/Autotest_Platform` 里 `llm_client.py` 的既有实践，其生产配置同样只验证过 Qwen 未验证过 DeepSeek）。最终决定不做按模型分支的调用逻辑，统一 `auto` + Instructor 校验 + Temporal 重试兜底；实测 `auto` 模式下 DeepSeek v4 的 reasoning 开销很小（约 30 tokens），不是性能妥协。`models.yaml` 的 `tool_choice_forced` 字段保留作参考，不再是调用策略依据。 |
| 2026-07-04 | M1 | **M1 完成，状态改为已完成**：`openai-rss` 单源端到端全链路（preflight→fetch→filter→enrich×M child workflow fan-out→aggregate→write）真实跑通，经真实 Temporal Server + Worker 执行（非直接函数调用），16 篇文章 0 失败全部写入 `documents` 表。过程中三处修正：① Instructor 的 `Mode.TOOLS` 会忽略显式 `tool_choice="auto"`，改用 `Mode.JSON`（决策记录见 decisions.md）；② openai.com 文章页挂 Cloudflare 反爬挑战，参考旧项目 `fetch-with-assets.py`/`news-originalizer.md` 提前把 04 §2.4 状态②（Jina Reader 兜底）实现到 M1，`articles` 表新增 `fetch_channel`/`published_at` 两列（migrations 0002/0003）；③ `deepseek-v4-flash` 偶发把答案整个写进 `reasoning_content`、`content` 留空（DeepSeek 官方文档承认的已知问题），`max_retries` 从 0 改为 1 后稳定修复。**路线图调整**：M2-M4 从"逐个里程碑独立验收"改为"合并成一个连续阶段推进"（04-roadmap.md §3 已更新），理由是 M1 验证的是"新架构管道形状对不对"这类高不确定性工作，M2-M4 是从老系统迁移已验证规则，不确定性低。 |
| 2026-07-04 | M2+M3+M4 | **M2+M3+M4 合并阶段完成，三个里程碑状态均改为已完成**：分 Stage A-E 推进。Stage A（M2 Filter）补齐同批次去重③④、跨源论文去重、信噪比过滤表，新增 Postgres 表 `url_index`（migration 0004）落地跨日去重（替代旧系统 JSON 文件方案）。Stage B（M3 全源接入）补全 14 条源配置，`fetch_activity` 新增 api/webfetch/script 三分支，新增 `source_health` 表（migration 0005）落地健康状态机，`AInewsPipelineWorkflow` 从单源单次调用改造成按活跃源 fan-out。Stage C 用真实 14 源数据复验 M2 规则，修正跨源论文去重的同源误判。Stage D（M4 Enrich）补齐 Fallback A（Playwright 无头浏览器，backend 镜像新增 Chromium）+ Fallback B 占位、配图分级渲染（新增 Docker 卷 + `ainews-media://` 自定义 scheme 引用）、翻译完整性机械校验、富元数据抽取（migration 0006 补 `word_count`/`translation_fallback_notice`），并修复安全审查发现的 SSRF 风险（`_assert_public_url`/`_safe_get`）。Stage E 验收：全 14 源真实批次 168 篇文章，**enrich_failed=0，覆盖率 100%**，达成 M4 核心验收标准。过程中还修正了 trafilatura `include_images` 默认关闭、arxiv.org 引用工具区块与站点跟踪像素拖累翻译校验（降级率从 29% 修到 9.5%）等实测踩坑，详见 decisions.md。 |
| 2026-07-05 | M4（追加） | **翻译降级问题深度排查完成**：M4 验收后用户额外要求专项 investigation，目标"复杂文章翻译也做到无降级"。给 `translate_activity` 加逐块诊断日志跑真实批次（146 篇 17 篇失败），归成 5 类根因，针对性修复 3 类——分块级数据/噪声行识别（跳过噪声翻译+从 CJK 占比分母排除数据行）、单块重试上限从 1 次加到 2 次、openai.com 头部导航与相关文章推荐区块清理、通用重复段落去重（一次覆盖 a16z 侧栏与 arxiv 许可证图标块）、本地图片自定义 scheme 补漏不计入分母；新增独立复审机制（机械校验不通过时另开一次 LLM 调用核对是否真的不完整，减少专有名词/数据密度导致的误杀，机械校验仍是唯一主判据）。另外 2 类根因（品牌名密度临界失败、state-of-ai 历史 Edition 落地页）确认不需要修——前者旧项目也没有豁免逻辑，后者跨日去重已能自动处理。**最终全新全量批次验收：144 篇，0 enrich_failed，0 篇最终降级**（原始诊断批次 11.6% → 0%）。详见 `docs/milestones/M4-enrich.md` 与 decisions.md。 |
| 2026-07-05 | M5 | **M5（Aggregate 阶段完整化）完成**：补齐 04 §2.6 "Original"归档层这个此前被 zettel 顶替、一直没做的缺口——每篇文章都建 `original`（真正的完整译文归档），只有聚类判断 `zettel_worthy` 的文章才额外建/复用 `zettel`（原子笔记，三级复用判断）。实现 Topic 追加铁律（首建/追加分支，`documents` 整行 UPSERT 语义下靠读旧文档+计算合并结果落地"绝不整体重写"）、Daily 五种情形分类、Digest 五项自检、Tags 四轴打标，`tags`/`links` 两张建表以来从未写入的表首次真正启用。真实批次验证：136+ 篇一次性聚类/打标调用被 max_tokens 截断，改成分块调用；zettel_worthy 首次实测 48% 远超"3-10 张"软性指标，收紧 prompt 后降到符合区间；`aggregate_activity`/`write_activity` 的 Temporal 超时沿用 M1 时代 30 秒遗留值不够用，调至 180s/90s；发现并修复了一个真实的悬空引用风险（`url_index.zettel_id` 指向的文档实际不存在时会导致 `links` 外键违反，整批写入失败）。工程化收敛补项：新增 pytest 测试基建（`backend/tests/`，48 个用例，覆盖 M5 新逻辑 + 回溯覆盖 M2/M4 已有纯函数），全部 mock 数据库/LLM，不连真实环境即可快速验证分支逻辑。最终真实全量批次：146 篇保留、145 篇 enrich 成功、165 条文档写入（145 original+8 zettel+10 topic+1 daily+1 digest）。详见 `docs/milestones/M5-aggregate.md` 与 decisions.md。 |
| 2026-07-05 | M6 | **M6（前端上线）完成**：前置调研发现旧前端（`/Volumes/Projects/AInews/web/frontend`）实际已是 Astro 7.0.5（不是计划里写的"升级到6"），核实 SSR 所需能力均已具备后改为"维持 7.0.5，只做 SSR 配置改造"。整体复制旧前端到新仓库 `frontend/`，只换两处：① 自建 `postgres-loader.ts` 实现 `astro/loaders` 的 Live Loader 接口，请求时查 `documents` 表（关键细节：Live entry 不走 build-time 渲染管线，需手动调用 `createMarkdownProcessor` 生成 `rendered.html`）；② `wiki-link.ts` 存在性判断改批量查 Postgres。五类内容页面+新增 Digest/Tags 两个此前完全不存在的功能全部接入真实数据，搜索从 Pagefind 换成 Postgres 全文搜索（`body_tsv` GIN 索引第一次真正用上），图片改自定义 scheme 改写+独立静态服务端点。新增 `web` Docker 服务（两份 compose 同步），未引入 nginx（明确留给 M7）。**核心卖点验证**：对着真实持久化部署直接 `INSERT`/`UPDATE` 数据库记录，同一个未重建未重启的容器立即反映变化。详见 `docs/milestones/M6-frontend.md` 与 decisions.md。 |
| 2026-07-05 | M7 | **M7（生产化收尾）开发工作完成，进入 7 天连续运行观察期**：讨论中用户主动提出"Temporal 自带定时器能否取代 Celery Beat"，核实 Redis 全代码库唯一用途就是 Celery broker、`temporalio==1.30.0` 原生支持 Schedule Client API 后，**架构调整为 Temporal 原生 Schedule**（`worker.py` 新增 `ensure_pipeline_schedule` 幂等注册），`celery-worker`/`celery-beat`/`redis` 三个容器连同 `backend/beat/` 模块整体退役；`batch_id` 生成从"Celery task 在 workflow 外部生成"改为"workflow 内部用 `workflow.info().start_time` 兜底生成"（同样满足确定性约束）。新增 `postgres-backup` 服务（`infra/scripts/pg_backup.sh`，每日 pg_dump 快照 + 按保留天数清理，决策为不上 WAL/PITR），新增运维手册 `docs/06-runbook.md`。Langfuse/git_export 均决策暂缓（不是遗漏）。**真实批次验证**：`handle.trigger()` 手动触发一次完整批次，`batch_id` 正确从 `workflow.info().start_time` 生成、14 源全部成功、0 enrich 失败、100 条文档写入；备份容器首次启动即成功生成可用 dump 文件，保留策略清理逻辑用构造的过期文件验证通过。验收标准"连续运行 7 天不需要人工干预"是观察期而非开发任务，观察起始 2026-07-05，详见 `docs/milestones/M7-production-hardening.md` 与 decisions.md。 |
| 2026-07-05 | M10（新增） | **新增 Deep Dive 里程碑占位（延后）**：用户提出"Deep Dive 后续做成独立 Temporal 工作流是否合适"，核实旧系统 `40-Deep-Dives/` 后发现这个功能从未真正实现过（空 `.gitkeep`、无 agent、无 phase 集成，不是"已验证规则待迁移"）；旧系统自己"≥7 天 Digest 历史"的设计门槛其实已经达到（8 天）但从未被开发补上。新建 `docs/milestones/M10-deep-dive.md`，状态"延后"，触发条件是新系统 `documents` 表 `doc_type='digest'` 积累足够天数历史（核实时点只有 1 天）。同步更新 `04-roadmap.md` §4 与 `milestones/README.md`。详见 decisions.md。 |
| 2026-07-05 | M8（提前设计） | **M8 迁移设计定稿，提前于原定"M7 验收后"启动**（仅分析设计，未执行任何导入）：调研旧 vault（341 个内容文档，`50-Zettel`/`20-Topics`/`10-Daily`/`30-Digests`/`60-Originals`）发现新旧系统时间窗口存在真实重叠（新系统 `original`/`zettel` 的 `doc_date` 是文章发布日而非抓取日）。迁移策略：Original/Zettel/Topic/Digest 冲突时新系统数据为准，只补新系统没有的内容；**Daily 例外**，用户明确要求保留旧系统按天拆分的粒度。Zettel/Topic 的 ID 格式与新系统一致无需转换，Digest 需重排前缀，**Original 的 ID 必须按 `source_url` 重新计算**（旧文件名格式与新系统 `original-<hash>` 体系不同）。已知脏数据（19 处悬空 wikilink、arXiv 脚注误转义成伪链接、YAML 引号风格不统一）记录进迁移设计供后续导入脚本处理。完整设计见 `docs/milestones/M8-legacy-migration.md`，实际导入执行仍留到 M7 观察期结束后。详见 decisions.md。 |

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

### 观察期真实故障：`aggregate_activity` 反复超时（gRPC 4MB 消息上限），已定位并修复（2026-07-06）

观察期第二天，`ainews-pipeline-daily` 当天批次连续 3 次执行到 `aggregate_activity` 就超时失败（180s→360s→900s 逐次调大仍不够）。按用户要求走了一遍系统性排查（不满足于"调大超时"这种表面缓解）：先用一个不含真实业务逻辑的诊断 workflow（`worker/diag.py`，排查完已删除）逐一排除工作流历史膨胀、线程池饱和、大payload传递、共享 HTTP 连接池退化几个假设；最后用 `py-spy` 对卡住的线程做实时栈追踪，确认线程确实阻塞在等待真实 LLM 网络响应（不是 bug），一度怀疑是模型（`deepseek-v4-flash`）慢，切到 `qwen3.6-flash` 后单次测试"看似"解决——但坚持看完整、不加过滤的容器日志后，找到了真正的根因：`aggregate_activity` 实际每次都执行成功，但其返回值（101 条记录，含 arxiv 全文正文）序列化后达 4,327,723 字节，超过 Temporal gRPC 硬性消息上限 4,194,304 字节（4MB），触发 `ResourceExhausted`（`"grpc: received message after decompression larger than max"`），导致"activity 完成"信号发不出去——从 workflow 角度看就是一直挂起直到 `StartToClose` 超时。这个风险其实在 M5 验收时就已经被 `PayloadSizeWarning`（1.16MB，见 [M5-aggregate.md](./M5-aggregate.md) "已知的后续关注项"）预警过，当时判断"尚未触及硬上限，暂不处理"，随着 arxiv 全文抓取修复（单篇正文从 KB 级涨到 2-7 万字符）真实触发。

**修复**：`aggregate_activity` 与 `write_activity` 合并为一个 Temporal activity——records（含全文正文）只在 `aggregate_activity` 内部产生和消费，`write_activity` 改为普通函数调用（不再单独注册为 Temporal activity），只把不含正文的小 summary（`{"written", "new_topics", "new_zettels"}`）返回给 workflow，彻底避免大 payload 跨 Temporal 序列化边界。

**真实验证**：重建 `temporal-worker` 镜像并重启后，手动触发一次完整批次（`batch_id='2026-07-06-manual-verify'`，同时补跑了当天因故障缺失的数据）：`sources_attempted=14`/`sources_failed=0`/`fetched=1366`/`kept=95`/`enrich_failed=4`/`written=112`，workflow 从 `RUNNING` 正常流转到 `COMPLETED`；完整日志核查确认没有再出现 `ResourceExhausted`/gRPC 相关报错；`documents` 表按 `updated_at` 核实新增 91 original + 8 zettel + 11 topic + 1 daily + 1 digest = 112 条，与返回值完全对应。验证通过后恢复了排查期间暂停的 `ainews-pipeline-daily` Schedule。详见 `.claude/memory/decisions.md`「aggregate_activity/write_activity 合并修复 gRPC 4MB 消息上限」。

### 观察期真实故障：translate_activity 超大论文超时 + 正文重复标题 + jiqizhixin 抓取污染，三处修复 + 两处新发现暂缓（2026-07-07）

观察期第三天，巡检 09:00 自动批次（`kept=94`/`enrich_failed=5`/`written=103`）发现三类问题：① 5 篇 arxiv 全文论文（120/88/65/54 分块级别）`translate_activity` 三次重试均撞到 600s 超时（`ActivityError('Activity task timed out')`）；② 今天 89 篇 original 里至少 11 篇（约 12%）正文开头重复了一次标题；③ jiqizhixin 5 条只有 1 条入库，且内容是微信反爬验证页而非真实正文。

**修复**：① `enrich.py::translate_activity` 分块等待改用 `as_completed` + `activity.heartbeat()`，`workflows.py` 对应加 `heartbeat_timeout=90s`、`start_to_close_timeout` 600s→1800s，靠心跳区分"真卡死"与"确实很慢"；② `_clean_fetched_markdown`（direct/jina/playwright 三通道共用清洗管线）新增通用"去掉正文开头重复 H1"，替代此前只覆盖 arxiv 全文页的专门处理（`_ARXIV_LEADING_H1_RE` 已删除）；③ `_fetch_jiqizhixin` 提取的微信直链补 `html.unescape()`（历史 9 篇里 4 篇受此 bug 影响）。为了让重跑的文章能正确补进 Daily/Topic/Digest，顺手修了 `aggregate_activity` 对同一 `batch_id` 重跑时 Topic 文档会重复追加的问题（`_build_topic_record` 幂等化）。

**真实验证**：5 篇失败论文用心跳化后的新超时策略各自独立重跑 `EnrichArticleWorkflow`（新 `workflow_id`，沿用同一 `batch_id`），全部成功（`word_count` 4.3-7.3 万字符）；今天这篇 jiqizhixin 具体文章（`mid=2651042834`）用两条独立抓取路径（`_fetch_direct` + Jina Reader）重跑仍拿到反爬验证页，确认是微信对这篇内容本身的限制、不是我们的抓取实现问题，按范围只删除污染记录不强行补数据；对 `batch_id` 重新执行 `aggregate_activity` 后 `documents` 表核实 93 original + 12 topic + 1 daily + 1 digest = 107 条，逐一核对确认无同批次重复追加、无跨主题重复引用（修复过程中人工两次重跑 aggregate 触发了 4 篇文章的同批次聚类不一致，已按 `frontmatter.topic_slug` 手动核对清理）。

**两处新发现、本次未处理**：`normalize_url_for_index`（`filter.py`）对 query-string 型 URL（如微信公众号）归一化会发生碰撞（丢弃 query string 后同域名同路径的不同文章塌缩成同一个跨日去重 key），影响面可能不止 jiqizhixin；同一篇文章跨天可能被 LLM 重新聚类到不同 topic 桶（`aggregate_activity` 每次聚类判断都是独立的 LLM 调用，不保证跨天稳定）。两者均为架构层面的问题，需要单独设计，未在本次改动范围内处理。详见 `.claude/memory/decisions.md`「2026-07-07 观察期真实故障：translate_activity 超大论文超时 + 正文重复标题 + jiqizhixin 抓取污染，三处修复 + 两处新发现暂缓」。

### 观察期第四天：源治理 + 翻译降级三层兜底 + HuggingFace 转筛选层 + 新增独立 arxiv 全文回补 workflow（2026-07-08）

观察期第四天，工作分四段推进，均已真实验证：

**① 源治理**：`huggingface-daily-papers` 连续两天 400 报错，根因是 CST/EDT 时差导致查询"今天"命中还没有数据的日期，且既有日期回退循环有个死角（`raise_for_status()` 在第一次 400 就抛异常，回退到昨天那一步执行不到）——修复后真实验证拿到 40 条。`jiqizhixin`（历史 9 篇里 5 篇有数据质量问题，微信反爬限制确认非我方代码问题）与 `state-of-ai`（历史产出全是静态年度报告页、无持续新闻价值）两源停用（`reliability: dead`），新增 `marktechpost`/`venturebeat-ai`/`techcrunch-ai` 三个原生 RSS 替补源并真实验证跑通，活跃源数 14→13→16→15。

**② 翻译降级三层兜底**：`translate_activity` 新增换模型兜底重试（主模型失败→`qwen3.6-flash`+加大 `max_tokens`），真实批次触发 9 次；进一步加了第三层安全边界切分再合并兜底（不切进表格/代码块，递归对半拆分直到 400 字符下限）。用 6 篇历史降级文章复查：3 篇经二层换模型修复，另 3 篇根因是 arxiv 全文当时还没渲染完（只抓到摘要），确认现有抓取逻辑本身无 bug、纯粹是时间差，重跑后 2 篇完全修复。同时补翻译了数据库里已知的 29 篇历史降级文章，途中因直接复用已译中文标题触发 `needs_translation()` 误判导致 29 篇全部被跳过翻译，发现后改用源页面真实原始标题重新走一遍，28 篇修复、1 篇（源 URL 已 404）用公开网络找到的替代地址补齐。

**③ HuggingFace 转筛选层 + 新增独立 arxiv 全文回补 workflow**：核实 huggingface-daily-papers 从来没有全文、只是社区筛选信号（点赞数），且 arxiv 全文渲染延迟是 arxiv 自己后台异步批处理的固有特性（LaTeXML 渲染，实测 88% 在 3 天内完成）——据此把 HF 抓到的 entry 统一改标 `source_name=arxiv-api`（复用 arxiv 全文抓取/去重/分级逻辑），`articles` 表新增 `arxiv_fulltext_pending` 布尔列（migration 0007）全链路透传，新增独立的 `ArxivFulltextBackfillWorkflow` + 每日 09:00 Asia/Shanghai 独立 Schedule（`ainews-arxiv-fulltext-backfill-daily`），只查 14 天窗口内仍标记 pending 的候选、只更新 `documents.original` 正文，完全不碰 Topic/Daily/Digest。真实验证：模拟候选手动触发新 workflow 返回 `{"checked": 1, "ready": 1, "upgraded": 1}`，确认 Topic/Daily/Digest 的 `updated_at` 全程未变。当天晚些时候用真实数据核查发现一个部署时序缺口——新架构镜像中午才重建，晚于当天 09:00 的自动批次，导致该批次 10 篇 arxiv 论文漏打 pending 标记，已手动补标记修复，确认次日回补能正确捕捉到。

**④ 代码审查 + 修复**：用 10 视角多角度审查当天全部改动，发现并修复 8 个真实问题（2 个正确性回归——分块翻译 CJK 复检循环在换模型/安全切分成功后仍无保护调用失败过的模型、arxiv 摘要页清洗顺序回归导致样板文字重新泄漏进正文；1 处数据丢失——HuggingFace 点赞信号从未被下游持久化，订正了此前的错误结论；其余为 KeyError 防御、重复抓取效率、SQL 双重数据源、Temporal fan-out 异常处理不一致等），详见 `.claude/memory/decisions.md`「2026-07-08 代码审查：修复今天新增代码的 8 个真实问题」。

### 观察期真实故障：NUL 字节脏数据拖死 workflow task，Schedule 静默跳过 2 天（2026-07-20 发现，事故发生于 07-18）

用户巡检反馈"最近3天好像没跑"，排查发现 7 月 18 日的 `ainews-pipeline-daily-run-2026-07-18T01:00:00Z` 卡在 Running 状态整整 2 天没结束。根因链路：批次第 98 篇文章的子工作流 `2026-07-18-0100-enrich-98` 在 `upsert_article_activity` 写库时命中 `psycopg.DataError: PostgreSQL text fields cannot contain NUL (0x00) bytes`（正文含 NUL 字节，来源疑似某信源抓取/编码环节产出的脏字符）；`maximum_attempts=3` 按预期用尽（这一层本身符合设计——单篇文章失败不该拖累其余文章），但该失败随后没有被子工作流正常捕获收尾，而是让 **workflow task 本身**陷入无限重试崩溃循环（`temporal workflow show` 都因"string field contains invalid UTF-8"拒绝显示历史，推测是异常信息里带出的非法字节导致 gRPC 序列化本身失败，workflow task 永远提交不成功）。父工作流 `AInewsPipelineWorkflow` 要等全部 Enrich 子工作流结束才能进入 aggregate/write，因而被一并拖死；`ainews-pipeline-daily` Schedule 的 `OverlapPolicy=Skip`（上一次未结束就跳过新触发，不排队）让 7/19、7/20 两次定时触发被静默跳过（`ActionCounts.SkippedOverlap=2`，与观察到的"3 天没跑"吻合，实际中断仅 2 天）。

**修复**：① 用 `temporal workflow terminate` 直接终止卡死的父工作流（级联终止子工作流），放弃 7/18 这一批次（7/18 当天已成功写入的记录不受影响，缺口只有第 98 篇文章本身与 7/19 全天）；② `temporal schedule trigger` 手动补跑 7/20 当天流水线（不回补 7/19，用户明确接受这天开天窗）；③ 根因修复：`backend/worker/db.py::upsert_enriched_article` 新增 `_strip_nul_bytes`，写库前对全部自由文本字段（标题/摘要/正文/译文/gist/实体/关键词等）及其嵌套 JSON 结构递归清洗 `\x00`，防止同类脏数据再次把整条流水线拖死数天；重建 `temporal-worker` 镜像并重启生效，重启期间正在跑的 7/20 批次活动自动续跑未受影响。

**待观察**：这次修复只处理了"NUL 字节导致 activity 永久失败"这一诱因，没有改变"活动失败后 workflow task 本身可能陷入无限重试"这个更底层的 SDK/序列化层行为——如果未来出现其他类型的非法负载（而非 NUL 字节）触发同样的 gRPC 序列化失败，同样的卡死模式可能复现。是否需要在 `EnrichArticleWorkflow` 层面加一个"整体超时/`workflow_execution_timeout` 兜底"来防止任意原因导致的无限卡死，留待下次观察期问题出现时再评估，不在本次改动范围内。

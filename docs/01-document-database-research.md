# 文档数据库选型调研

> 对应 [00-overview.md](./00-overview.md) 结论摘要第一行。调研范围：Markdown 全文 + 灵活元数据的存储与查询，部署规模为"个人/小型项目自托管（单台 VPS 或 Docker host，千级文档量，日增几十篇，几 GB 数据）"，消费方是 Python（Celery/Temporal）后端 + Astro SSR 前端。

> **2026-07-04 修订**：初版结论是"默认 SQLite，成长到 PostgreSQL"，前提是假设两者运维成本有明显落差。但 [02-pipeline-orchestration-research.md](./02-pipeline-orchestration-research.md) 确定编排层用 Temporal 之后，出现一个改变判断的硬事实：**Temporal 生产环境的持久化后端强制要求 PostgreSQL（12+）或 MySQL（8.0.17+）——SQLite 官方明确只支持 dev/test，不支持生产**（[Temporal Persistence 文档](https://docs.temporal.io/temporal-service/persistence)）。也就是说，只要自托管 Temporal，Postgres 就已经是不可绕开的依赖；这时候内容索引再单独起一个 SQLite 文件，不是"省了一个组件"，而是"多了一个存储引擎"。结合用户明确的"AInews 要长期运转"这一前提，结论改为**直接上 PostgreSQL + JSONB，与 Temporal 共用同一个 Postgres 实例（各开独立 database，不混表）**。原 SQLite 分析保留在下文，作为"仅需要独立原型验证索引/检索逻辑、尚未接 Temporal"这个场景下的备选，不再是生产推荐。

## 结论速览（排序）

| 排名 | 方案 | 定位 |
|---|---|---|
| 🥇 首选 | **PostgreSQL + JSONB**（tsvector / pg_trgm） | 与 Temporal 共用同一 Postgres 实例（独立 database），一次部署两处复用，长期运转的稳妥落点 |
| 🥈 原型验证备选 | **SQLite（FTS5 + JSON1）** | 仅适合"先于 Temporal 独立验证内容索引/检索逻辑"这个早期阶段；不支持 Temporal 生产持久化，不作为最终产品数据库 |
| 🥉 基线对照 | **MongoDB** | 文档模型原生，但对此规模偏重，SSPL 授权，用户已熟悉但非必需 |
| 观望 | **SurrealDB** | 多模型 + BM25 + 向量检索，适合未来 RAG 实验层，不宜当权威主库 |
| 特定场景 | **CouchDB** | 仅当"离线优先同步"成为刚需时才选（Obsidian LiveSync 即基于它） |

核心判断：**此负载（千级文档、日增几十、几 GB、单机、Python 管道批量写入、前端读多写少）本身用不到重型数据库；但既然 Temporal 已经把 Postgres 带进了基础设施，复用它比额外引入 SQLite 更简单**。用户熟悉 MongoDB，但它在这个体量属于"杀鸡用牛刀"。

## 关键横切问题：git 权威 vs DB 权威

> **2026-07-04 修订（三）**：以下原结论是照搬现有 AInews vault 项目的"vault ↔ 管道解耦"惯性得出的，前提是"数据库只是给前端查询用的派生索引，git/Markdown 才是真正驱动系统运行的东西"。但 `ainews-project` 一旦切到 Astro SSR 直接查库，**git 就不再处于任何请求路径或运行时依赖链上**——它原本存在的理由（Obsidian 打开查看、diff 审阅、vault 同步）对一个纯服务化系统而言不再是刚需，除非显式选择保留。结论翻转为：**Postgres 是 `ainews-project` 唯一的权威存储，git/Markdown（如果保留）只是一个脱离主流程的可选导出/备份能力，不再是"数据库的上游"**。

**结论：PostgreSQL 是 `ainews-project` 的唯一权威存储。git（如果保留）是可选的、脱离主流程的下游导出，不是数据库的上游来源。**

理由：
- SSR 请求路径直接查 Postgres，git 完全不在这条链路里，把它保留在"每次 pipeline 跑都要 commit+push"这个关键路径上没有运行时收益，只是历史包袱；
- 是否需要 git 导出，取决于是否还想要"能用 Obsidian 打开浏览"或"要一份人类可读的 diff 历史"这类**产品性**需求，而不是系统运行的**功能性**需求——这两类需求性质不同，不该用同一套机制强绑；
- **重要连带影响**：既然 DB 不再是"随时可从 git 重建的派生物"，Postgres 自身的备份策略（`pg_dump` 定期快照 / WAL 归档做 PITR）就从"锦上添花"变成**硬需求**——不能再靠"backup = backup git"这套简化叙事了，这是这次修订之后必须补上的一项。

如果确实想保留 git 导出能力（比如继续在 Obsidian 里浏览、或想要一份可审阅的内容变更历史），推荐做法是：**导出方向反过来**——写一个独立的、脱离主 pipeline 的 `git_export` 任务（可以是完全独立的定时任务，或者手动触发），定期把 Postgres `documents` 表的当前内容导出成 Markdown 文件并 commit，而不是让每次内容更新都强制走 git。这样"要不要导出、多久导出一次"和"内容能不能正常提供服务"完全解耦，导出任务挂了也不影响线上系统。具体是否保留、按什么节奏导出，见 [03-architecture-proposal.md §7](./03-architecture-proposal.md) 待决问题。

## 逐方案评估

### SQLite（FTS5 + JSON1）
- **Schema 弹性**：JSON1 存 frontmatter，任意字段可查；不需要预先固定 schema。
- **全文检索**：FTS5 提供 BM25 排序、短语/前缀匹配、高亮片段，对 Markdown 全文完全够用，远超"关键词/标签查询"这条底线。
- **运维**：单文件、零守护进程、备份即拷贝文件、监控几乎为零。
- **Python 生态**：标准库内置 `sqlite3`；FastAPI 场景用 SQLAlchemy / `aiosqlite`，生态最成熟。
- **注意点**：单写者并发——但在"Temporal worker 批量写、前端只读"的模式下，配合 WAL 模式完全不构成问题。
- 与"git 派生索引"模型是天作之合。

### PostgreSQL + JSONB
- **Schema 弹性**：JSONB + GIN 索引，灵活度接近文档库，且能混合关系模型（未来如果要做用户/订阅等关系型数据，同一引擎覆盖）。
- **全文检索**：`tsvector` 倒排 + GIN 索引，`pg_trgm` 补模糊/错拼容错，百万文档量级仍可亚秒响应，是官方文档级方案。
- **运维**：需要守护进程、连接池、备份策略（`pg_dump` / PITR），比 SQLite 重，但生态极其成熟。
- **Python 生态**：`psycopg3` / `asyncpg` + SQLAlchemy 一流，是 FastAPI + Celery/Temporal 的标配组合。
- **定位**：这是"未来一定会长大"时最稳的落点——一个引擎覆盖关系 + 文档 + 全文 + （`pgvector`）向量检索。

### MongoDB（基线对照）
- 文档模型原生，`pymongo` / `motor` 成熟。
- text index 可用，但相关性排序弱于 PG 的 tsvector 或 SQLite FTS5。
- 对几 GB 体量而言运维偏重；SSPL 授权对自托管场景存在合规噪音。
- 除非团队已经在 MongoDB 上标准化，否则此项目用它性价比偏低。

### CouchDB
- 唯一杀手锏是 master-master 复制与离线优先（PouchDB 生态，Obsidian LiveSync 插件即基于它）。
- Mango 查询引擎能力偏弱，无内建相关性全文（要另挂 `couchdb-lucene` 这个 JVM 组件），开发节奏已放缓，存储开销偏大。
- 对"可查询索引"这个具体需求属于倒退，**不推荐**——除非哪天要做多端离线同步（例如给移动端做离线优先读写）。

### SurrealDB
- 最新 3.x 版本，多模型（文档 + 图 + 关系），内建 BM25 全文 + 向量 + 混合检索，还能以嵌入式方式跑在 Python 进程内。
- 对"AI 资讯 + 未来 RAG 检索增强"有很强的想象空间。
- 但项目相对年轻，社区/生态规模、Python SDK 成熟度、备份/监控经验都不如 PostgreSQL 沉淀深。
- **当权威主库风险偏高；更适合作为独立的 RAG/向量实验层，与主存储解耦并存**，不建议现在就压上生产路径。

## 最终推荐与场景边界

1. **直接上 PostgreSQL + JSONB，作为 git 的派生只读索引**——和 Temporal 自身的持久化存储共用同一个 Postgres 实例，开一个独立 database（如 `ainews_content`），不与 Temporal 内部表混用。前端直接查这个 database 做动态渲染，彻底去掉"重建静态站点"这个环节。
2. **SQLite 仅用于两种场景**：(a) 在正式接入 Temporal 之前，想先独立跑通"内容索引 + 全文检索 + 前端查询"这条链路做原型验证；(b) 本地开发环境不想连远程 Postgres 时的临时替代。这两种场景下用完即弃，不需要纠结迁移成本，因为索引本来就是可从 git 重建的派生物。
3. **MongoDB** 仅在明确想要文档原生模型且不想碰 SQL 时，作为 PostgreSQL 的替代——但对本项目没有独特优势。
4. **SurrealDB** 留给未来的 RAG PoC，不宜现在就当主库。
5. **CouchDB** 只有当"离线多端同步"变成刚需时再考虑。

**一句话结论：Temporal 已经把 Postgres 带进了基础设施，内容索引直接复用它，MongoDB/SQLite（生产用途）都可以跳过。**

## Sources

- [Temporal Persistence 官方文档（生产环境后端要求）](https://docs.temporal.io/temporal-service/persistence)
- [SQLite FTS5 官方文档](https://www.sqlite.org/fts5.html)
- [JSON1 + FTS5 + Python 实践](https://charlesleifer.com/blog/using-the-sqlite-json1-and-fts5-extensions-with-python/)
- [PostgreSQL 全文检索文档](https://www.postgresql.org/docs/current/datatype-textsearch.html)
- [GIN 索引：JSONB / 数组 / 全文检索](https://dev.to/philip_mcclarence_2ef9475/postgresql-gin-indexes-jsonb-arrays-full-text-search-29i2)
- [PostgreSQL 全文检索作为 Elasticsearch 轻量替代](https://iniakunhuda.medium.com/postgresql-full-text-search-a-powerful-alternative-to-elasticsearch-for-small-to-medium-d9524e001fe0)
- [SurrealDB Features（全文/向量/嵌入式）](https://surrealdb.com/features)
- [SurrealDB Python SDK](https://github.com/surrealdb/surrealdb.py)
- [CouchDB 3.5 Mango 查询文档](https://docs.couchdb.org/en/stable/ddocs/mango.html)
- [couchdb-lucene 全文检索插件](https://github.com/rnewson/couchdb-lucene)
- [用 CouchDB 自托管替代 Obsidian Sync 的实践](https://medium.com/@abhirajsinghtomar/i-replaced-obsidian-sync-with-a-self-hosted-couchdb-server-heres-how-you-can-too-9d2f1aaa1f62)

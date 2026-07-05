# M5 — Aggregate 阶段完整化（聚类 + 写作规则）

> 前置依赖：[M4-enrich.md](./M4-enrich.md)
> 状态：**已完成**（2026-07-05）
> 关联文档：[04-roadmap.md](../04-roadmap.md) §2.5（本里程碑的权威规则来源，唯一允许跨文章判断的地方）· §4 M5

## 目标

完整实现 `aggregate_activity`——topic 聚类桶与粒度规则、`is_new` 强制规则、zettel 复用三级判断、Daily/Topic/Digest 写作规则、tags 四轴策略。

## Scope（范围内）

完整实现 04 §2.5 全部规则。

## Out of scope（明确不做）

- 前端渲染（留 M6，虽然可以并行开始）

## 任务清单（全部完成，落地方式见下）

- [x] Topic 聚类预设主题桶：`model-releases` / `safety-alignment` / `opensource-tools` / `research-papers` / `policy-regulation` / `industry-moves` / `funding-investment` / `infra-hardware` / `applications` / `agents`；按"事件类型"分类，**不按来源/公司分类**
- [x] 分桶粒度规则：桶内 <2 条归并入杂项；>8 条考虑拆分子类；新领域涌现 ≥3 条可创建新 topic
- [x] `is_new` 判定强制规则：唯一依据是"该 slug 是否存在于当前实际的 topic 记录清单里"——不允许凭经验/推荐桶名称推断；下游 Write 阶段应以实际存储状态为最终依据，聚类误判要记录并纠正，不能将错就错
- [x] Zettel 入选标准（三选一）：概念/方法首次出现（全库检索确认无对应笔记）；重大事件锚点（半年后回看仍重要）；可复用洞察（含"可被引用"的关键判断）
- [x] Zettel 复用判断三级优先：① 跨日索引里该 URL 是否已有 `zettel_id` → 有则直接复用 ② 索引没有则按 slug 前缀搜索现有笔记库，命中则复用 ③ 都未命中才视为新概念创建
- [x] 产出量软性指标核查：单次运行建议 3-10 张原子笔记，超过说明前置过滤不够严格；全部不达标是可接受的"低产日"
- [x] Daily 写作结构：TL;DR（3-5条关键事件）+ 昨日回顾（若存在）+ 按主题分组（五种情形渲染：有笔记+有归档 / 无笔记有归档 / 有笔记归档缺失 / 复盘+旧笔记 / 复盘+未升级笔记）+ 本日数据统计
- [x] Topic 写入铁律：首次创建写完整 frontmatter，**后续追加绝不整体重写**（会丢历史）；日期区块必须倒序（最新在前），需判断当天区块是否已存在再决定"区块内追加"还是"插入到最新区块之前"
- [x] Digest 五项自检硬约束（违反视为输出失败）：① 禁止合成条目，每条必须对应唯一原始条目 ② 来源标识必须与注册表逐字一致，不可意译 ③ URL 字段必填且直接取自结构化数据源，不能从渲染文件反查 ④ 去重自检（同 URL/同标题不出现两次）⑤ 每条 2-3 句、硬性字符上限（120 字）
- [x] Tags 打标策略：四轴分类（技术领域/产品公司/事件类型/来源质量标签），每条 2-5 个 tag，kebab-case 全小写，不发明新分类轴，不打宽泛无信息量标签
- [x] Wikilink 格式：原子笔记用时间戳 ID，主题用 slug，日报用日期，原文归档用 ID（同文件名 stem）；**其余各层引用条目应优先双链到原文归档层**，只有归档失败（ID 为 null）才回退外链

## 验收标准

- [x] 人工抽查一次完整跑的产出，逐项对照 §2.5 checklist 全部通过（真实 14 源批次，146 篇保留、145 篇 enrich 成功，165 条文档写入：145 original + 8 zettel + 10 topic + 1 daily + 1 digest）

## 备注 / 风险

- 这是"唯一允许跨文章判断的地方"——实现时要明确区分哪些判断依赖同批次其他文章（属于这里），哪些不依赖（应该已经在 Enrich 阶段做完），避免职责边界混乱。M5 前置调研（旧项目 cluster/writer/digester agent 定义）确认：事件级去重早在 filter 阶段（M2）完成，aggregate 阶段唯一的跨文章判断就是"这批 entries 该怎么分桶"，不是再做一次事件合并。
- `is_new` 判定是历史上容易出错的点：一定要查实际存储状态，不能靠模型"记得"或"觉得应该有"。

## 落地方式说明

### 补齐 Original 归档层（架构缺口修正）

M1-M4 遗留的 `aggregate.py` 用"zettel"顶替了 04 §2.6 五类文档里"Original"（原文归档）的角色——每篇 enriched 文章无差别生成一条 zettel，从未真正落地"选择性创建原子笔记"这条规则。M5 补齐：

- **`original`**：每篇 enriched 文章都建，`doc_id` 用 `original-<sha256(url)[:12]>`（按 URL 稳定 hash，不用时间戳）——保证同一 URL 跨批次重新处理（如跨日去重"已淡出"后重新保留）天然 upsert 到同一条记录，不产生重复归档；`body_md` 是真正的完整译文（`articles.translated_summary`，此前从未写进 `documents`，只有 gist 被保留过）。
- **`zettel`**：只有聚类判断 `zettel_worthy=true` 的文章才创建/复用，走三级复用判断；复用时不改写已有内容。`original.frontmatter.related_zettel_id` 与 `zettel.frontmatter.original_id` 互相显式引用（不依赖字符串 ID 约定配对，比"共用 HHMM"更不容易出错）。

### Topic 聚类 + 分桶粒度规则

一次跨文章 LLM 调用（`ClusterAssignment` tool schema）判断每篇文章的 `topic_slug` 建议 + `zettel_worthy`，**不含 is_new 字段**——是否新建 topic 完全由代码核验（查 `documents WHERE doc_type='topic'` 的实际 slug 列表）。分桶粒度规则的两个数字（"<2 归并"与"新领域 ≥3 条"）对同一类判断的表述略有出入，实现时统一为：新 topic 候选本批次 <3 条 → 降级并入 `uncategorized`；已存在的 topic 不受最低条数限制；单 topic 本批次 >8 条只记录建议拆分子类的日志，不自动拆分（自动拆分需要模型二次判断合理的子类命名，一次性做到位风险较高）。

**真实批次实测踩坑**：136+ 篇文章一次性塞进一次聚类/打标调用会被 `max_tokens` 截断（`instructor.IncompleteOutputException`），单纯调高 `max_tokens` 只是把问题推迟到更大的批次——改成按 `_CLUSTER_BATCH_SIZE`（20 篇/块）分块调用，沿用 enrich.py 翻译阶段"按段落分块"的既有模式，才是不随批次规模增长而失效的方案。分块不影响 `is_new`/粒度规则的正确性（这些是在全部分块结果合并后统一计算的）。

### Zettel 三级复用判断 + 悬空引用防御

三级判断：① `url_index.zettel_id` 命中 → 复用 ② 按 slug 后缀查 `documents(doc_type='zettel')` 命中 → 复用 ③ 都未命中 → 新建（沿用已有的 12 位时间戳 ID 方案）。真实验证中发现（由验证过程自身的表不同步触发，但确认是代码层面值得补的真实健壮性缺口）：如果 ① 命中的 `zettel_id` 对应的文档实际已不存在（`url_index` 与 `documents` 理论上不该失步，但一旦失步），直接信任会在 `write_activity` 写 `links` 表时触发外键违反导致整批写入失败。修复：①额外校验 `document_id_exists(zettel_id)`，不通过则退化走②/③，不放大成硬失败。

### Topic 追加铁律的文本层实现

`documents` 表是整行 UPSERT，数据库不懂"追加"语义——追加铁律在 `aggregate.py` 里实现：写入前先读旧文档的 `body_md`/`frontmatter`，判断"当天日期区块是否已存在"（`## <date>` 标题行精确匹配）决定"区块内追加"还是"新区块插入到最新区块之前"（倒序），计算出完整的新 `body_md`/`frontmatter`（`article_count` 累加、`created_date` 保留、`last_updated_date` 刷新）再整行写回。真实 DB 往返验证过：手工把一个 topic 文档的日期区块改成"昨天"再触发新一轮写入，确认新区块正确插入在旧区块之前、历史条目一字不丢。

### Daily 五种情形分类 + Digest 五项自检

五种情形（有笔记+有归档 / 无笔记有归档 / 有笔记归档缺失 / 复盘+旧笔记 / 复盘+未升级笔记）按 `(is_recap, zettel_id 是否存在, fetch_channel 是否为 placeholder)` 三个信号机械分类；`is_recap` 复用 `url_index.first_seen_date != today` 判断，与 Zettel 三级判断①共用同一次 `url_index` 查询，不重复查库。TL;DR 只做"选哪几条"的跨文章比较判断（`DailyHighlights` schema），文案直接复用 enrich 阶段已产出的 gist，不重新生成摘要文本；批次 ≤5 篇时跳过这次 LLM 调用。Digest 五项自检全部机械实现：来源名逐字比对 `sources.yaml` 注册表、URL 直接取自结构化字段、去重用 `set` 防御、120 字上限用"优先句末标点截断，否则硬截断+省略号"机械处理；自检失败的条目记录警告并跳过，不让整批失败。

### Tags 四轴 + tags/links 表首次真正启用

`tags`/`links` 两张表建表以来（M0）从未写入过。M5 起 `write_activity` 每次 upsert 文档后同步重建该文档的 `tags` 行（先删后插，反映最新判断）+ 增量插入 `links` 出边（**只增不删**——Topic/Daily 这类追加型文档的历史出边需要长期保留，不能因为某次调用只传入"本批次新增的链接"就把历史边冲掉）。Digest 明确不产生 `links` 边（定位"去 wikilink、可独立分享打印"，与 Daily/Topic/Zettel/Original 不同）。

### zettel_worthy 判断偏松，靠 prompt 收紧 + 软性指标日志双重把关

首次真实批次（136 篇）判定 66 篇 `zettel_worthy`（48%），远超"单次运行 3-10 张"的软性指标——根因是分块调用后每块（20 篇）独立判断，模型没有"全局配额"概念。收紧 prompt 措辞（明确"大多数文章都不应该 zettel_worthy，每 20 篇里预期只有 0-3 篇够格"）后复测，同规模批次降到 8 篇，落入建议区间。同时补了一条镜像 topic ">8条" 模式的诊断日志：新建 zettel 数超过 10 或低于 3 都会记录日志，供后续批次持续观察（不做自动裁剪——机械砍掉多出来的笔记没有依据判断该保留哪些）。

### 真实批次超时配置修正

`aggregate_activity`/`write_activity` 的 Temporal activity 超时沿用的是 M1 纯 Python 版本的 30 秒遗留值——M5 起 `aggregate_activity` 含多次覆盖整批文章的 LLM 调用，`write_activity` 要为大幅增多的记录数（five 类文档）逐条 upsert+同步 tags/links，30 秒在真实批次下必然超时。分别调到 180 秒 / 90 秒。

### 测试基建（工程化收敛，M0-M4 全靠真实批次验证，从未有过单元测试）

新增 `backend/tests/`（pytest + pytest-mock），`backend/pytest.ini` 配置 `pythonpath=.`。覆盖范围：
- Stage A-F 全部纯逻辑分支：`is_new` 核验/分桶粒度规则、Zettel 三级复用（含悬空引用防御分支）、Topic 追加/首建分支（含真实 DB 往返验证过的插入定位逻辑）、Daily 五种情形分类、Digest 五项自检与截断逻辑、Original doc id 稳定性
- 回溯覆盖 M2/M4 已有的纯函数：`filter.py` 的 Jaccard/URL 归一化/同批次去重、`enrich.py` 的噪声行识别/CJK 占比计算
- 全部 mock 掉 `worker.db`/`worker.llm_client.call_structured`，不连真实 Postgres/LiteLLM，48 个用例本地运行 <1 秒
- 真实批次验证仍然保留作为最终验收手段（本次两轮全量批次 + 一次针对性的跨天追加真实 DB 往返验证）

### 已知的后续关注项（不阻塞本里程碑）

- `aggregate_activity` 的返回值（含全部文档的完整 `body_md`）触发过 Temporal `PayloadSizeWarning`（真实批次达 1.16MB，警告阈值 512KB）——尚未达到 Temporal 的硬性消息大小上限，暂不处理，但如果未来批次规模继续增长，需要考虑减少跨 activity 传递的数据量（如只传 doc_id 列表，body_md 直接在 activity 内部读写）。
- `>8 条建议拆分子类`目前只记录日志不自动执行，`research-papers` 桶真实批次里达到 74 条，明显需要人工评估是否要拆子类——留给后续批次观察积累后再决定。

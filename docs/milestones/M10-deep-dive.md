# M10 — Deep Dive（跨天聚合深度解读）

> 关联文档：[04-roadmap.md](../04-roadmap.md) §4 M10；旧系统设计参考：`/Volumes/Projects/AInews` 的 `40-Deep-Dives/`、`SCHEMA.md`、`.claude/skills/ai-news/references/vault-schema.md`
> 状态：已完成（2026-07-09）
> 决策记录：`.claude/memory/decisions.md`「M10：Deep Dive 实现落地」

## 背景

旧系统设计过"Deep Dive"（`40-Deep-Dives/`）——对 Digest 层做跨天/跨周二次聚合，识别"跨日延续主题、热门 topic 趋势线"，产出周报/月报形态的第五种内容类型。**这个功能在旧系统里从未真正实现过**：目录自建库以来只有一个空 `.gitkeep`，`git log` 无任何改动记录；对应的 `news-weekly-digester` subagent 从未创建；前端页面是"筹备中"空态。旧系统设计文档写的触发门槛是"**积累 ≥7 天 `30-Digests/` 历史**"。

M7 生产化收尾讨论时用户提出"这个后续做成独立 Temporal 工作流是否合适"，确认这是一个值得追踪但尚不能开工的待办，先新增本里程碑文件占位。2026-07-09 核查数据库确认 `documents` 表 `doc_type='digest'` 已积累 12 天连续历史（2026-06-28 ~ 2026-07-09，含 M8 迁移历史数据 + 新系统自身产出），远超门槛，用户提出"Deep Dive 的特性更适合跑一个周任务"，正式启动本里程碑。走完整设计确认流程（Plan Mode + Mermaid 架构图 + 用户显式批准）后分 4 步实现。

## 触发条件（已达成）

新系统 `documents` 表 `doc_type='digest'` 积累足够天数历史（沿用旧系统"≥7 天"门槛作参考起点）。2026-07-09 核查时已有 12 天连续历史，门槛达成，正式启动实现。

## 设计与实现

**这是 M0-M9 里第一个"旧系统没有可迁移先例"的里程碑**——不参照旧系统的任何"已验证规则"（旧系统这个功能从未跑通过），是净新设计。完整设计对齐见 `.claude/memory/decisions.md`，摘要如下：

- **触发方式**：独立 Temporal Schedule `ainews-deep-dive-weekly`（`worker/worker.py::ensure_deep_dive_schedule`，每周一 09:00 Asia/Shanghai），跟主流水线、arxiv 全文回补同一时间点但完全互不阻塞。
- **数据聚合规则（机械统计，不重新聚类）**：窗口为触发日前 7 天（`window_end` = 触发日前一天，`window_start = window_end - 6 天`）；查询 `documents WHERE doc_type='original'` 在窗口内按既有 `topic_slug` 分组（**不重新调用聚类 LLM**，直接复用每篇文章 aggregate 阶段已判定的分类——"跨文章判断只能发生在 aggregate 阶段"这条项目铁律的自然延伸）；"热门 topic"入选规则是机械双门槛 `total_count >= 3 且 active_days >= 2`，按 `total_count` 降序取前 8 个，`uncategorized` 溢出桶不参与评选；命中 0 个仍正常产出文档（机械兜底文案，不跳过整周）。
- **LLM 使用边界**：唯一一次 LLM 调用只生成一段周叙事导语（新增 `DeepDiveIntro` tool schema），不参与"哪个 topic 算热门"的判断，只能引用给定素材（热门 topic 统计 + 代表文章 gist + 逐日 Digest 原文）中的事实。
- **输出**：新增独立 `doc_type='deep_dive'`（`documents.doc_type` 是无约束 TEXT 列，不需要 migration），`doc_id` 格式 `deep-dive-{window_end}`，`frontmatter` 含结构化 `trending_topics`/`entry_count`/`source_digest_ids`，正文含 wikilink 回 topic/original。完全只读输入 + 单条新增输出，不改写任何既有 Topic/Daily/Digest/Original/Zettel 文档——比 arxiv 全文回补更彻底的"完全解耦"。

## 落地方式

**Step 1（后端核心）**：`backend/worker/db.py` 新增 `deep_dive_list_original_documents_in_window`/`deep_dive_list_digest_documents_in_window`（延续 `filter_*`/`aggregate_*` 的按消费模块前缀命名惯例）；`backend/worker/schemas.py` 新增 `DeepDiveIntro`；`backend/worker/aggregate.py` 的 `_topic_heading` 改名为跨模块公开的 `topic_heading`（供 deep_dive.py 复用同一套 emoji/中文名映射，视觉一致）；新建 `backend/worker/deep_dive.py`（趋势统计 `compute_deep_dive_trends_activity` + 导语生成/落库 `generate_deep_dive_activity` 两个 activity，`uncategorized` 溢出桶排除在趋势评选外）；`backend/worker/workflows.py` 新增 `DeepDiveWorkflow`（两步 activity 串联，`window_end` 用 `workflow.info().start_time` 保证 replay 确定性）；`backend/worker/worker.py` 注册新 Schedule/workflow/activity。

**Step 2（单测+冒烟）**：新建 `backend/tests/test_deep_dive.py`（14 个用例，覆盖趋势统计/双门槛筛选/降序截断/0 命中兜底/`link_targets` 排除 digest id 等纯逻辑分支）；全量测试套件 148→149 全部通过。冒烟验证：把改动文件拷进运行中的 `temporal-worker` 容器，用真实窗口数据（2026-07-02~07-08，451 篇原文）跑通完整链路（不落库），确认趋势统计与真实 LLM 生成的导语质量符合预期。

**Step 3（前端）**：`frontend/src/lib/doc-type.ts`/`frontend/src/components/enhance/LuminaBacklinks.astro` 补齐 `deep_dive`（这两处若遗漏会分别导致 `docHref()` 死链接、反链静默丢失——冒烟阶段类型检查主动发现的必改项，不在最初的改动清单里）；`pagination.ts` 新增 `DEEP_DIVES_PAGE_SIZE`；`live.config.ts` 新增 `deepDives` collection；`deep-dives/index.astro` 从"筹备中"占位页改造为真实列表页（镜像 `digest`/`topics` 列表页模式），新建 `more.astro`（分页片段端点）与 `[slug].astro`（详情页，正文含 wikilink，走 `getLiveEntry`+`render`+`LuminaBacklinks` 完整渲染管线，顶部用 `ChipsRail` 展示热门 topic 趋势卡片）。类型检查（`astro check`）过程中抓到一个真实 bug：`getLiveEntry` 第一个参数必须是 `live.config.ts` 里的 collection 变量名（`deepDives`），不是 doc_type 原始值（`deep_dive`）——已修正。

**Step 4（部署验证）**：重建 `temporal-worker`/`web` 两个镜像并重启，确认 `ainews-deep-dive-weekly` Schedule 正确注册。**手动触发一次真实执行时发现一个真实 bug**：`compute_deep_dive_trends_activity` 的返回类型标注是未指定字段类型的 `-> dict`，`pydantic_data_converter` 解码这类跨 activity 边界传递的 dict 时，`window_start`/`window_end` 两个 `date` 字段会退化成 ISO 字符串（`date.isoformat()` 直接调用报 `AttributeError`），单测因为是直接函数调用（不经过 Temporal 序列化）没有暴露这个问题。修复：在 `generate_deep_dive_activity` 入口做显式双态兼容（`isinstance` 判断后 `date.fromisoformat()`），补一条回归单测覆盖字符串输入路径。修复后重新构建镜像、重启、再次手动触发，`documents` 表成功写入 `deep-dive-2026-07-08`（8 个热门话题，`links` 表新增 32 条出边），真实 HTTP 请求验证列表页/详情页均 200，wikilink 正确解析，Topic 详情页反链栏正确出现"深度周报"分组。这条真实产出的周报直接保留在库里作为上线后第一条数据，不清理。

## 边界约束（已落实）

- 不重新聚类：topic 归属直接复用每篇文章已有的 `topic_slug`，聚类 LLM 只在主流水线的 `aggregate_activity` 里调用一次。
- 不修改任何既有 Topic/Daily/Digest/Original/Zettel 文档：`DeepDiveWorkflow` 全程只读 `original`/`digest`，只新增一条 `deep_dive` 记录。
- v1 只做周报，不做月报：留待以后视需求评估，避免过度设计。
- 不做"上周同期对比/增长率"环比分析：数据周期尚不够长，留待以后视需求评估。

## 备注

这是 M0-M9 里第一个"旧系统没有可迁移先例"的里程碑，走了完整的 CLAUDE.md 抽象设计确认流程（Plan Mode + Mermaid 架构图 + 用户显式批准）才开始编码，全过程分 4 步执行并逐步确认，两处真实 bug（前端 `getLiveEntry` collection key 不匹配、后端 Temporal 跨 activity dict 序列化丢失 date 类型）均由类型检查/真实端到端触发验证主动发现并修复，不是靠猜测推断代码正确。

## 追加：重点加粗 + emoji 图标 + Mermaid 趋势饼图（2026-07-09，同日）

用户看了首条真实周报后要求"增加重点加粗、图标，可以引入 mermaid 渲染"，并指出"加粗"应该扩大复用面到 Daily/Digest。同样先给出设计提案（复用范围/图表类型/渲染方案三点）经用户确认后再实现，分 3 步落地：

- **加粗复用到共享源头**：`enrich.py::gist_activity`（`ArticleGist` schema）的 prompt 新增"用 Markdown `**加粗**` 标出 1-2 个核心关键词"指令——`gist` 是 Daily TL;DR/Daily 主题条目/Digest blurb/Deep Dive 代表文章行**共用的唯一摘要来源**，改一处即可让四个消费点同时生效，不需要分别处理。连带发现并修复一个真实的截断安全问题：Digest 的 `_truncate_blurb`（120 字硬截断）如果截断点刚好落在 `**加粗**` 标记中间会留下奇数个 `**`，渲染成 Markdown 时变成裸露星号而不是加粗——新增 `_balance_bold_markers` 辅助函数在截断后砍掉未闭合的加粗片段，补了单测覆盖。Deep Dive 自己的导语（`DeepDiveIntro`）和热门 topic 统计行也加了同样的加粗指令。
- **emoji 图标**：Deep Dive"本周数据统计"区块四行各加语义化 emoji（🗓️📰📈📅），topic 小标题本来就有 emoji（`TOPIC_EMOJI`）不用新加。
- **Mermaid 趋势饼图**：`deep_dive.py` 新增 `_build_trend_pie_chart`，直接从 `compute_deep_dive_trends_activity` 已经算好的 `trending_topics` 结构化统计机械拼接 mermaid `pie` 语法（不经过 LLM，避免图表语法出错或数字跟真实统计不一致），插在导语之后、分主题正文之前；0 个热门 topic 时不生成（没有数据可画）。**前端引入净新能力**：新增 `mermaid` npm 依赖 + `MermaidRenderer.astro`（客户端脚本，只接入 `deep-dives/[slug].astro`，不是全站默认行为）——找 `pre[data-language="mermaid"]` 提取 `textContent` 用 `mermaid.render()` 转 SVG 替换原来的代码块，监听 `astro:page-load` 适配全站 ClientRouter。实现前先用真实渲染管线做了实测（不是猜测）：确认现有 `rehype-sanitize` schema **不需要改动**——`code` 元素默认已允许 `/^language-./` 的 class，Astro/Shiki 实际把 mermaid 代码块渲染成 `<pre data-language="mermaid">`，而 `pre` 的 `data-language` 属性早就在白名单里（M6 起就有）。

**真实验证**：后端新增 6 个单测（149→154 全部通过）；重建两个镜像重启后，重新手动触发 `DeepDiveWorkflow`（`window_end` 计算结果与首条记录相同，天然 upsert 刷新同一条 `deep-dive-2026-07-08`），数据库核对加粗/emoji/饼图代码块均正确生成；**用 Playwright 真实打开详情页截图确认**——饼图确实渲染成了带图例的彩色扇形图（不只是"代码能跑通"），加粗关键词、emoji 图标全部正常显示，浏览器控制台 0 报错。

## 追加二：加粗力度加强 + 每日产出量柱状图 + 热度/延续性象限图（2026-07-09，同日）

用户反馈"加粗内容还是有点少"，明确要求"每个文章的摘要信息都要有重点加粗，方便快速扫描抓重点"，并要求"再深度挖掘一下是否还有其他图表可以作为可视化内容"。

- **加粗力度**：`gist_activity`/`ArticleGist`/`DeepDiveIntro` 三处 prompt 从"1-2 个关键词"提升到"2-4 处关键词/短语，或一句不超过15字的核心结论短句"，覆盖度明显提高。
- **图表深度挖掘**：实现前用 Playwright 真实调用 mermaid 11.16.0（不是猜测）测试了 4 种候选图表（单系列柱状图/多系列折线图/象限图均渲染成功），综合"不堆砌图表"原则确定加 2 张而非全加：① `_build_daily_volume_bar_chart`——窗口内每日原文产出量，机械统计自 `_compute_daily_counts`（按日期填满窗口全部 7 天，含 0 篇的日子，保证柱子数量固定）；② `_build_trend_quadrant_chart`——按 (延续天数, 相对热度) 把每个热门 topic 定位到"持续热点/集中爆发/边缘话题/细水长流"四象限，这是最直接对应 M10 设计里"热门 topic 趋势线"这个说法的图，坐标机械归一化到 (0,1] 区间。放弃了多主题折线图（8 条线视觉太乱）和单独的"延续天数"柱状图（象限图已经把这个维度包含进去，重复）。三张图（柱状图/饼图/象限图）分别回答"节奏/占比/性质"三个不同问题，象限图要求至少 2 个热门 topic 才生成（1 个点归一化后 y 必为 1 没有对比意义）。
- **真实发现并修复一个 mermaid 解析器 bug**：象限图上线首次真实渲染就在浏览器控制台报错——不是本项目代码问题，是 mermaid `quadrantChart` 词法分析器解析不了形如 `"1.0"` 这种小数点后只有一个尾随零的浮点数字面量（`[0.5, 1.0]` 报 Lexical error，`[0.5, 1]` 裸整数或 `[0.5, 0.99]` 非整数小数都正常）。这不是理论边缘情况——坐标固定归一化到 (0,1]，批次里 `total_count` 最高的那个 topic 必然精确算出 `1.0`，**每次都会触发**。排查过程没有靠猜测：先怀疑加粗/换行/中文字符，逐一隔离测试（Playwright 直接调 `mermaid.render()` 二分排查标题/坐标轴/象限标签/数据点各个字段组合），最终定位到具体是浮点数格式问题。修复：新增 `_format_quadrant_coord` 辅助函数，整数值（1.0/0.0）格式化成不带小数点的整数字面量，其余保留两位小数；补了直接针对这个 mermaid 解析器行为的回归单测。

**真实验证**：后端新增 4 个单测（154→159 全部通过，含专门锁定 mermaid `"1.0"` 解析 bug 的回归测试）；重建镜像重启后重新触发 `DeepDiveWorkflow` 刷新同一条记录；**用 Playwright 真实截图确认三张图表全部正确渲染**（柱状图/饼图/象限图，象限图坐标点正确落在四象限空间内），浏览器控制台 0 报错，导语里能看到新加强的加粗（导语每次都是新生成的）。**当时未发现的遗留问题**：各 topic 小节下面列出的代表文章摘要仍然一处加粗都没有——这批 `gist` 是文章最初 enrich 时一次性生成、存在 `documents.original.frontmatter.gist` 里的历史值，Deep Dive 只读取现有值不会重新生成，prompt 改动只对之后新处理的文章生效，历史 gist 需要单独回填，见「追加三」。

## 追加三：历史 gist 加粗回填 + 图表配色区分度（2026-07-09，同日）

用户看到报告后指出"每篇的摘要信息都没有任何加粗重点信息"，并要求"删掉这篇 deep dive 重新跑一次"；同时反馈三张图表"颜色区分度太低"，要求加强。

- **诊断**：核实 `original-1bce1ac8a146` 等代表文章的 `frontmatter.gist` 确实不含任何 `**` 标记——这些文章是本周批次（2026-07-02~07-08）早于今天加粗 prompt 改动前就已经 enrich 完成的历史数据。**单纯"删掉重新跑"不能解决问题**：`DeepDiveWorkflow` 只读取 `documents.original` 里已经存在的 `gist` 字段，不会重新调用 LLM 生成——重跑只是用同样的旧 gist 再拼一遍报告，加粗不会凭空出现。已把这个根因和"delete+rerun 实际无效"的结论说明给用户，转而执行正确的修复路径。
- **修复**：先用 `compute_deep_dive_trends_activity` 拿到本周报告实际引用的 24 篇代表文章 doc_id（8 个热门 topic × 每个最多 3 篇），针对性地对这 24 篇（不是全部 451 篇窗口内文章——只有代表文章的 gist 会出现在报告正文里，全量回填成本不成比例）用新 prompt 重新调用 `gist_activity` 生成 gist，直接 `UPDATE documents SET frontmatter = jsonb_set(...)` 写回 `frontmatter.gist`（只改这一个字段，不碰 `topic_slug`/`related_zettel_id`/`body_md` 等其他字段），随后重新触发 `DeepDiveWorkflow` 刷新报告。**这个回填只影响 Deep Dive 的展示**——不会改变对应文章在已发布的 Daily/Digest/Topic 里的历史正文（那些文档在创建时已经把旧 gist 文本原样写死进自己的 `body_md`，不是运行时动态引用 `original.frontmatter.gist`），不算重写历史发布内容。
- **图表配色**：mermaid 默认主题 8 个饼图色彼此明度/饱和度接近，8 个热门话题挤在一起区分度低。`MermaidRenderer.astro` 改用 `theme: 'base'` + 自定义 `themeVariables`（改自 Tableau 10 的高区分度定性调色板，饼图/柱状图/象限图数据点统一复用同一套色）。**过程中发现之前"站内没有深色模式"的判断是错的**——重新核查 `frontend/src/lib/theme-toggle-client.ts`/`tokens.css` 的 `[data-theme='dark']` 覆盖，确认站点确实有真实的深色模式切换，之前判断依据的是一次不完整的 grep 结果；顺手把 `mermaid.initialize()` 也改成读取 `document.documentElement.dataset.theme` 动态选取明暗两套文字/线条/象限背景色（数据本身的调色板两种主题下复用不变）。**排查配色不生效的过程走了弯路**：一开始在独立的 Playwright 沙盒页面里用 `esm.sh` CDN 动态 import mermaid 测试自定义 `themeVariables`，反复验证 `pie1`/`primaryColor` 等变量始终不生效（配置对象本身能读到正确值，但渲染出的 SVG 颜色岿然不动）——怀疑是 CDN 重新打包 mermaid 内部图表类型的懒加载 chunk 时破坏了主题配置的模块间共享状态。改为直接在真实项目环境（Vite 打包）里改完 `MermaidRenderer.astro` 重建镜像验证，一次就确认配色完全生效，说明沙盒环境不能代表生产环境下的构建行为，及时切换验证方式没有继续在错误的路径上打转。

**真实验证**：24 篇代表文章 gist 回填后加粗标记从 11 处增至 112 处；重新触发 `DeepDiveWorkflow` 刷新报告；**用 Playwright 直接从真实渲染出的 SVG 提取 `fill` 属性核对**（不是肉眼判断"看起来是不是更好看"）——饼图 8 个扇形分别对应调色板 8 种颜色、象限图 4 个背景区域对应 4 种柱状图未涉及的浅色调、象限点与柱状图主色一致，浏览器控制台 0 报错。

## 追加四：每个热门 topic 小节接入深度分析引擎，取代机械 bullet 列表（2026-07-09，M11 期间同日）

M11（专题月报）首版真实产出后用户反馈"太浅薄，像罗列文章"，追溯发现周报的每个热门 topic 小节（`_render_trend_section`）从 M10 上线以来就是**纯机械 bullet 列表，全程没有 LLM 参与**，是这个观感的直接根因之一。这次重写是周报+月报共享的同一次改动，完整设计与真实验证记录在 [`M11-topic-deep-dive.md`「追加：深度叙事引擎重写」](./M11-topic-deep-dive.md#追加深度叙事引擎重写周报月报共享五维度分析--关系图2026-07-09同日)，不重复展开。周报侧的关键变化：`generate_deep_dive_activity` 现在给每个热门 topic 循环调用共享的 `_generate_topic_analysis`（五维度深度分析 + 关系图，深挖全文上限 5 篇），`DeepDiveWorkflow` 超时按热门话题数动态估算；原机械列表降级为小节末尾"参考文章"辅助链接。

## 追加五：周报素材来源从 zettel 反查改为全部原文子主题聚类，与月报同构（2026-07-20）

用户看了 7/13~7/19 这期周报后指出"只有一个 topic 有延续性/交叉验证/分歧/新兴信号，其他都是空的，似乎返回有问题"。排查确认这不是 LLM 调用异常，而是「追加四」接入深度分析引擎时遗留的一个真实数据源缺口：`generate_deep_dive_activity` 当时给每个热门 topic 查的分析素材是 `topic_deep_dive_list_zettel_documents_in_window`（zettel 反查），而 `zettel_worthy` 判定本身很苛刻——核查这一周实际数据，8 个热门 topic 里只有 `industry-moves` 凑够 2 篇 zettel，其余 7 个（含本周 200 篇原文的 research-papers）一篇都没有，直接触发 `_generate_topic_analysis` 的"素材皆空"机械兜底，展示成"本周该专题暂无可用于生成分析的素材"+ 四个空维度。这正是 M11 期间"追加：全部原文子主题聚类"已经诊断并修复过的同一类问题（[`M11-topic-deep-dive.md`「追加二/追加三」](./M11-topic-deep-dive.md)），但当时只把修复应用到了月报，代码注释里也留了痕迹（`_select_fulltext_original_ids` 函数文档明确写着"周报专用（月报...不再经过 zettel 反查）"）——是当时刻意的范围切分，没有跟进回补周报这一侧。

**修复**：`generate_deep_dive_activity` 改用 `topic_deep_dive_list_original_documents_in_window` 查该 topic 本周**全部**原文（不再过滤 zettel），复用月报已有的 `_cluster_topic_articles`（子主题聚类，0 条有效线索时退化成"整个 topic 当一条线索"）+ `_select_cluster_fulltext_ids`（每子主题独立挑选深挖全文，上限沿用 `WEEKLY_TOPIC_FULLTEXT_LIMIT=5`，语义从"整个 topic 固定上限"变成"每子主题独立上限"）；`_render_topic_analysis_section`（周报单 topic 一段分析）替换成 `_render_topic_cluster_sections`（一个 topic 可能渲染多个子主题小节，结构与月报 `_build_topic_deep_dive_record` 一致），`_build_deep_dive_record` 的正文组装与 `link_targets` 按子主题校验逻辑同步调整。清理了随之变成死代码的 `_select_fulltext_original_ids`（deep_dive.py）与 `topic_deep_dive_list_zettel_documents_in_window`（db.py）。

**单测同步重写**（`test_deep_dive.py` 100 个用例全部通过，全量套件 252 个通过），重建镜像重启后用 `temporal schedule trigger` 重新生成本期周报——**这次触发暴露了本次改动自身引入的问题，没有一次成功**，完整排查与最终真实验证结果见「追加六」，不在这里重复。详见 `.claude/memory/decisions.md`「周报素材来源从 zettel 反查改为全部原文子主题聚类」。

## 追加六：部署后连续暴露三个真实 bug——超时预算/max_tokens/Schedule 定义未随代码更新（2026-07-20，同日）

「追加五」部署后，用户要求顺手补跑历史周报验证效果，过程中连续触发三个独立故障，都源于"改了核心逻辑但没同步调整依赖这段逻辑固有特性的周边配置"：

- **超时预算没跟上聚类改造**：`DeepDiveWorkflow` 给 `generate_deep_dive_activity` 的超时公式还是按旧版"每 topic 固定 1 次分析调用"估算（22分钟），改版后单个 topic 变成"1次聚类+最多7次子主题分析"，本周批次连续 3 次撞 `StartToClose timeout` 硬顶失败。修复：仿 `translate_activity` 的既定心跳模式，逐子主题分析完成后 `activity.heartbeat(...)`，超时改用宽裕的 worst-case 兜底 + `heartbeat_timeout=90s` 负责快速发现真卡死。
- **聚类调用 max_tokens 不够用**：大 topic（`research-papers` 单周 200 篇原文）聚类输出体积大，`llm_client.DEFAULT_MAX_TOKENS=8000` 在上周(7/12)回补批次真实撞出 `IncompleteOutputException`，3 次重试确定性失败。修复：显式传 `max_tokens=16000`；顺带修复 `_cluster_topic_articles` prompt 硬编码"本月"（周报调用这个共享函数时素材明明是"本周"）的问题，新增必填 `window_label` 参数。
- **Schedule 定义没有随 workflow 签名变化自动更新**：`DeepDiveWorkflow.run` 改成接收 `params: DeepDiveParams` 后，`ensure_deep_dive_schedule` 是"不存在才创建"的幂等模式，几周前就建好的 `ainews-deep-dive-weekly` Schedule 被判定"已存在"直接跳过，服务端保存的 Action 定义停留在旧的零参数版本，触发后 `DeepDiveWorkflow.run()` 缺参数直接抛 `TypeError`——跟当天早些时候 NUL 字节事故是同一种故障模式（Python 异常导致 workflow task 无限重试），只是诱因换成了配置漂移。处理：终止卡死实例 + 删除旧 Schedule 让 worker 重启时用当前代码重建。

**真实验证**：三处修复同批次部署（单测新增 2 个，全量套件 254 个通过），重建镜像、确认无其他运行中工作流后重启；`schedule trigger` 重新生成本周报告（`deep-dive-2026-07-19`，`RunTime 25m52s`，`COMPLETED`）+ 手动指定 `window_end` 回补上周报告（`deep-dive-2026-07-12`，`RunTime 33m23s`，`COMPLETED`，验证了 `DeepDiveParams.window_end` 这个新增的手动回补能力）。两份都核实正文里"暂无可用于生成分析的素材"出现次数为 0（此前 7/8 个 topic 命中这句兜底文案），且产出了真实的多子主题结构——本周 8 个热门 topic 里 5 个识别出 5-7 条子主题、3 个（素材本身少）合理退化成 1 条；上周更丰富，8 个里 7 个产出 6-7 条子主题。详见 `.claude/memory/decisions.md`「周报 generate_deep_dive_activity 部署后连续暴露三个真实 bug」。

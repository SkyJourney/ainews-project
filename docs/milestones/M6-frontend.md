# M6 — 前端上线

> 前置依赖：依赖 `documents` 表 schema（M0 已定），不依赖 Enrich/Aggregate 的完整业务规则——可以在 M4/M5 收尾阶段就先动手
> 状态：**已完成**（2026-07-05）
> 关联文档：[04-roadmap.md](../04-roadmap.md) §2.7 · §4 M6 · [00-overview.md](../00-overview.md) §2 目标（"内容更新=数据库写入，前端下一次请求立即可见"）

## 目标

Astro SSR 改造，复用现有组件设计，五类页面接入 Postgres 查询，并验证"无需重建"这一核心架构卖点。

## Scope（范围内）

- Astro 6，`output: 'server'` + Node adapter，Live Content Collections 请求时查 Postgres
- 复用现有组件/布局/Tailwind 设计，五类内容页面渲染
- wikilink 渲染改为 DB 查询解析（不再是文件存在性检查）
- 页面级缓存（Node middleware 或 CDN + Cache-Control），避免每请求都打库

## Out of scope（明确不做）

- 修改现有视觉设计（组件/布局/Tailwind 样式是已验证资产，本里程碑只做数据源切换，不重新设计 UI）

## 任务清单（全部完成，落地方式见下）

- [x] Astro 升级/配置为 `output: 'server'` + Node adapter
- [x] 接入 Live Content Collections，loader 指向 Postgres（`ainews_content` 库的 `documents` 表）
- [x] 五类内容页面（Daily/Topic/Zettel/Digest/Original）接入真实查询，复用现有组件/布局/Tailwind 设计
- [x] wikilink 渲染逻辑改为查询 `links` 表解析，替换原来的文件存在性检查
- [x] tags 页面/过滤功能接入 `tags` 表
- [x] 页面级缓存策略：Node middleware 或 CDN + `Cache-Control`，避免每次请求都直接打库

## 验收标准

- [x] 浏览器访问可见 M1-M5 产出的真实内容
- [x] **验证"无需重建"这一核心卖点**：手动往 `documents`/`tags` 表插一条测试记录，刷新页面立即可见，全程不碰 Astro 容器（不重新构建、不重启）——对着真实持久化部署（`ainews-service-web` 容器）实测通过

## 备注 / 风险

- 这是本次重构相对旧系统"每次都要重新发布静态站点"模式的根本区别所在，验收标准第二条不要简化或跳过——它是检验架构目标是否真正达成的关键测试。

## 落地方式说明

### 前置调研发现：版本目标与预期不符

旧前端（`/Volumes/Projects/AInews/web/frontend`）git 历史显示确实从 Astro 5 起步，但后续依赖升级已经推到 **7.0.5**（不是计划里写的"升级到 6"）。核实后确认 SSR 所需能力（`output:'server'` + adapter + Live Content Collections，后者在 Astro 6.0 从 experimental 转正为 stable）在 7.0.5 上均已具备，最终方案是**维持 7.0.5，只做 SSR 配置改造**，不做版本变更。

### 迁移策略：复用旧前端代码，只换数据源两处

`web/frontend` 整体复制进新仓库 `frontend/`（components/layouts/lib/styles/public 原样带过来），只替换两处：
- **数据获取**：`vault-loader.ts`（build-time 扫描 Obsidian vault 目录）→ 自建 `postgres-loader.ts`（实现 `astro/loaders` 的 `LiveLoader` 接口，`loadCollection`/`loadEntry` 请求时查 `documents` 表）。**关键技术细节**（官方文档没写清楚，读 `astro` 包源码才确认）：Live Loader 返回的条目不走 Astro 内置的 build-time markdown 渲染管线（那条管线只认 `filePath`/`deferredRender`），Live Loader 必须自己调用 `@astrojs/markdown-remark` 的 `createMarkdownProcessor` 把 markdown 编译成 HTML 塞进 `rendered.html`，`astro:content` 的 `render()` 才能用。
- **Wikilink 存在性判断**：`wiki-link.ts` 从查内存 vault 缓存改成批量查 Postgres（先遍历收集本文档全部 `[[target]]`，一次 SQL `WHERE id = ANY($1)` 查询，不是逐个查）。

### 补齐架构缺口：Original 归档层（详见 M5 记录）

M5 已经把 Original/Zettel 拆分成两个独立文档类型，前端页面结构相应调整：Original 是永远可靠的归档层（每篇文章都有），Zettel 是精选层（`zettel_worthy` 才有）。Zettel 详情页的"来源"栏改为链接回 `original_id`（不再假设 Zettel 有单一固定来源——M6 起 Zettel 可被多篇文章复用，不再是"一篇文章一张卡"）。

### 数据契约缺口：部分旧字段没有直接的新 schema 等价物

旧 Zod schema（`content.config.ts`）里一些字段在新的 `documents.frontmatter` 里没有直接对应（`original_title`/`fetched_at`/`language`/`translated`/`translation_engine`/`sources_alive`/`sources_dead`），逐一核实后简化或替换：
- `Daily.topics`（数组）+ `Daily.previous_daily` 在 M5 的 `aggregate.py` 里原本只计算但没写进 frontmatter，顺手补上（两行改动，已有单测覆盖）
- `Original.related_daily/related_zettels/related_topics`（旧schema假设一对多）→ 改为 M5 实际的 `topic_slug`（单一）+ `related_zettel_id`（单一），反向发现"谁引用了我"改由 `LuminaBacklinks`（查 `links` 表）统一承担
- `tags` 不在 frontmatter 里（独立建表），`postgres-loader.ts` 统一查 `tags` 表注入
- `Zettel` 列表页摘要预览原来靠 body 前 160 字，Live entry 没有 build-time `CollectionEntry` 那样的原始 `.body` 字段，改用 `frontmatter.gist`（enrich 阶段已产出，语义更贴切）

### Tags/Digest：从零设计的新功能（旧前端完全没有）

`30-Digests` 目录在旧前端从未被消费过，`tags` 也只是不可点击的展示 chip——这两块是新增页面而非迁移：`/digest/`（列表+详情，body_md 无 wikilink，不接 `LuminaBacklinks`）+ `/tags/`（标签云 + 按标签筛选文档列表），复用现有 `IntelligenceCard`/`HeroSection` 组件，未新增视觉设计。

### 搜索：Pagefind → Postgres 全文搜索

Pagefind 的"构建后扫描 `dist/` 静态 HTML 建索引"模型与 SSR 架构冲突（页面不再一次性生成）。改为 `/api/search` 路由查 `documents.body_tsv`（GIN 索引 M0 建表时就预留好，这次第一次用上），`ts_headline` 生成高亮摘要。踩坑：SQL 里 `regexp_replace` 的反向引用 `\1` 直接写进 JS 模板字符串会被当成非法 legacy octal 转义（ESM 模块天然严格模式），构建直接报 SyntaxError——改成把正则/替换串参数化传入解决。

### 图片资产：自定义 scheme 改写 + 独立静态服务端点

`ainews-media://` 引用（M4 配图分级渲染产物）渲染时改写成 `/media/<path>`；没有引入 nginx 反代（明确超出 M6 范围，留给 M7 生产化收尾决定是否引入），用一个轻量 Astro API 路由（`src/pages/media/[...path].ts`）直接读盘返回，含路径穿越防护。

**安全审查发现并修复**：`enrich.py` 的配图下载本身就接受 `image/svg+xml`，SVG 可以内嵌 `<script>`——同源用 `image/svg+xml` 直出，若有人直接导航到图片链接（或被 `<object>`/`<iframe>` 嵌入），浏览器会执行其中脚本，构成 XSS。`<img>` 标签场景本身不受影响（浏览器不会在 `<img>` 里执行 SVG 脚本），修复只针对 `.svg` 响应加了一条 `Content-Security-Policy: default-src 'none'; style-src 'unsafe-inline'; sandbox`，不影响正常图片显示，只是给"直接打开图片链接"这类场景做兜底防护。

### 部署：新增 `web` 服务，两份 docker-compose 同步

`frontend/Dockerfile`（多阶段构建）+ `infra/docker-compose.yml`/本机部署版都新增 `web` 服务，只读挂载 `images` 卷。本机部署版用 `WEB_PORT=8802`（旧 AInews 静态站点占了 8801）。**未引入 nginx**：M6 验收标准不需要，且当前其余服务（Postgres/Redis/Temporal UI）也都是直接端口暴露，没有统一反代层，留给 M7 生产化收尾统一考虑是否引入。

### 真实批次验收

对着真实持久化部署（`ainews-service-web` 容器，连接真实 `ainews_content` 库、144+ 篇真实文章）逐页验证：全部 8 类页面（含 `/tags/`）返回 200，144 个 wikilink 全部正确解析（0 断链），配图正确显示，搜索返回真实高亮结果，容器全程无错误日志。核心卖点验证：手动 `INSERT`/`UPDATE` 真实数据库记录，同一个持续运行、未重建未重启的容器立即在下一次请求里反映变化。

### 验收后发现并修复：标题重复渲染

浏览器实测发现五类详情页标题都渲染了两遍——`aggregate.py` 的五个 `_build_*_record` 函数都在 `body_md` 开头拼了一遍 `"# {title}"`，但 `title` 本来就是独立字段（`documents.title` 列 + `frontmatter.title`），前端详情页（`HeroSection`/`orig-detail-title`）已经单独渲染过一次，body_md 里的这份是纯粹冗余（对 Original 类型还会跟原文本身自带的标题结构叠成三层 H1）。

修复：五个函数全部去掉 `"# {title}\n\n"` 前缀，`body_md` 只保留正文本身。`_insert_topic_block` 依赖"查找第一个 `## ` 标题"定位插入点的逻辑不受影响（不关心前面有没有 H1）。已有 Topic/Zettel 走"复用"路径的历史文档不会自动重写（复用语义就是不改内容），只对全新创建的文档生效——真实批次验证时用清表重新生成的方式复验证过全部五类文档不再重复。补了 5 条回归单测（`backend/tests/test_aggregate.py`），断言各 `_build_*_record` 输出的 `body_md` 第一行不是标题本身。

## 追加优化：日期格式修正 + 六个列表页分页懒加载 + 渲染性能优化（2026-07-05）

M8 迁移写入 272 条真实历史文档后暴露出 M6 验收时数据量太小、没触发到的两个问题：一是 `doc_date` 显示成 `Sat Jul 04 2026 00:00:00 GMT+0800 (China Standard Time)` 这类 `Date.toString()` 格式（而非 `yyyy-MM-dd`）；二是 Sources（Originals）列表页随数据量增长打开要好几秒。

### 根因与修复

- **日期格式**：`node-postgres` 默认把 Postgres `DATE`（oid 1082）解析成 JS `Date` 对象，模板字符串插值时走 `toString()`。在 `frontend/src/lib/db.ts` 模块顶部用 `pg.types.setTypeParser(1082, (value) => value)` 在驱动层直接返回原始字符串，一处修复覆盖全站，不需要逐页面加格式化逻辑。
- **列表页慢的真实根因不是"没分页"，是 `postgres-loader.ts` 的 `toLiveEntry()` 对每一行都做整篇 markdown→HTML 渲染 + 两次 N+1 查询（`fetchBacklinks`/`fetchTags`）**——268 篇 Originals 的列表页原本要做 268 次渲染 + 536 次额外查询。新增 `fetchDocumentSummariesByType`/`fetchBacklinkCounts` 等轻量查询（`frontend/src/lib/db.ts`），列表页不再经过 Live Loader，只查列表展示需要的字段 + `LIMIT/OFFSET` 分页。

### 六个导航列表页（Daily/Digest/Topics/Zettel/Originals/Tags）分页 + 滑动懒加载

分页大小按各页面卡片形态/增长速度分别设置（`frontend/src/lib/pagination.ts`：Daily 10/Digest 15/Topics 30/Zettel 20/Originals 24，Tags 60 但未接懒加载，范围见下）。共用一份 vanilla JS（`frontend/public/infinite-scroll.js`，无框架依赖）：`IntersectionObserver` 监听哨兵元素 + 400px 预加载余量，`data-infinite-list`/`data-endpoint`/`data-mode`（flat|grouped）等 `data-*` 属性传参；首屏内容不满一屏时自动补一批（`maybeAutoFill`，最多 20 轮安全阀）。Tags 详情页只接了轻量查询，未接懒加载（明确不在"六个导航列表页"范围内）。

**过程中发现并修复两个真实 bug**：
1. Astro 的 scoped CSS 哈希是按**源文件**算的，`index.astro`/`more.astro` 各自内联同一段卡片标记会得到不同哈希、样式对不上——修复为抽成共享组件文件（`OriginalCard.astro`/`ZettelCard.astro`/`TopicCard.astro`/`DailyMonthGroup.astro`），两个页面都 import 同一份。
2. 全站已接入 `<ClientRouter />`（View Transitions），导航是客户端 DOM 替换而非整页刷新，`<script src="/infinite-scroll.js">` 的顶层代码只在首次加载时跑一次——切换列表页后懒加载失效（用户报告的 bug，现象是"刷新当前页恢复正常，切到别的页/切回来都不行"）。修复为监听 `astro:page-load`（首次加载+每次切页后都触发）重新初始化，`astro:before-swap` 断开旧 `IntersectionObserver`；用真实 Playwright 点击导航（非 `page.goto`）复现并验证修复。

### Topic 详情页渲染性能：缓存 + 按区块懒加载

Topic 是唯一无限期持续累积内容的文档类型（每天追加一个 `## YYYY-MM-DD` 区块）。实测搭建临时基准页验证：真实数据 46KB/9 区块渲染 56ms，合成 5 倍规模 269ms，合成 20 倍规模 1.7s——渲染耗时随历史长度**超线性**增长。用户要求"两个方案都做"：
- `frontend/src/lib/markdown-render.ts` 加一个按 `content_hash` 为 key 的内存 LRU 缓存（`Map` 插入序实现，300 条上限），命中重复请求时跳过重新渲染。
- `frontend/src/pages/topics/[slug].astro` 改为只渲染最近 `TOPIC_DETAIL_SECTIONS_PER_BATCH`（3）个日期区块，其余通过 `frontend/src/pages/topics/[slug]/more.astro` 片段端点按需懒加载（`frontend/src/lib/topic-sections.ts` 按 `## YYYY-MM-DD` 标题行整块切分，不会像 Daily 按月分组那样出现"同一区块被分页切断"）——从根本上让单次渲染成本不再随历史无限增长，而不只是缓存重复访问的成本。

### 真实验证

Playwright 真实浏览器验证：Originals/Zettel 列表页滚动触发 24→48/20→40 张卡片正常追加；Topic 详情页滚动触发 3→9 个日期区块懒加载，199 处 wikilink 全部正确解析；ClientRouter 导航场景（Originals→Zettel→切回 Originals，全程不刷新）复现并验证懒加载 bug 修复生效。backend 54 个 pytest 用例全部通过（含 M5 追加的 Daily 标题回归测试）。

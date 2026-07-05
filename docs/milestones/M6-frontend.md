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

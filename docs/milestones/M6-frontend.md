# M6 — 前端上线

> 前置依赖：依赖 `documents` 表 schema（M0 已定），不依赖 Enrich/Aggregate 的完整业务规则——可以在 M4/M5 收尾阶段就先动手
> 状态：未开始
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

## 任务清单

- [ ] Astro 升级/配置为 `output: 'server'` + Node adapter
- [ ] 接入 Live Content Collections，loader 指向 Postgres（`ainews_content` 库的 `documents` 表）
- [ ] 五类内容页面（Daily/Topic/Zettel/Digest/Original）接入真实查询，复用现有组件/布局/Tailwind 设计
- [ ] wikilink 渲染逻辑改为查询 `links` 表解析，替换原来的文件存在性检查
- [ ] tags 页面/过滤功能接入 `tags` 表
- [ ] 页面级缓存策略：Node middleware 或 CDN + `Cache-Control`，避免每次请求都直接打库

## 验收标准

- [ ] 浏览器访问可见 M1-M5 产出的真实内容
- [ ] **验证"无需重建"这一核心卖点**：手动往 `documents` 表插一条测试记录，刷新页面立即可见，全程不碰 Astro 容器（不重新构建、不重启）

## 备注 / 风险

- 这是本次重构相对旧系统"每次都要重新发布静态站点"模式的根本区别所在，验收标准第二条不要简化或跳过——它是检验架构目标是否真正达成的关键测试。

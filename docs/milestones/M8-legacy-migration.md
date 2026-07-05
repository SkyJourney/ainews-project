# M8 — 历史数据迁移

> 前置依赖：设计已完成（2026-07-05）；**实际导入执行**建议等 [M7-production-hardening.md](./M7-production-hardening.md) 7 天观察期结束（预计 2026-07-12）确认新系统稳定运行后再做
> 状态：**设计已定稿，尚未执行导入**
> 关联文档：[04-roadmap.md](../04-roadmap.md) §4 M8 · §2.6（ID/slug 命名规则）

## 触发条件

原定触发条件为"M7 验收标准达成后才启动"。2026-07-05 用户明确要求提前开始分析旧系统文件、设计迁移方案，覆盖了这条既定规则——**提前的是分析与设计，不是实际执行**。真正跑导入脚本、写入生产 Postgres 仍按原计划放到 M7 观察期结束之后，避免迁移执行本身给数据库带来的负载/风险与"观察期内新系统需独立稳定运行"这个验证目标冲突。决策记录见 `.claude/memory/decisions.md`。

## 目标

把旧 AInews vault（`/Volumes/Projects/AInews`）里新系统没有覆盖到的历史内容导入 Postgres `documents`/`tags`/`links` 表，作为种子数据补齐，不影响新系统已产出内容的权威性。

## Scope（范围内）

- `50-Zettel/`（112 篇）、`20-Topics/`（13 个 slug）、`10-Daily/`（9 天）、`30-Digests/`（8 天）、`60-Originals/`（199 篇）
- 按下方"数据现状与迁移策略"处理新旧重叠内容的去重/合并/优先级

## Out of scope（明确不做）

- `00-Inbox/`（95 个 JSON，pipeline 内部 IPC 中间态，非内容文档）
- `40-Deep-Dives/`、`90-Archive/`（均为空目录，仅 `.gitkeep`）
- `99-Log/`（10 个运维日志，非内容文档）
- 本次不写导入脚本、不执行任何实际写入（见"触发条件"）

## 数据现状调研结论（2026-07-05）

- 旧 vault 内容文档共 341 个，时间跨度 2026-06-27～2026-07-05（约 9 天，`git log` 首末提交印证）
- **新旧系统时间窗口存在真实重叠**，不是简单的"旧数据在前、新数据接续在后"：新系统 Postgres 当前 `original` 148 篇（`doc_date` 即文章发布日，跨 2026-06-21～07-05）、`zettel` 8 篇（06-25～07-03）、`daily`/`digest`/`topic` 目前都只有 07-05 当天的批次产出
- 已知脏数据（迁移解析器必须处理）：
  - 19 处"裸 12 位时间戳无 slug"的悬空 wikilink（如 `[[202607010911]]`），无法匹配任何真实文件
  - 部分 Originals 正文把 arXiv 论文脚注 `[22]` 误转义成 `[[22]]`，与合法 wikilink 语法冲突
  - YAML frontmatter 引号风格不统一（曾导致旧系统 Astro 构建失败过一次），必须用标准 YAML 库解析，不能假设引号约定
  - `related_zettels`/`related_topics` 两个"应回填"字段在全部 199 个 Originals 样本里恒为空数组，回填逻辑从未生效，不能作为关联数据源，需要靠正文实际 wikilink 重建关系
- 可复用的解析逻辑：旧前端 `web/frontend/src/lib/vault-loader.ts`（`gray-matter` 解析 + wikilink 正则提取）与 `slug-utils.ts` 的 `classifySlug()`（按 ID 形态分类文档类型），可以作为导入脚本的分类/解析起点

## 迁移设计（目录 → doc_type 映射与冲突策略）

| 旧 vault 目录 | 数量 | 旧 ID 格式 | 新 doc_type | 新 ID 格式 | 冲突策略 |
|---|---|---|---|---|---|
| `50-Zettel/` | 112 | `YYYYMMDDHHmm-slug` | `zettel` | 同上（格式一致，无需转换） | 按 `source_url` 反查对应 `original-<hash>` 是否已被新系统 zettel 化，已有则跳过 |
| `20-Topics/` | 13 | `slug` | `topic` | 同上 | **按日期区块合并，不整体覆盖**：只追加新系统 topic 文档里没有的日期区块，已存在的日期跳过 |
| `10-Daily/` | 9 | `YYYY-MM-DD` | `daily` | 同上 | **按 `doc_id`（日期）判重，新系统数据为准**：新系统已有当天记录则跳过，其余日期原样导入保留旧系统的 Daily 拆分粒度——这是本次用户明确要求保留的部分 |
| `30-Digests/` | 8 | `YYYY-MM-DD-digest` | `digest` | `digest-YYYY-MM-DD` | 同 Daily：按日期判重，新系统为准，其余日期补齐 |
| `60-Originals/` | 199 | `YYYY-MM-DD-HHMM-slug` | `original` | `original-{sha256(url)[:12]}`（**ID 需要按 `source_url` 重新计算，不能沿用旧文件名**） | 新 ID 已存在则跳过（新系统同一篇文章已抓取，且经过 M4/M5 完整 enrich/aggregate 规则，质量优先于旧 vault 版本）；不存在才导入 |

**总体优先级（已与用户确认）**：Original/Zettel/Topic/Digest 遇到冲突时新系统数据为准，旧 vault 只补新系统没有的内容；**Daily 例外**——旧系统的按天拆分本身就是要保留补齐的目标，不是"能不导入就不导入"的次要数据。

**Wikilink 重写规则**：只重写命中已知合法 ID 正则的链接（`YYYYMMDDHHmm-slug` / topic slug / `YYYY-MM-DD` 日期 / 新计算出的 `original-<hash>`）；已知的悬空裸时间戳链接与 arXiv 脚注伪链接一律跳过，不生成对应的 `links` 行（避免建出指向不存在文档的错误引用）。

## 任务清单（设计已定稿，实际执行留到 M7 观察期结束后）

- [ ] 编写迁移脚本（复用 `vault-loader.ts` 的分类/解析思路改写成 Python，或直接用 Python 生态的 YAML/frontmatter 库重新实现，不强求跨语言复用）
- [ ] 按上表策略逐类型导入：Zettel → Topic → Daily → Digest → Original（建议顺序：先处理不依赖去重判断的 Topic/Daily/Digest，再处理需要按 URL 反查的 Zettel/Original）
- [ ] 迁移后校验：抽样对比迁移前后 wikilink 解析结果、`tags`/`links` 表行数是否符合预期
- [ ] 更新本文件状态为"已完成"，补充真实执行的落地方式说明（数量统计、耗时、发现的额外边界情况）

## 验收标准

- [ ] 旧 vault 里新系统没有覆盖到的内容全部导入 Postgres，且不覆盖/破坏新系统已有的、经过完整规则链产出的内容
- [ ] 迁移后前端页面能正常展示这些补齐的历史内容，wikilink 正确解析（悬空/伪链接不产生错误引用）

## 备注 / 风险

- 本次设计阶段不写任何导入代码、不做任何实际数据库写入；真正执行前建议先在非生产环境（或至少先 `pg_dump` 一份完整快照）跑一次干跑（dry-run，只打印将要写入的记录不真正 upsert），核对数量与内容符合预期后再正式执行
- 迁移是一次性操作，不是常态化任务，执行完成后不需要保留迁移脚本的持续运行能力（区别于 `postgres-backup` 这类常驻服务）

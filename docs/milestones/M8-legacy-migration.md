# M8 — 历史数据迁移

> 前置依赖：无（已完成）
> 状态：**已完成（2026-07-05）**
> 关联文档：[04-roadmap.md](../04-roadmap.md) §4 M8 · §2.6（ID/slug 命名规则）

## 触发条件

原定触发条件为"M7 验收标准达成后才启动"。2026-07-05 用户先后两次要求提前推进——先是提前做分析设计，dry-run 验证通过后又明确要求"现在就跑"实际导入，两次都覆盖了既定的等待规则。执行前额外做了一次手动 `pg_dump` 快照作为安全网（`postgres-backup` 容器已就绪，见 M7）。决策记录见 `.claude/memory/decisions.md`。

## 目标

把旧 AInews vault（`/Volumes/Projects/AInews`）里新系统没有覆盖到的历史内容导入 Postgres `documents`/`tags`/`links` 表，作为种子数据补齐，不影响新系统已产出内容的权威性。

## Scope（范围内）

- `50-Zettel/`（112 篇）、`20-Topics/`（13 个 slug）、`10-Daily/`（9 天）、`30-Digests/`（8 天）、`60-Originals/`（199 篇）
- 按下方"数据现状与迁移策略"处理新旧重叠内容的去重/合并/优先级

## Out of scope（明确不做）

- `00-Inbox/`（95 个 JSON，pipeline 内部 IPC 中间态，非内容文档）
- `40-Deep-Dives/`、`90-Archive/`（均为空目录，仅 `.gitkeep`）
- `99-Log/`（10 个运维日志，非内容文档）

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

## 任务清单（全部完成）

- [x] 编写迁移脚本：`backend/scripts/migrate_legacy_vault.py`，复用 `worker.db`/`worker.aggregate`/`worker.filter` 里已有的 `upsert_document`/`document_id_exists`/`filter_lookup_url_index`/`_content_hash`/`_humanize_slug` 等函数，不重新发明写入逻辑；默认 dry-run，`--execute` 才真正写库
- [x] Dry-run 验证：对着真实 vault + 真实 Postgres 跑通，人工抽查合并/链接重写结果（见下方"Dry-run 结果"）
- [x] 实际执行导入（`--execute`）：执行前手动 `pg_dump` 一份快照作为安全网，正式写入 272 条文档
- [x] 迁移后校验：`documents`/`links`/`tags` 表行数符合预期，外键完整性检查 0 悬空引用，抽查真实前端页面（Daily/Topic/Original/Zettel 各一个迁移样本）全部 200 且渲染正确
- [x] 更新本文件状态为"已完成"，补充真实执行的落地方式说明

## Dry-run 结果（2026-07-05，已修复损坏文件后的最终结果）

```
original: 计划导入 138 条，跳过（新系统已有）61 条
zettel:   计划导入 106 条，跳过（新系统已有）6 条
topic:    计划导入 13 条（9 个合并回填历史日期区块 + 4 个全新创建）
daily:    计划导入 8 条，跳过（新系统已有）1 条（07-05）
digest:   计划导入 7 条，跳过（新系统已有）1 条（07-05）
悬空/伪 wikilink 丢弃：51 处
Original 旧 id → 新 id 映射：199 条（全部 341 个文件均解析成功，0 parse_errors）
```

人工抽查确认：`agents` topic 合并后日期区块正确降序排列（07-05 现有内容在前，07-04 起的历史内容追加在后，无重复）；`10-Daily/2026-07-03.md`/`2026-07-02.md` 里大量 `[[YYYY-MM-DD-HHMM-slug]]` 格式的旧 Original 引用全部正确重写成了 `[[original-<hash>]]`；Zettel 的合成 gist 字段可读、长度合理。

**修复了 1 处旧 vault 数据损坏**：`60-Originals/2026-07-04-0900-breaking-failure-cascades-step-aware-reinforcement.md` 的 `title` 字段单引号原本没有闭合（跨行吃掉了下一行 `original_title`），不是"未加引号含冒号"这种脚本能自动修复的已知模式，手动补上闭合引号后重跑 dry-run，全部 341 个文件解析零错误（该修复在旧 vault 自己的独立 git 仓库里，不随本仓库提交）。

## 真实执行结果（2026-07-05）

`python -m scripts.migrate_legacy_vault --execute` 正式写入 **272 条文档**（与 dry-run 计划的 138+106+13+8+7=272 完全一致）。执行前手动触发一次 `pg_dump` 快照（`postgres-backup` 容器）作为安全网。

`documents` 表迁移前后对比：

| doc_type | 迁移前 | 迁移后 | 净增 |
|---|---|---|---|
| original | 148 | 270 | +122 |
| zettel | 8 | 114 | +106 |
| topic | 10 | 14 | +4 |
| daily | 1 | 9 | +8 |
| digest | 1 | 8 | +7 |

**Original 净增（+122）比计划导入数（138）少 16**——核查发现旧 vault 自身存在 22 组、共 29 篇重复 `source_url` 的 Original 文件（同一篇文章被"🔄 [复盘]"多次重新建档，是旧系统自己从未做过跨日 URL 去重的已知缺陷，新系统的 `filter_activity` 正是为了解决这类问题而设计的）。这些重复文件按新 hash id 冲突会通过 `upsert_document` 的 `ON CONFLICT DO UPDATE` 合并成同一条记录（最后处理的版本生效），不是数据丢失，是预期内的去重副作用。

`links` 表最终 1068 行，`tags` 表 1405 行，外键完整性检查（`links.to_id` 全部能在 `documents` 里找到对应记录）0 条悬空引用。抽查 4 个迁移样本页面（`10-Daily/2026-06-27`、`20-Topics/opensource-tools`、一个迁移 Original、一个迁移 Zettel）在真实运行的 `ainews-service-web` 容器上全部返回 200 且标题/内容渲染正确。

## 验收标准（全部达成）

- [x] 旧 vault 里新系统没有覆盖到的内容全部导入 Postgres，且不覆盖/破坏新系统已有的、经过完整规则链产出的内容
- [x] 迁移后前端页面能正常展示这些补齐的历史内容，wikilink 正确解析（悬空/伪链接不产生错误引用）

## 备注 / 风险

- 迁移是一次性操作，不是常态化任务，执行完成后不需要保留迁移脚本的持续运行能力（区别于 `postgres-backup` 这类常驻服务）；`backend/scripts/migrate_legacy_vault.py` 保留在仓库里作为历史记录/未来同类迁移的参考，不会被定时调用
- 迁移前的快照（`ainews_content-pre-m8-migration-*.dump`）保留在 `postgres-backup` 的常规保留策略内，会在 `BACKUP_RETENTION_DAYS`（默认 14 天）后被自动清理

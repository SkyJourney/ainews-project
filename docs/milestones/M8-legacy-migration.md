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

## 迁移后清理（2026-07-05，真实执行结果之后追加）

用户核查发现两条问题 Original：

- `original-f09a58f2760f`（`stateof.ai`）与 `original-6f3f72961006`（`state.ai`）内容高度重复，均为 2025 版 State of AI Report——根因是旧系统抓取 `state.ai` 时 SSL 不可达，改从 `stateof.ai` 兜底抓取，但 `source_url` 仍记成原始 `state.ai` 地址，两个不同 URL 字符串各自 hash 出不同 doc_id，M8 迁移按 URL 去重无法识别。确认 `original-6f3f72961006` 零反链后直接删除。
- `original-6f972d4ea8f3`（Google Slides 演示文稿链接）`body_md` 是三级抓取全部失败后的占位文案——canvas/SVG 渲染结构性拿不到内容，判断不值得重抓，直接删除（1 处反链，前端优雅降级成灰色断链样式）。

详见 `.claude/memory/decisions.md`「M8 迁移后清理：域名 fallback 导致的重复 Original（State of AI Report 2025）」。

## 迁移数据修复（2026-07-05，全量工程审查发现并修复）

M8 完成后同一天做的一次全量工程审查发现，`migrate_legacy_vault.py` 本身有 3 类 bug，已经把错误数据写进了生产 `documents` 表（不是理论风险）：

1. **`doc_date` 误判**：`isinstance(published_at, date)` 判断假设 YAML 解析类型统一，加引号的 `published_at` 字符串会静默落到 `date.today()`——影响 47/120 条迁移 Original。
2. **`article_count` 语义错误**：把"待补日期区块数"当"文章篇数"累加——影响 13 个迁移 Topic 中的 9 个合并分支。
3. **frontmatter 字段名与权威 schema 不一致**：Original 缺 `word_count`/`gist`，Daily 用了 `stats.entry_count` 而非 `stats.articles_processed`——影响全部 120 条迁移 Original 与 8 条迁移 Daily，前端显示"0 字"/"0 条"。

修复过程中还发现一个第 4 类、更早存在的 bug：`strip_leading_title_block()` 的 blockquote 剥离正则漏了 `re.MULTILINE`，标题下连续两行引用（`> 原文：...` / `> 抓取：...：N 字`）只有第一行被剥离，第二行技术元数据残留在 Original/Daily 正文最前面。

**修复方式**：patch `migrate_legacy_vault.py`（4 处根因，确保未来任何重跑不会重现），新建一次性回填脚本 `backend/scripts/repair_m8_legacy_data.py`（dry-run 对照表确认后 `--execute`，执行前 `pg_dump` 快照），共修正 153 条文档（136 Original + 9 Topic + 8 Daily）。回填脚本重跑 dry-run 显示 0 条残留（幂等收敛），前端抽查全部正确。完整清单见 `.claude/memory/known_issues.md`。

## 验收标准（全部达成）

- [x] 旧 vault 里新系统没有覆盖到的内容全部导入 Postgres，且不覆盖/破坏新系统已有的、经过完整规则链产出的内容
- [x] 迁移后前端页面能正常展示这些补齐的历史内容，wikilink 正确解析（悬空/伪链接不产生错误引用）

## 备注 / 风险

- 迁移是一次性操作，不是常态化任务，执行完成后不需要保留迁移脚本的持续运行能力（区别于 `postgres-backup` 这类常驻服务）；`backend/scripts/migrate_legacy_vault.py` 保留在仓库里作为历史记录/未来同类迁移的参考，不会被定时调用
- 迁移前的快照（`ainews_content-pre-m8-migration-*.dump`）保留在 `postgres-backup` 的常规保留策略内，会在 `BACKUP_RETENTION_DAYS`（默认 14 天）后被自动清理

## 追加：`repair_m8_legacy_data.py` 本身引入了一个真实回归——Daily 正文的 wikilink 改写被撤销（2026-07-06，用户报告后排查修复）

用户报告"迁移过来的 daily 很多原文链接都是断的"。排查发现：`repair_dailies()`/`repair_digests()`（上面第 4 类 bug 的修复脚本）重算正文时只调用了 `strip_leading_title_block()`，没有重新走 `migrate_legacy_vault.py::resolve_links()` 那一步"旧 Original 文件名 wikilink → 新 hash id"的改写。首次迁移时这一步是做对的（`links` 表当时也确实记录了正确反链），但 repair 脚本用"直接读 vault 原始文件重算"的方式跟数据库里"已经改写过 wikilink 的正文"比较差异——只要正文里有改写过的 wikilink，两者就必然不同（差异根本不是 `re.MULTILINE` 那个 bug，而是 wikilink 改没改写），触发"需要修复"的误判，进而用没改写过的旧文本覆盖了原本正确的内容。

**实测影响**：3 篇 Daily（2026-07-02/03/04）、133 处 wikilink 100% 断链，其中 `arxiv-api`/`huggingface-daily-papers` 来源占比最高。

**修复**：`migrate_legacy_vault.py` 新增 `build_old_to_new_original_id_mapping()`/`rewrite_original_wikilinks()`（从 `resolve_links()` 抽出的独立复用函数），`repair_dailies()`/`repair_digests()` 重算正文时补上这一步；`repair_m8_legacy_data.py` 本身天然幂等（按当前 DB 状态与重算结果的差异判断要不要写），重跑一次就同时修正了已经跑错的 3 篇 Daily，不需要额外的一次性脚本。断链统计重跑归零，验证收敛。

**How to apply**：任何"重算正文再跟现有内容比较差异"的修复脚本，重算逻辑必须复用完整的原始处理链路（不能只挑其中一步），否则重算结果会跟"经过完整链路处理的现有正确内容"产生虚假差异，被误判为需要修复并回退。

# M4 — Enrich 阶段完整化（原文归档能力）

> 前置依赖：[M3-all-sources.md](./M3-all-sources.md)
> 状态：**已完成**（2026-07-04，作为 M2+M3+M4 合并阶段的 Stage D+E）
> 关联文档：[04-roadmap.md](../04-roadmap.md) §2.4（本里程碑的权威规则来源，本次架构的核心重设计）· §4 M4

## 目标

完整实现 `enrich_activity` 的三态处理 + Fallback A/B + 翻译完整性机械校验 + 配图分级渲染，并验证 Temporal 的持久化重试确实解决了旧系统"覆盖率不足靠人工补跑"的痛点。

## Scope（范围内）

完整实现 04 §2.4 全部规则：原文抓取三态、Fallback A/B、翻译逻辑与机械校验、配图分级渲染、字数统计、富元数据抽取、独立 upsert + 独立重试。

## Out of scope（明确不做）

- 跨文章判断（该归哪个 topic、是否与同批次其他文章重复）——这是 Aggregate 阶段（M5）的职责，Enrich 阶段严禁越界判断

## 任务清单（全部完成，落地方式见下）

- [x] 原文抓取三态：①direct（httpx+trafilatura，M1 已实现）②Jina Reader 兜底（M1 已实现）③ **Fallback A 用 Playwright 无头浏览器渲染**（旧系统用 Claude Code 的 WebFetch 工具，新后端没有这个工具，改用真实浏览器渲染——能过部分 JS 挑战/反爬，纯 HTTP 客户端做不到；backend 镜像新增 `playwright install --with-deps chromium`）
- [x] Fallback B：三态全部失败时的占位正文（标题+原文链接+说明），**不再抛异常**——直接返回占位记录，这是 M4 与 M1/M3 的关键区别：M1 时 `fetch_original_activity` 三态失败会 raise，M4 起变成"总是成功，只是内容质量分级"，直接对应验收标准的"originalize 覆盖率 100%"
- [x] 翻译逻辑：沿用 M1 的分块翻译（按段落贪心分块），语言判断沿用 CJK 占比启发式
- [x] 翻译完整性机械校验（硬约束）：CJK 占比（去代码块/URL 后）<50% 或检测到 HTML/LaTeX 残留（`ltx_`/`<td`/`<tr`/`class="ltx`）→ 判定校验不通过
- [x] 唯一允许的降级路径：校验不通过时保留首尾分块完整翻译，中间占位说明，`translation_fallback_notice` 如实记录（新增 migration 0006 字段），**不悄悄标记为完成**
- [x] 配图抓取分级渲染（只在状态①做，状态②③明确不下载图片）：成功→本地 Docker 卷 + `ainews-media://` 自定义 scheme 引用；失败/跳过→占位块+"查看原图"外链；视频占位图→"演示视频未归档"提示；UI 图标噪声/无内容占位图（1x1 像素）/已知跟踪像素域名→完全跳过
- [x] 字数统计：机械计算（去代码块后统计非空白字符数），不靠 LLM 自估
- [x] 富元数据抽取：新增 `metadata_activity`，用 `ArticleMetadata` tool schema 抽取 entities/content_type/novelty_keywords（migration 0006 补 `articles.entities`/`content_type`/`novelty_signal` 列的实际使用，这几列 M0 建表时就有但 M1-M3 一直没填）
- [x] 独立 upsert 进 `articles` 表，独立重试——沿用 M1 已验证的机制

## 验收标准

- [x] **50+ 条真实批次覆盖率测试**：全 14 源真实批次，168 篇文章，**enrich_failed=0（100% 覆盖率）**
- [x] originalize 覆盖率达到 100%（Fallback B 占位也算完成，不算失败）——本次实测全部通过 direct/jina 通道成功（Fallback A/B 在批次里没有实际触发，但已通过单独测试验证功能可用）

## 落地方式说明

- **新增外部依赖**：Playwright（`requirements.txt` + Dockerfile 新增 Chromium 安装）；新增 Docker 卷 `images`（`infra/docker-compose.yml` + 本机部署版 compose 同步更新，本机路径 `/Volumes/Docker/data/ainews-service/images`）
- **SSRF 防护**（安全审查发现的真实问题，不在原始任务清单里但属于本阶段必须补的硬化项）：`enrich.py` 新增 `_assert_public_url`/`_safe_get`，拒绝抓取解析到私有/回环/链路本地/云元数据网段的 URL，重定向逐跳重新校验。详见 `.claude/memory/decisions.md`「SSRF 防护」条目
- **实测踩坑并修复**：`trafilatura.extract()` 的 `include_images` 默认 `False`（不显式传 `True` 配图渲染结果会被丢弃）；图片引用改用自定义 scheme 而非根相对路径（`urljoin` 会把根相对路径错误解析成文章原站域名）；arxiv.org 摘要页固定引用工具区块 + 部分站点跟踪像素会拖累翻译完整性校验（降级率从 29% 修复到 9.5%，详见 decisions.md）

## 备注 / 风险

- 这是"本次架构的核心重设计"（04 §2.4 原文用语），50+ 条批次测试没有跳过或缩小规模——168 篇真实文章、14 个真实源，直接对应旧系统的真实故障案例（28% 覆盖率故障），这次验证结果是 100%。
- 边界约束反复检查过：Enrich 阶段的任何判断（gist/metadata）都只针对单篇文章本身，没有引入跨文章比较逻辑。

## 追加深化：翻译降级问题深度排查（2026-07-05）

M4 验收时翻译降级率是 9.5%（168 篇里 16 篇 `translation_fallback_notice` 非空），用户要求专门开一个阶段深挖，目标是"看能否让复杂文章翻译也做到无降级"。

**排查方法**：给 `translate_activity` 加逐块诊断日志（`[chunk_diag]`：CJK 占比/残留命中/是否判定为噪声/重试次数），跑真实全源批次（146 篇，17 篇失败）拿到精确数据，逐案例核对原文/最终 gist，把 17 个失败归成 5 类根因（详见 `.claude/memory/decisions.md`）。

**落地的修复**（`backend/worker/enrich.py`）：
- `_is_data_or_noise_line`/`_is_mostly_noise`：识别"数据行/图表提取噪声"（判据：一行里有没有长度>=3 的连续拉丁字母或长度>=2 的连续中日韩文字），噪声块跳过翻译调用，数据行从 CJK 占比分母里排除
- `_translate_chunk_with_retry`：单块译文 CJK 占比过低时带纠错提示重试，上限从 1 次加到 **2 次**（实测发现同一段原文独立重跑结果会因模型随机性摆动，多一次重试比放宽阈值更对症）
- `_strip_openai_header_nav` + `_KNOWN_BOILERPLATE_RE` 扩展"Keep reading"：openai.com 文章页 Jina 通道的重复头部导航 + 相关文章推荐区块清理
- `_dedup_repeated_paragraphs`：通用"重复段落去重"（响应式页面同一 DOM 元素被多套布局重复渲染），一次覆盖 a16z 侧栏与 arxiv 许可证图标块两类问题，不需要分别写站点专用正则
- `_strip_code_and_links` 补漏：本地图片自定义 scheme（`ainews-media://`）之前没有被排除在 CJK 占比分母之外
- `_review_translation_completeness`（`TranslationCompletenessReview` schema）：机械校验（CJK 占比）不通过且无 HTML/LaTeX 残留时，独立开一次 LLM 调用对照原文核对译文是否真的不完整，减少专有名词/数据密度导致的误杀——机械校验仍是唯一主判据，这一步只在校验已判定失败后触发，不是让翻译模型自证完成

**明确不追的 2 类**：品牌名密度导致的临界失败（旧项目 `news-originalizer.md` 同样没有豁免逻辑）；`state-of-ai-report` 历史 Edition 落地页（`url_index` 显示这类页面摘要恒为空，跨日去重会在第二天自动丢弃，不是持续复发问题）。

**验收**：全新全量批次（清空表、真实走完整 Temporal 流水线）：144 篇，0 enrich_failed，**0 篇最终降级**（原始诊断批次 17/146=11.6%）。复审机制用正负样本各测过一次确认不是橡皮图章；重试上限调整依据是同一原文独立重跑三次结果不一致，证明是模型随机性而非内容结构性问题。

## 追加：arxiv 来源只抓到摘要，未拿到全文（2026-07-06，用户报告后排查修复）

用户报告"arxiv 文章明显有问题"，排查发现：`_fetch_direct` 请求 `arxiv.org/abs/<id>`（论文摘要页）从 M2-M4 接入 arxiv-api 起就从未变过，但这个页面本身只有几百字摘要 + "查看PDF/全文链接/许可"侧边栏文字，从来不含论文全文——真实生产数据验证：全部 83 篇非迁移 arxiv Original 无一例外只有摘要（255-979 字），M4 验收时的"0 enrich_failed"没有把这个问题暴露出来，因为它没有失败，只是内容偏薄。migrate_legacy_vault.py 迁移进来的旧系统 arxiv 内容之所以是全文，是旧系统（另一代码库）走了不同的抓取端点，跟新系统这次的 bug 无关。

**根因**：arXiv 另有全文 HTML 渲染服务（`arxiv.org/html/<id>`），但不保证一定存在——论文提交后渲染有延迟（实测提交 3 天后 88% 可用），复杂 LaTeX/图表也可能永久渲染失败。新系统的抓取代码从未尝试过这个端点。

**修复**（`backend/worker/enrich.py`）：
- `_try_arxiv_fulltext`：`_fetch_direct` 内部优先尝试改写成 `/html/<id>` 抓取，404/失败静默回退到原有的 `/abs/`→Jina→Playwright 兜底链路，不影响非 arxiv 来源
- `_ARXIV_LEADING_H1_RE`/`_clean_arxiv_abs_markdown`：全文页与摘要页抓取内容都自带一份论文标题，与 `documents.title` 字段重复导致"标题渲染两遍"（M6 修过 `aggregate.py` 自己拼标题的同款问题，这次是抓取源内容自带的）；摘要页额外有一段带 4+ 空格缩进的"全文链接/访问论文"侧边栏噪声，缩进会被 markdown 解释成代码块导致图片/标题语法原样显示成文字（真实截图复现过），按已知结构整体清洗
- `_ICON_SRC_PATTERNS` 补充 arxiv 全文页的静态资源路径模式（`/static/base/.../images/`，跟摘要页的 `/static/browse/.../icons/` 不是同一套），避免 arXiv 官方 logo/吉祥物图标被当正文配图误抓
- `_chunk_paragraphs`：单个段落超过 `max_chars` 时硬切——全文版论文常见没有空行分隔的超长参考文献列表/附录，整段塞进一个分块会导致翻译输出被 `max_tokens` 截断触发 `IncompleteOutputException`，真实批次实测过程序崩溃
- `translate_activity` 分块翻译改并发（`ThreadPoolExecutor`，`_CHUNK_TRANSLATE_CONCURRENCY=6`）：全文版论文常见 30-50 个分块，顺序翻译单篇要十几分钟，`workflows.py` 的 `translate_activity`/`fetch_original_activity` 超时相应从 300s/150s 调到 600s/240s；单块翻译异常不再让整篇崩溃，降级为保留原文并在 `fallback_notice` 里显式记录失败分块数

**重跑**：`backend/scripts/refetch_arxiv_originals.py`（新增一次性脚本，核对 arXiv API 源头存在性 → 重抓 → 重翻译 → 重算字数/摘要 → upsert，`arxiv_fulltext_refetched` 标记支持断点续跑）对全部 83 篇重新处理，平均字数从约 600（摘要）提升到 **20,402**（71/83 命中全文，12 篇因论文本身没有 HTML 渲染仍是摘要）。过程中因瞬时网络问题中断过 2 次，断点续跑机制正常工作。

**连带发现并修复的独立问题**：前端 `rehype-sanitize`（全量工程审查新增）不认识 `ainews-media://` 自定义协议，会把本地图片 `src` 整个清空——这是全站性回归（M6 之后所有带本地图片的文档都受影响），不是本次 arxiv 改动引入的。修复：`markdown-render.ts` 把 URL 改写挪到 sanitize 之前。详见 `.claude/memory/known_issues.md`。

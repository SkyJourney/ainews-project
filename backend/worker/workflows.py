"""AInewsPipelineWorkflow：M1 单源端到端管道主体，替换 M0 占位版 HelloWorldWorkflow。"""

import asyncio
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from worker.aggregate import aggregate_activity
    from worker.arxiv_backfill import (
        list_arxiv_fulltext_backfill_candidates_activity,
        refresh_original_document_activity,
    )
    from worker.deep_dive import (
        compute_deep_dive_trends_activity,
        compute_topic_deep_dive_candidates_activity,
        compute_topic_deep_dive_stats_activity,
        generate_deep_dive_activity,
        generate_topic_deep_dive_activity,
    )
    from worker.enrich import (
        check_arxiv_fulltext_activity,
        fetch_original_activity,
        gist_activity,
        metadata_activity,
        needs_translation,
        translate_activity,
        upsert_article_activity,
    )
    # needs_translation/compute_word_count/content_hash/estimate_translate_timeout_seconds
    # 是直接调用（不是 activity），四者都在 enrich.py 里标了 "[Temporal 回放安全]"——
    # 必须保持确定性纯函数，改动前先看那边的说明。
    from worker.enrich import compute_word_count
    from worker.enrich import content_hash as compute_content_hash
    from worker.enrich import estimate_translate_timeout_seconds
    from worker.fetch import (
        fetch_activity,
        list_active_sources_activity,
        preflight_activity,
        record_source_health_activity,
    )
    from worker.filter import filter_activity
    from worker.schemas import DeepDiveParams, Entry, EnrichArticleParams, PipelineParams, TopicDeepDiveParams


@workflow.defn
class EnrichArticleWorkflow:
    """per-article child workflow（04 §2.4）：抓原文 → 翻译判断 → 摘要 → 独立 upsert，独立重试。"""

    @workflow.run
    async def run(self, params: EnrichArticleParams) -> None:
        default_retry = RetryPolicy(maximum_attempts=3)

        fetch_result = await workflow.execute_activity(
            fetch_original_activity,
            params.entry.url,
            # direct(30s，含配图下载) + jina(45s) + playwright(30s) 三级兜底顺序尝试，留够余量；
            # arxiv 来源现在会优先尝试全文 HTML 端点（图多、页面比摘要页大得多），配图下载
            # 数量随之明显增加，150s 在真实全文批次下有触底风险，调到 240s（见 decisions.md
            # "arxiv 只抓到摘要"修复记录）。
            start_to_close_timeout=timedelta(seconds=240),
            retry_policy=default_retry,
        )
        body_md = fetch_result["body_md"]
        fetch_channel = fetch_result["fetch_channel"]
        # .get() 而非直接索引：这个键是 2026-07-08 新加的，如果某次执行在部署新代码
        # 前后跨越（fetch_original_activity 的历史记录来自旧代码，不含这个键），
        # Temporal replay 用新代码重放这段历史时直接索引会抛 KeyError 导致 workflow
        # 永久失败，需要人工 reset 才能恢复。
        arxiv_fulltext_pending = fetch_result.get("arxiv_fulltext_pending")

        translated_title: str | None = None
        translated_body: str | None = None
        translation_fallback_notice: str | None = None
        translation_needed = needs_translation(params.entry.title, body_md)

        if translation_needed:
            # 2026-07-09：固定 1800s 在 131 分块的 arxiv 超大论文上真实撞线过（enrich-85
            # 彻底 enrich_failed，见 decisions.md）——改按分块数量动态估算，分块越多留的
            # 预算越大，具体公式与参数取值见 enrich.py::estimate_translate_timeout_seconds。
            translate_timeout_seconds = estimate_translate_timeout_seconds(body_md)
            translation = await workflow.execute_activity(
                translate_activity,
                args=[params.entry.title, body_md],
                # 分块翻译内部已并发（_CHUNK_TRANSLATE_CONCURRENCY），2026-07-07 起
                # translate_activity 内部按分块完成顺序上报 heartbeat，heartbeat_timeout
                # 判断"真卡死"（90s 无新心跳）与这里的 start_to_close_timeout（总预算
                # 上限）是两层独立保护，前者不随分块数变化。
                start_to_close_timeout=timedelta(seconds=translate_timeout_seconds),
                heartbeat_timeout=timedelta(seconds=90),
                retry_policy=default_retry,
            )
            translated_title = translation["translated_title"]
            translated_body = translation["translated_body_md"]
            translation_fallback_notice = translation["translation_fallback_notice"]

        final_title = translated_title or params.entry.title
        final_body = translated_body or body_md

        gist = await workflow.execute_activity(
            gist_activity,
            args=[final_title, final_body],
            start_to_close_timeout=timedelta(seconds=60),
            retry_policy=default_retry,
        )

        metadata = await workflow.execute_activity(
            metadata_activity,
            args=[final_title, final_body],
            start_to_close_timeout=timedelta(seconds=60),
            retry_policy=default_retry,
        )

        await workflow.execute_activity(
            upsert_article_activity,
            {
                "url": params.entry.url,
                "source_name": params.entry.source_name,
                "batch_id": params.batch_id,
                "fetched_title": params.entry.title,
                "fetched_summary": params.entry.raw_summary,
                "original_text": body_md,
                "translation_needed": translation_needed,
                "translated_title": translated_title,
                "translated_summary": translated_body,
                "gist": gist,
                "content_hash": compute_content_hash(body_md),
                "fetch_channel": fetch_channel,
                "published_at": params.entry.published,
                "entities": metadata["entities"],
                "content_type": metadata["content_type"],
                "novelty_signal": {"keywords": metadata["novelty_keywords"]},
                "word_count": compute_word_count(final_body),
                "translation_fallback_notice": translation_fallback_notice,
                "arxiv_fulltext_pending": arxiv_fulltext_pending,
            },
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=default_retry,
        )


@workflow.defn
class AInewsPipelineWorkflow:
    """全信息源端到端管道：preflight+fetch×N（活跃源 fan-out）→ filter（合并后批量）→
    enrich×M（child workflow fan-out）→ aggregate → write。batch_id 由 Temporal Schedule
    触发时留空，workflow 内部用 workflow.info().start_time 兜底生成（M7 起取代 Celery Beat，
    该时间戳在 workflow 历史里一次写死、replay 不会重新计算，满足确定性约束）；
    活跃源列表从 sources.yaml 读取（03 doc 既定架构）。
    """

    @workflow.run
    async def run(self, params: PipelineParams) -> dict:
        default_retry = RetryPolicy(maximum_attempts=3)
        batch_id = params.batch_id or workflow.info().start_time.strftime("%Y-%m-%d-%H%M")

        active_sources = await workflow.execute_activity(
            list_active_sources_activity,
            start_to_close_timeout=timedelta(seconds=10),
            retry_policy=default_retry,
        )

        # 按源 fan-out：某个源耗尽重试仍失败不影响其余源，只记录健康状态后继续
        # （04 §2.1：连续失败先标 degraded，仍参与后续批次，不是直接拉黑整条流水线）。
        fetch_results = await asyncio.gather(
            *(self._fetch_one_source(name, default_retry) for name in active_sources),
            return_exceptions=True,
        )
        entries = []
        source_failures = 0
        for name, result in zip(active_sources, fetch_results):
            if isinstance(result, BaseException):
                workflow.logger.warning(f"源 {name} fetch 失败（不影响其余源）: {result}")
                source_failures += 1
            else:
                entries.extend(result)

        kept = await workflow.execute_activity(
            filter_activity,
            args=[entries, batch_id],
            start_to_close_timeout=timedelta(seconds=60),
            retry_policy=default_retry,
        )

        # per-article fan-out：每篇文章独立 child workflow，独立重试；某一篇耗尽重试仍失败
        # 不影响其余文章（04 §2.4 设计初衷——不需要人工补跑覆盖率缺口）。
        enrich_results = await asyncio.gather(
            *(
                workflow.execute_child_workflow(
                    EnrichArticleWorkflow.run,
                    EnrichArticleParams(entry=entry, batch_id=batch_id),
                    id=f"{batch_id}-enrich-{i}",
                )
                for i, entry in enumerate(kept)
            ),
            return_exceptions=True,
        )
        enrich_failures = [r for r in enrich_results if isinstance(r, BaseException)]
        for failure in enrich_failures:
            workflow.logger.warning(f"EnrichArticleWorkflow 失败（不影响其余文章）: {failure}")

        aggregate_result = await workflow.execute_activity(
            aggregate_activity,
            batch_id,
            # 2026-07-06 起 aggregate_activity 内部直接调用 write_activity 完成落库
            # （不再是两个独立 activity，见 aggregate.py::aggregate_activity 顶部
            # 说明——records 含全文正文经 gRPC 传递会超过 Temporal 4MB 消息上限）。
            # 超时相应覆盖"聚类+打标 LLM 调用"与"upsert+同步 tags/links"两段耗时：
            # M5 起两次覆盖整批文章的 LLM 调用（聚类+打标）+ 每条记录的写库同步，
            # 真实批次（100+ 篇、documents 表 400+ 条）实测过 360s 不够用，调到 600s。
            start_to_close_timeout=timedelta(seconds=600),
            retry_policy=default_retry,
        )

        return {
            "batch_id": batch_id,
            "sources_attempted": len(active_sources),
            "sources_failed": source_failures,
            "fetched": len(entries),
            "kept": len(kept),
            "enrich_failed": len(enrich_failures),
            "written": aggregate_result["written"],
        }

    @staticmethod
    async def _fetch_one_source(source_name: str, retry_policy: RetryPolicy) -> list:
        """单个源的 preflight+fetch+健康状态记录，供 fan-out 并发调用。

        preflight_activity 返回的 PreflightResult（reliability/stale）目前只在 activity
        内部打日志用于人工告警，这里没有据此做任何跳过/降级决策——是预留的扩展点，不是
        遗漏（见 .claude/memory/known_issues.md）。如果以后要真正"stale 就跳过这个源"，
        需要在这里读返回值再决定是否继续 fetch_activity。
        """
        await workflow.execute_activity(
            preflight_activity,
            source_name,
            start_to_close_timeout=timedelta(seconds=10),
            retry_policy=retry_policy,
        )
        try:
            entries = await workflow.execute_activity(
                fetch_activity,
                source_name,
                # webfetch 分支含一次 LLM 抽取调用，script 分支的 a16z 有 N 次详情页串行请求，
                # 留够余量。
                start_to_close_timeout=timedelta(seconds=180),
                retry_policy=retry_policy,
            )
        except Exception as exc:
            await workflow.execute_activity(
                record_source_health_activity,
                args=[source_name, False, str(exc)],
                start_to_close_timeout=timedelta(seconds=10),
            )
            raise
        await workflow.execute_activity(
            record_source_health_activity,
            args=[source_name, True, None],
            start_to_close_timeout=timedelta(seconds=10),
        )
        return entries


@workflow.defn
class ArxivFulltextBackfillWorkflow:
    """每日独立调度（09:00 Asia/Shanghai，与主流水线同一时间但完全独立的 Temporal
    Schedule，见 worker.py::ensure_arxiv_fulltext_backfill_schedule），跟主流水线的
    fetch/filter/enrich/aggregate 完全解耦。2026-07-08 新增，见
    .claude/memory/decisions.md。

    只更新 documents.original 本身（保留原有 topic_slug/tags/related_zettel_id），
    不碰 Topic/Daily/Digest——这是内容质量回补，不是"今天的新闻"，不应该在
    Topic/Daily 里产生新的当天条目。
    """

    @workflow.run
    async def run(self) -> dict:
        candidates = await workflow.execute_activity(
            list_arxiv_fulltext_backfill_candidates_activity,
            start_to_close_timeout=timedelta(seconds=30),
        )
        if not candidates:
            return {"checked": 0, "ready": 0, "upgraded": 0}

        # 先做一次便宜的检查（单次 HTTP 请求，不含 LLM 调用），筛掉仍然只有摘要的候选，
        # 避免对每天都还没等到全文的文章重复浪费翻译/摘要/元数据这几个 LLM 调用。
        default_retry = RetryPolicy(maximum_attempts=3)
        availability = await asyncio.gather(
            *(
                workflow.execute_activity(
                    check_arxiv_fulltext_activity,
                    c["url"],
                    start_to_close_timeout=timedelta(seconds=30),
                    retry_policy=default_retry,
                )
                for c in candidates
            ),
            return_exceptions=True,
        )
        ready = []
        for c, avail in zip(candidates, availability):
            if isinstance(avail, BaseException):
                # 2026-07-08 修复：此前这里只用 `avail is True` 过滤，check_arxiv_fulltext_
                # activity 耗尽重试后抛出的异常会被无声当成"未就绪"丢弃，日志里完全看不出
                # 是真的还没渲染好还是探测本身在报错，排查方向会找错。
                workflow.logger.warning(f"arxiv 回补：{c['url']} 可用性检查失败（不影响其余候选）: {avail}")
                continue
            if avail is True:
                ready.append(c)
        if not ready:
            return {"checked": len(candidates), "ready": 0, "upgraded": 0}

        batch_id = f"arxiv-fulltext-backfill-{workflow.info().start_time.strftime('%Y-%m-%d')}"
        enrich_results = await asyncio.gather(
            *(
                workflow.execute_child_workflow(
                    EnrichArticleWorkflow.run,
                    EnrichArticleParams(
                        entry=Entry(
                            title=c["fetched_title"],
                            url=c["url"],
                            source_name="arxiv-api",
                            published=c["published_at"],
                            raw_summary="",
                            low_confidence=False,
                            extra={},
                        ),
                        batch_id=batch_id,
                    ),
                    id=f"{batch_id}-{c['doc_id']}",
                )
                for c in ready
            ),
            return_exceptions=True,
        )

        succeeded = []
        for c, result in zip(ready, enrich_results):
            if isinstance(result, BaseException):
                workflow.logger.warning(f"arxiv 回补：{c['url']} 重新 enrich 失败（不影响其余候选）: {result}")
                continue
            succeeded.append(c)

        # 候选之间互不依赖，跟前两段一样一次性并发发起，不要逐个 await 串行等待
        # （2026-07-08 修复：此前是 for 循环里逐条 await，总耗时随候选数线性叠加）。
        refresh_results = await asyncio.gather(
            *(
                workflow.execute_activity(
                    refresh_original_document_activity,
                    c,
                    start_to_close_timeout=timedelta(seconds=30),
                )
                for c in succeeded
            ),
            return_exceptions=True,
        )
        upgraded = 0
        for c, result in zip(succeeded, refresh_results):
            if isinstance(result, BaseException):
                workflow.logger.warning(f"arxiv 回补：{c['url']} 写回正文失败（不影响其余候选）: {result}")
                continue
            upgraded += 1

        return {"checked": len(candidates), "ready": len(ready), "upgraded": upgraded}


@workflow.defn
class DeepDiveWorkflow:
    """每周独立调度（周一 09:00 Asia/Shanghai，见 worker.py::ensure_deep_dive_schedule），
    对过去一周的内容做二次聚合，产出一条 `documents.doc_type='deep_dive'` 记录（M10，
    2026-07-09 新增，见 .claude/memory/decisions.md）。

    比 ArxivFulltextBackfillWorkflow 更彻底的"完全解耦"——只读 original/digest，不改写
    任何既有 Topic/Daily/Digest/Original/Zettel 文档，只新增这一条记录。

    `params.window_end` 可选（2026-07-20 新增，见 decisions.md「周报素材来源...」附近
    的回补记录）：留空时按 Schedule 触发时间兜底计算当周窗口，手动回补历史某一周报告时
    可显式指定，`doc_id=deep-dive-{window_end}` 天然 upsert 覆盖同一条历史记录，不会
    产生重复文档。
    """

    @workflow.run
    async def run(self, params: DeepDiveParams) -> dict:
        default_retry = RetryPolicy(maximum_attempts=3)
        # window_end 可选，留空时用确定性时间源兜底——date.today() 只能留在 activity
        # 内部，沿用 AInewsPipelineWorkflow 用 start_time 保证 replay 确定性的既定模式。
        # 09:00 Asia/Shanghai = 01:00 UTC，.date() 直接取不会跨日翻转。手动回补历史某一
        # 周报告时可显式传入 window_end（仿 PipelineParams.batch_id 的既定模式）。
        window_end = params.window_end or workflow.info().start_time.date() - timedelta(days=1)

        trends = await workflow.execute_activity(
            compute_deep_dive_trends_activity,
            window_end,
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=default_retry,
        )
        # generate_deep_dive_activity 现在给每个热门 topic 先做 1 次子主题聚类 LLM 调用，
        # 再给每个子主题各做 1 次深度分析 LLM 调用（2026-07-20 改版：素材来源从 zettel
        # 反查改为全部原文子主题聚类，与月报同构，见 .claude/memory/decisions.md）——
        # 子主题数由 LLM 聚类结果决定（prompt 要求 3-7 条线索），调用方无法在触发这次
        # activity 前精确预估总调用次数，固定"每 topic 一次分析"的旧公式已经在真实
        # 8-topic 批次上撞过 start_to_close 超时（2026-07-20 真实事故）。改用 activity
        # 内部逐子主题上报心跳（仿 translate_activity 的既定模式）+ 宽裕的 worst-case
        # 兜底超时：每个热门 topic 预留 90s 聚类 + 最多 7 条线索 × 150s 分析，
        # heartbeat_timeout 负责快速发现真卡死，start_to_close_timeout 只是兜底上限，
        # 不需要精确。
        topic_count = len(trends["trending"])
        generate_timeout = timedelta(seconds=300 + topic_count * (90 + 150 * 7))
        return await workflow.execute_activity(
            generate_deep_dive_activity,
            trends,
            start_to_close_timeout=generate_timeout,
            heartbeat_timeout=timedelta(seconds=90),
            retry_policy=default_retry,
        )


@workflow.defn
class TopicDeepDiveWorkflow:
    """专题月报（M11）child workflow：单个达标 topic 的两段 activity（仿 DeepDiveWorkflow
    结构），由 TopicDeepDiveMonthlyWorkflow 按达标 topic 数 fan-out 出来。失败隔离由
    parent 的 asyncio.gather(..., return_exceptions=True) 负责（仿 EnrichArticleWorkflow
    的既定模式），这个 child workflow 本身不需要额外处理。
    """

    @workflow.run
    async def run(self, params: TopicDeepDiveParams) -> dict:
        default_retry = RetryPolicy(maximum_attempts=3)
        # compute_topic_deep_dive_stats_activity 现在还包含 1 次子主题聚类 LLM 调用
        # （2026-07-09 深度改版二），30s 加到 90s 留余量。
        stats = await workflow.execute_activity(
            compute_topic_deep_dive_stats_activity,
            params,
            start_to_close_timeout=timedelta(seconds=90),
            retry_policy=default_retry,
        )
        # generate_topic_deep_dive_activity 现在给每个子主题单独做一次深度分析 LLM
        # 调用（不再是整个 topic 只有 1 次），超时按子主题数动态估算（仿周报
        # DeepDiveWorkflow 按热门话题数动态估算的既定模式），基础 120s 覆盖延续性素材
        # 查询+写库，每个子主题额外估 150s。
        cluster_count = len(stats["clusters"])
        generate_timeout = timedelta(seconds=120 + 150 * max(cluster_count, 1))
        return await workflow.execute_activity(
            generate_topic_deep_dive_activity,
            stats,
            start_to_close_timeout=generate_timeout,
            retry_policy=default_retry,
        )


@workflow.defn
class TopicDeepDiveMonthlyWorkflow:
    """每月独立调度（1 号 09:00 Asia/Shanghai，见 worker.py::ensure_topic_deep_dive_monthly_
    schedule），M10 周报的正交扩展：固定 1 个 topic 桶 × 自然月窗口的纵向深挖（M11，
    2026-07-09 新增，见 .claude/memory/decisions.md）。

    先机械统计上月各 topic 桶是否达标（compute_topic_deep_dive_candidates_activity），
    再对达标 topic 做 child workflow fan-out（仿 EnrichArticleWorkflow 的 per-unit
    fan-out + 失败隔离模式），每个 child 独立生成一条 deep_dive 记录，互不影响。
    """

    @workflow.run
    async def run(self) -> dict:
        default_retry = RetryPolicy(maximum_attempts=3)
        # workflow 代码内必须用确定性时间源（同 DeepDiveWorkflow 的既定模式）。
        # 09:00 Asia/Shanghai = 01:00 UTC，触发日固定是每月 1 号，.date() 直接取不会
        # 跨月翻转。
        today = workflow.info().start_time.date()
        window_end = today.replace(day=1) - timedelta(days=1)  # 上月最后一天
        window_start = window_end.replace(day=1)  # 上月第一天

        candidates = await workflow.execute_activity(
            compute_topic_deep_dive_candidates_activity,
            args=[window_start, window_end],
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=default_retry,
        )

        # per-topic fan-out：仿 AInewsPipelineWorkflow 的 enrich 阶段，某个 topic 生成
        # 失败不影响其余 topic（不需要人工判断该不该重跑整批月报）。
        results = await asyncio.gather(
            *(
                workflow.execute_child_workflow(
                    TopicDeepDiveWorkflow.run,
                    TopicDeepDiveParams(
                        topic_slug=c["slug"], window_start=window_start, window_end=window_end
                    ),
                    id=f"topic-deep-dive-{window_end.isoformat()}-{c['slug']}",
                )
                for c in candidates
            ),
            return_exceptions=True,
        )
        failures = [r for r in results if isinstance(r, BaseException)]
        for failure in failures:
            workflow.logger.warning(f"TopicDeepDiveWorkflow 失败（不影响其余 topic）: {failure}")

        return {
            "window_start": window_start.isoformat(),
            "window_end": window_end.isoformat(),
            "candidate_count": len(candidates),
            "succeeded": len(candidates) - len(failures),
            "failed": len(failures),
        }

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
    from worker.enrich import (
        check_arxiv_fulltext_activity,
        fetch_original_activity,
        gist_activity,
        metadata_activity,
        needs_translation,
        translate_activity,
        upsert_article_activity,
    )
    # needs_translation/compute_word_count/content_hash 是直接调用（不是 activity），
    # 三者都在 enrich.py 里标了 "[Temporal 回放安全]"——必须保持确定性纯函数，改动前
    # 先看那边的说明。
    from worker.enrich import compute_word_count
    from worker.enrich import content_hash as compute_content_hash
    from worker.fetch import (
        fetch_activity,
        list_active_sources_activity,
        preflight_activity,
        record_source_health_activity,
    )
    from worker.filter import filter_activity
    from worker.schemas import Entry, EnrichArticleParams, PipelineParams


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
            translation = await workflow.execute_activity(
                translate_activity,
                args=[params.entry.title, body_md],
                # 分块翻译内部已并发（_CHUNK_TRANSLATE_CONCURRENCY），但 arxiv 全文来源常见
                # 30-50 个分块，300s→600s 都在真实全文批次下踩过超时（见 decisions.md
                # "arxiv 只抓到摘要"修复记录 + 2026-07-07 M7 观察期批次 5 篇超大论文
                # 翻译超时记录）。2026-07-07 起 translate_activity 内部按分块完成顺序上报
                # heartbeat，改用 heartbeat_timeout 判断"真卡死"（90s 无新心跳），
                # 不再需要靠不断调大固定 start_to_close_timeout 硬顶超大论文——硬上限本身
                # 放宽到 1800s 只是兜底，正常不会跑满。
                start_to_close_timeout=timedelta(seconds=1800),
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

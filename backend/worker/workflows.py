"""AInewsPipelineWorkflow：M1 单源端到端管道主体，替换 M0 占位版 HelloWorldWorkflow。"""

import asyncio
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from worker.aggregate import aggregate_activity
    from worker.enrich import (
        fetch_original_activity,
        gist_activity,
        metadata_activity,
        needs_translation,
        translate_activity,
        upsert_article_activity,
    )
    from worker.enrich import compute_word_count
    from worker.enrich import content_hash as compute_content_hash
    from worker.fetch import (
        fetch_activity,
        list_active_sources_activity,
        preflight_activity,
        record_source_health_activity,
    )
    from worker.filter import filter_activity
    from worker.schemas import EnrichArticleParams, PipelineParams
    from worker.write import write_activity


@workflow.defn
class EnrichArticleWorkflow:
    """per-article child workflow（04 §2.4）：抓原文 → 翻译判断 → 摘要 → 独立 upsert，独立重试。"""

    @workflow.run
    async def run(self, params: EnrichArticleParams) -> None:
        default_retry = RetryPolicy(maximum_attempts=3)

        fetch_result = await workflow.execute_activity(
            fetch_original_activity,
            params.entry.url,
            # direct(30s，含配图下载) + jina(45s) + playwright(30s) 三级兜底顺序尝试，留够余量
            start_to_close_timeout=timedelta(seconds=150),
            retry_policy=default_retry,
        )
        body_md = fetch_result["body_md"]
        fetch_channel = fetch_result["fetch_channel"]

        translated_title: str | None = None
        translated_body: str | None = None
        translation_fallback_notice: str | None = None
        translation_needed = needs_translation(params.entry.title, body_md)

        if translation_needed:
            translation = await workflow.execute_activity(
                translate_activity,
                args=[params.entry.title, body_md],
                start_to_close_timeout=timedelta(seconds=300),
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

        records = await workflow.execute_activity(
            aggregate_activity,
            batch_id,
            # M5 起这里含两次覆盖整批文章的 LLM 调用（聚类+打标），30s 是 M1 纯 Python
            # 版本的遗留值，真实批次（100+ 篇）单次调用就可能超过 30s，留够余量。
            start_to_close_timeout=timedelta(seconds=180),
            retry_policy=default_retry,
        )

        written = await workflow.execute_activity(
            write_activity,
            records,
            # M5 起每条记录（original/zettel/topic/daily/digest）都要 upsert+同步
            # tags/links，真实批次记录数比 M1-M4 明显增多，留够余量。
            start_to_close_timeout=timedelta(seconds=90),
            retry_policy=default_retry,
        )

        return {
            "batch_id": batch_id,
            "sources_attempted": len(active_sources),
            "sources_failed": source_failures,
            "fetched": len(entries),
            "kept": len(kept),
            "enrich_failed": len(enrich_failures),
            "written": written,
        }

    @staticmethod
    async def _fetch_one_source(source_name: str, retry_policy: RetryPolicy) -> list:
        """单个源的 preflight+fetch+健康状态记录，供 fan-out 并发调用。"""
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

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
        needs_translation,
        translate_activity,
        upsert_article_activity,
    )
    from worker.enrich import content_hash as compute_content_hash
    from worker.fetch import fetch_activity, preflight_activity
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
            # direct 最长 30s + jina 兜底最长 45s，留够余量
            start_to_close_timeout=timedelta(seconds=90),
            retry_policy=default_retry,
        )
        body_md = fetch_result["body_md"]
        fetch_channel = fetch_result["fetch_channel"]

        translated_title: str | None = None
        translated_body: str | None = None
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

        gist = await workflow.execute_activity(
            gist_activity,
            args=[translated_title or params.entry.title, translated_body or body_md],
            start_to_close_timeout=timedelta(seconds=60),
            retry_policy=default_retry,
        )

        await workflow.execute_activity(
            upsert_article_activity,
            {
                "url": params.entry.url,
                "source_name": params.source_name,
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
            },
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=default_retry,
        )


@workflow.defn
class AInewsPipelineWorkflow:
    """M1 单源端到端管道：preflight → fetch → filter → enrich×M（child workflow fan-out）
    → aggregate → write。source_name/batch_id 由 Celery Beat 触发时生成并传入（04 §2.8）。
    """

    @workflow.run
    async def run(self, params: PipelineParams) -> dict:
        default_retry = RetryPolicy(maximum_attempts=3)

        await workflow.execute_activity(
            preflight_activity,
            params.source_name,
            start_to_close_timeout=timedelta(seconds=10),
            retry_policy=default_retry,
        )

        entries = await workflow.execute_activity(
            fetch_activity,
            params.source_name,
            start_to_close_timeout=timedelta(seconds=60),
            retry_policy=default_retry,
        )

        kept = await workflow.execute_activity(
            filter_activity,
            entries,
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=default_retry,
        )

        # per-article fan-out：每篇文章独立 child workflow，独立重试；某一篇耗尽重试仍失败
        # 不影响其余文章（04 §2.4 设计初衷——不需要人工补跑覆盖率缺口）。
        enrich_results = await asyncio.gather(
            *(
                workflow.execute_child_workflow(
                    EnrichArticleWorkflow.run,
                    EnrichArticleParams(entry=entry, source_name=params.source_name, batch_id=params.batch_id),
                    id=f"{params.batch_id}-enrich-{i}",
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
            params.batch_id,
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=default_retry,
        )

        written = await workflow.execute_activity(
            write_activity,
            records,
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=default_retry,
        )

        return {
            "batch_id": params.batch_id,
            "fetched": len(entries),
            "kept": len(kept),
            "enrich_failed": len(enrich_failures),
            "written": written,
        }

"""Temporal worker 启动入口：连接 Temporal Server，注册 workflow/activity，常驻监听 task queue。

所有 activity 都是同步函数（httpx/trafilatura/SQLAlchemy/openai SDK 均为阻塞调用），
按 Temporal Python SDK 的约定用 ThreadPoolExecutor 承载（同步 activity 必须显式提供
activity_executor，否则报错）。workflow 与 activity 之间会传递 pydantic 模型
（Entry/EnrichArticleParams/PipelineParams 等），需要 pydantic_data_converter。
"""

import asyncio
import logging
import os
from concurrent.futures import ThreadPoolExecutor

from temporalio.client import (
    Client,
    Schedule,
    ScheduleActionStartWorkflow,
    ScheduleAlreadyRunningError,
    ScheduleSpec,
)
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.worker import Worker

from worker.aggregate import aggregate_activity
from worker.arxiv_backfill import (
    list_arxiv_fulltext_backfill_candidates_activity,
    refresh_original_document_activity,
)
from worker.deep_dive import compute_deep_dive_trends_activity, generate_deep_dive_activity
from worker.enrich import (
    check_arxiv_fulltext_activity,
    fetch_original_activity,
    gist_activity,
    metadata_activity,
    translate_activity,
    upsert_article_activity,
)
from worker.fetch import (
    fetch_activity,
    list_active_sources_activity,
    preflight_activity,
    record_source_health_activity,
)
from worker.filter import filter_activity
from worker.schemas import PipelineParams
from worker.workflows import (
    AInewsPipelineWorkflow,
    ArxivFulltextBackfillWorkflow,
    DeepDiveWorkflow,
    EnrichArticleWorkflow,
)

# write_activity（worker/write.py）从 2026-07-06 起不再单独注册为 Temporal activity——
# aggregate_activity 内部直接调用它（普通函数调用，见 aggregate.py 顶部说明），不需要
# workflow 再单独调度一次。

TEMPORAL_HOST = os.environ.get("TEMPORAL_HOST", "localhost:7233")
TASK_QUEUE = "ainews-task-queue"
MAX_ACTIVITY_WORKERS = 20
PIPELINE_SCHEDULE_ID = "ainews-pipeline-daily"
ARXIV_BACKFILL_SCHEDULE_ID = "ainews-arxiv-fulltext-backfill-daily"
DEEP_DIVE_SCHEDULE_ID = "ainews-deep-dive-weekly"


async def ensure_pipeline_schedule(client: Client) -> None:
    """幂等确保每日调度存在：不存在则创建，已存在则跳过（M7 起用 Temporal 原生 Schedule
    取代 Celery Beat，04 §2.8）。cron 沿用原 Celery Beat 的 09:00 Asia/Shanghai；workflow id
    只是模板，Temporal Server 会在每次实际触发时自动加时间戳后缀，不会跨次冲突。"""
    try:
        await client.create_schedule(
            PIPELINE_SCHEDULE_ID,
            Schedule(
                action=ScheduleActionStartWorkflow(
                    AInewsPipelineWorkflow.run,
                    PipelineParams(),
                    id=f"{PIPELINE_SCHEDULE_ID}-run",
                    task_queue=TASK_QUEUE,
                ),
                spec=ScheduleSpec(
                    cron_expressions=["0 9 * * *"],
                    time_zone_name="Asia/Shanghai",
                ),
            ),
        )
    except ScheduleAlreadyRunningError:
        pass


async def ensure_arxiv_fulltext_backfill_schedule(client: Client) -> None:
    """幂等确保 arxiv 全文回补的每日调度存在（2026-07-08 新增，同 09:00 Asia/Shanghai，
    但是完全独立的 workflow/schedule，不影响主流水线，见 .claude/memory/decisions.md）。"""
    try:
        await client.create_schedule(
            ARXIV_BACKFILL_SCHEDULE_ID,
            Schedule(
                action=ScheduleActionStartWorkflow(
                    ArxivFulltextBackfillWorkflow.run,
                    id=f"{ARXIV_BACKFILL_SCHEDULE_ID}-run",
                    task_queue=TASK_QUEUE,
                ),
                spec=ScheduleSpec(
                    cron_expressions=["0 9 * * *"],
                    time_zone_name="Asia/Shanghai",
                ),
            ),
        )
    except ScheduleAlreadyRunningError:
        pass


async def ensure_deep_dive_schedule(client: Client) -> None:
    """幂等确保 Deep Dive 周报的每周调度存在（M10，2026-07-09 新增：完全独立的 workflow/
    schedule，只读 original/digest、只新增一条 deep_dive 记录，不碰主流水线或 arxiv 回补，
    见 .claude/memory/decisions.md）。cron 定在每周一 09:00 Asia/Shanghai，跟另外两个
    Schedule 同一触发时间点但互不阻塞。"""
    try:
        await client.create_schedule(
            DEEP_DIVE_SCHEDULE_ID,
            Schedule(
                action=ScheduleActionStartWorkflow(
                    DeepDiveWorkflow.run,
                    id=f"{DEEP_DIVE_SCHEDULE_ID}-run",
                    task_queue=TASK_QUEUE,
                ),
                spec=ScheduleSpec(
                    cron_expressions=["0 9 * * 1"],
                    time_zone_name="Asia/Shanghai",
                ),
            ),
        )
    except ScheduleAlreadyRunningError:
        pass


async def main() -> None:
    # 此前完全没有配置 logging，activity.logger/workflow.logger 的调用（含 enrich.py 的
    # [chunk_diag] 诊断日志）和 Temporal SDK 自身的内部日志都被静默丢弃，`docker logs`
    # 看不到任何应用层信息，排查真实批次问题时完全没有可用线索（2026-07-06 排查
    # aggregate_activity 反复超时时发现这个缺口）。
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    client = await Client.connect(TEMPORAL_HOST, data_converter=pydantic_data_converter)
    await ensure_pipeline_schedule(client)
    await ensure_arxiv_fulltext_backfill_schedule(client)
    await ensure_deep_dive_schedule(client)
    worker = Worker(
        client,
        task_queue=TASK_QUEUE,
        workflows=[
            AInewsPipelineWorkflow,
            EnrichArticleWorkflow,
            ArxivFulltextBackfillWorkflow,
            DeepDiveWorkflow,
        ],
        activities=[
            preflight_activity,
            fetch_activity,
            list_active_sources_activity,
            record_source_health_activity,
            filter_activity,
            fetch_original_activity,
            translate_activity,
            gist_activity,
            metadata_activity,
            upsert_article_activity,
            aggregate_activity,
            check_arxiv_fulltext_activity,
            list_arxiv_fulltext_backfill_candidates_activity,
            refresh_original_document_activity,
            compute_deep_dive_trends_activity,
            generate_deep_dive_activity,
        ],
        activity_executor=ThreadPoolExecutor(max_workers=MAX_ACTIVITY_WORKERS),
        max_concurrent_activities=MAX_ACTIVITY_WORKERS,
    )
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())

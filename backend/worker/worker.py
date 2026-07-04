"""Temporal worker 启动入口：连接 Temporal Server，注册 workflow/activity，常驻监听 task queue。

所有 activity 都是同步函数（httpx/trafilatura/SQLAlchemy/openai SDK 均为阻塞调用），
按 Temporal Python SDK 的约定用 ThreadPoolExecutor 承载（同步 activity 必须显式提供
activity_executor，否则报错）。workflow 与 activity 之间会传递 pydantic 模型
（Entry/EnrichArticleParams/PipelineParams 等），需要 pydantic_data_converter。
"""

import asyncio
import os
from concurrent.futures import ThreadPoolExecutor

from temporalio.client import Client
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.worker import Worker

from worker.aggregate import aggregate_activity
from worker.enrich import (
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
from worker.workflows import AInewsPipelineWorkflow, EnrichArticleWorkflow
from worker.write import write_activity

TEMPORAL_HOST = os.environ.get("TEMPORAL_HOST", "localhost:7233")
TASK_QUEUE = "ainews-task-queue"
MAX_ACTIVITY_WORKERS = 20


async def main() -> None:
    client = await Client.connect(TEMPORAL_HOST, data_converter=pydantic_data_converter)
    worker = Worker(
        client,
        task_queue=TASK_QUEUE,
        workflows=[AInewsPipelineWorkflow, EnrichArticleWorkflow],
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
            write_activity,
        ],
        activity_executor=ThreadPoolExecutor(max_workers=MAX_ACTIVITY_WORKERS),
        max_concurrent_activities=MAX_ACTIVITY_WORKERS,
    )
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())

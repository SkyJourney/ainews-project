"""Celery task：调用 Temporal Python Client 触发 workflow。
Celery 本身不跑业务逻辑，这里的 task 体只做"发起一次 start_workflow"这一件事。

batch_id 依赖真实时钟，必须在这里（非 workflow 代码）生成——workflow 内部不允许
自己取当前时间（确定性约束），04 §2.8 batch_id 格式沿用 03-architecture-proposal.md
的示例（如 "2026-07-04-0900"）。
"""

import asyncio
import os
from datetime import datetime, timezone

from celery import shared_task
from temporalio.client import Client
from temporalio.contrib.pydantic import pydantic_data_converter

from worker.schemas import PipelineParams
from worker.workflows import AInewsPipelineWorkflow

TEMPORAL_HOST = os.environ.get("TEMPORAL_HOST", "localhost:7233")
TASK_QUEUE = "ainews-task-queue"

# M1 单源：只有 openai-rss；M3 全源接入后改成按 sources.yaml 遍历活跃源触发。
SOURCE_NAME = "openai-rss"


@shared_task(name="beat.tasks.trigger_ainews_pipeline")
def trigger_ainews_pipeline() -> str:
    return asyncio.run(_start_workflow())


async def _start_workflow() -> str:
    batch_id = datetime.now(timezone.utc).strftime("%Y-%m-%d-%H%M")
    client = await Client.connect(TEMPORAL_HOST, data_converter=pydantic_data_converter)
    handle = await client.start_workflow(
        AInewsPipelineWorkflow.run,
        PipelineParams(source_name=SOURCE_NAME, batch_id=batch_id),
        id=f"ainews-pipeline-{batch_id}",
        task_queue=TASK_QUEUE,
    )
    return handle.id

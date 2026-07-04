"""Celery task：调用 Temporal Python Client 触发 workflow。
Celery 本身不跑业务逻辑，这里的 task 体只做"发起一次 start_workflow"这一件事。
"""

import asyncio
import os
import uuid

from celery import shared_task
from temporalio.client import Client

from worker.workflows import HelloWorldWorkflow

TEMPORAL_HOST = os.environ.get("TEMPORAL_HOST", "localhost:7233")
TASK_QUEUE = "ainews-task-queue"


@shared_task(name="beat.tasks.trigger_hello_workflow")
def trigger_hello_workflow() -> str:
    return asyncio.run(_start_workflow())


async def _start_workflow() -> str:
    client = await Client.connect(TEMPORAL_HOST)
    handle = await client.start_workflow(
        HelloWorldWorkflow.run,
        "ainews-service",
        id=f"hello-world-{uuid.uuid4().hex[:8]}",
        task_queue=TASK_QUEUE,
    )
    return handle.id

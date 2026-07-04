"""Temporal worker 启动入口：连接 Temporal Server，注册 workflow/activity，常驻监听 task queue。"""

import asyncio
import os

from temporalio.client import Client
from temporalio.worker import Worker

from worker.activities import say_hello
from worker.workflows import HelloWorldWorkflow

TEMPORAL_HOST = os.environ.get("TEMPORAL_HOST", "localhost:7233")
TASK_QUEUE = "ainews-task-queue"


async def main() -> None:
    client = await Client.connect(TEMPORAL_HOST)
    worker = Worker(
        client,
        task_queue=TASK_QUEUE,
        workflows=[HelloWorldWorkflow],
        activities=[say_hello],
    )
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())

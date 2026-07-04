"""M0 占位 workflow：验证 Temporal Server + Worker 能跑通一次完整执行历史。
真实的 AInewsPipelineWorkflow（preflight → fetch×N → filter → enrich×M → aggregate → write）
从 M1 开始搭建，替换本文件。
"""

from datetime import timedelta

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from worker.activities import say_hello


@workflow.defn
class HelloWorldWorkflow:
    @workflow.run
    async def run(self, name: str) -> str:
        return await workflow.execute_activity(
            say_hello,
            name,
            start_to_close_timeout=timedelta(seconds=10),
        )

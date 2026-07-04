"""M0 占位 activity：只验证 Temporal activity 执行链路本身是通的。
真实的 preflight/fetch/filter/enrich/aggregate/write activity 从 M1 开始逐个替换。
"""

from temporalio import activity


@activity.defn
async def say_hello(name: str) -> str:
    return f"Hello, {name}! (from ainews-service M0 skeleton)"

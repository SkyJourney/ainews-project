"""Celery Beat 薄触发器：只负责按 cron 调用 Temporal client 的 start_workflow，
不在这里跑任何业务逻辑（04-roadmap.md §2.8）。"""

import os

from celery import Celery
from celery.schedules import crontab

REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")

app = Celery("ainews_beat", broker=REDIS_URL, backend=REDIS_URL)
app.conf.timezone = "Asia/Shanghai"

app.conf.beat_schedule = {
    # M0 占位排期：每 5 分钟触发一次 hello-world workflow，只为验证
    # Beat → Worker → Temporal.start_workflow 这条链路是通的。
    # 真实的抓取排期（对应 14 个信息源的调度节奏）从 M1/M3 开始设计，届时替换本条。
    "trigger-hello-workflow": {
        "task": "beat.tasks.trigger_hello_workflow",
        "schedule": crontab(minute="*/5"),
    },
}

import beat.tasks  # noqa: E402,F401  确保 task 被注册

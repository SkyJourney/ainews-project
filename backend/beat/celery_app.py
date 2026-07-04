"""Celery Beat 薄触发器：只负责按 cron 调用 Temporal client 的 start_workflow，
不在这里跑任何业务逻辑（04-roadmap.md §2.8）。"""

import os

from celery import Celery
from celery.schedules import crontab

REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")

app = Celery("ainews_beat", broker=REDIS_URL, backend=REDIS_URL)
app.conf.timezone = "Asia/Shanghai"

app.conf.beat_schedule = {
    # M1：单源（openai-rss）每日跑一次，对应 03-architecture-proposal.md 的排期设计。
    # M3 全源接入后按需拆分成多条排期或在 workflow 内部对活跃源做 fan-out。
    "trigger-ainews-pipeline": {
        "task": "beat.tasks.trigger_ainews_pipeline",
        "schedule": crontab(hour=9, minute=0),
    },
}

import beat.tasks  # noqa: E402,F401  确保 task 被注册

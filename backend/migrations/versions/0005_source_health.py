"""source_health: 信息源健康检查状态机（04 §2.1/§3）

sources.yaml 的 reliability 字段只是"初始/种子值"，运行时状态（连续失败次数、
当前实际 reliability）必须落 Postgres 才能跨批次持久——静态配置文件不能被
运行中的进程运行时改写。preflight_activity 读这张表判断是否降级；
fetch_activity 结束后由 workflow 记录本次成功/失败。

Revision ID: 0005
Revises: 0004
Create Date: 2026-07-04

"""
from typing import Sequence, Union

from alembic import op

revision: str = "0005"
down_revision: Union[str, None] = "0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE source_health (
          source_name TEXT PRIMARY KEY,
          reliability TEXT NOT NULL,
          consecutive_failures INT NOT NULL DEFAULT 0,
          last_success_at TIMESTAMPTZ,
          last_failure_at TIMESTAMPTZ,
          last_failure_reason TEXT
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS source_health CASCADE")

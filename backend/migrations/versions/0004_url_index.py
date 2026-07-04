"""url_index: 跨日去重 30 天滚动窗口索引（04 §2.3）

旧系统用 JSON 文件维护这个索引，新架构下必须落 Postgres（唯一权威存储）。
字段与访问权限约束见 04 §2.3：仅 filter_activity（读写全部字段）和
write_activity（仅回填 zettel_id）能碰这张表，其余阶段严禁写入——
这条约束在代码层面通过 backend/worker/db.py 里两组不同函数的 API 边界体现
（filter_* 系列 vs write_backfill_zettel_id），不依赖数据库权限系统。

Revision ID: 0004
Revises: 0003
Create Date: 2026-07-04

"""
from typing import Sequence, Union

from alembic import op

revision: str = "0004"
down_revision: Union[str, None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE url_index (
          normalized_url TEXT PRIMARY KEY,
          first_seen_date DATE NOT NULL,
          first_seen_run TEXT NOT NULL,
          title TEXT,
          source_name TEXT,
          kept_in_daily JSONB NOT NULL DEFAULT '[]',
          zettel_id TEXT,
          raw_summary_excerpt TEXT
        )
        """
    )
    op.execute("CREATE INDEX url_index_first_seen_date_idx ON url_index (first_seen_date)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS url_index CASCADE")

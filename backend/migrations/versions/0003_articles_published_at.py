"""articles: add published_at

03-architecture-proposal.md 原始 schema 设计漏掉了这个字段——fetch_activity 产出的
Entry.published（04 §2.2）从未落进 articles 表，导致 aggregate/write 阶段无法得知
文章的发布日期。M1 write_activity 需要它填 documents.doc_date，这里补上。

Revision ID: 0003
Revises: 0002
Create Date: 2026-07-04

"""
from typing import Sequence, Union

from alembic import op

revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE articles ADD COLUMN published_at DATE")


def downgrade() -> None:
    op.execute("ALTER TABLE articles DROP COLUMN IF EXISTS published_at")

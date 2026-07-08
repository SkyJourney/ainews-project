"""articles: add arxiv_fulltext_pending

新增一列标记"这篇 arxiv 文章当前只抓到了摘要页，全文 HTML 还没渲染出来"
（NULL 表示非 arxiv 来源，不适用这个概念）。仅 arxiv 相关来源的文章会写入
true/false，供新增的每日 arxiv 全文回补 workflow（`worker/arxiv_backfill.py`）
查询候选：只回补 pending=true 且发布时间在 14 天窗口内的文章，超窗放弃重试。

Revision ID: 0007
Revises: 0006
Create Date: 2026-07-08

"""
from typing import Sequence, Union

from alembic import op

revision: str = "0007"
down_revision: Union[str, None] = "0006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE articles ADD COLUMN arxiv_fulltext_pending BOOLEAN")


def downgrade() -> None:
    op.execute("ALTER TABLE articles DROP COLUMN IF EXISTS arxiv_fulltext_pending")

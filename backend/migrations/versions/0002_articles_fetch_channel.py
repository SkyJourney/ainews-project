"""articles: add fetch_channel

M1 enrich 阶段实测发现部分源的原文页面挂 Cloudflare 等反爬挑战（如 openai.com 的
/index/* 文章页返回 403 cf-mitigated: challenge），直连必然失败，需要 Jina Reader
兜底通道（04 §2.4 状态②）。参考旧项目 fetch-with-assets.py 已验证的设计：新增
fetch_channel 列区分 'direct'/'jina'，供 aggregate_activity 据此回填 documents
的 fallback_notice 字段（04 §2.6：null=正常/字符串=降级原因）。

Revision ID: 0002
Revises: 0001
Create Date: 2026-07-04

"""
from typing import Sequence, Union

from alembic import op

revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE articles ADD COLUMN fetch_channel TEXT")


def downgrade() -> None:
    op.execute("ALTER TABLE articles DROP COLUMN IF EXISTS fetch_channel")

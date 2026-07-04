"""articles: add word_count, translation_fallback_notice

M4 Enrich 完整化（04 §2.4）新增两列：
- word_count：机械计算的正文字数（硬约束：不能靠 LLM 自估）
- translation_fallback_notice：翻译完整性机械校验未通过时的降级说明（与
  fetch_channel 对应的抓取降级说明是两个独立信号，aggregate 阶段组装
  documents.frontmatter 的 fallback_notice 时需要合并两者）

Revision ID: 0006
Revises: 0005
Create Date: 2026-07-04

"""
from typing import Sequence, Union

from alembic import op

revision: str = "0006"
down_revision: Union[str, None] = "0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE articles ADD COLUMN word_count INT")
    op.execute("ALTER TABLE articles ADD COLUMN translation_fallback_notice TEXT")


def downgrade() -> None:
    op.execute("ALTER TABLE articles DROP COLUMN IF EXISTS word_count")
    op.execute("ALTER TABLE articles DROP COLUMN IF EXISTS translation_fallback_notice")

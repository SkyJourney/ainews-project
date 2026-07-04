"""initial schema: articles / documents / links / tags

对应 docs/03-architecture-proposal.md §3 数据模型草案。用 op.execute() 原样落 DDL，
不用 SQLAlchemy ORM 抽象转译——GENERATED ALWAYS AS ... STORED / GIN 索引这些 Postgres
特性用 ORM Column 表达容易失真，直接写 SQL 更贴近文档原文、更好核对。

articles.embedding（VECTOR(1536)）本迁移不建：03 §3 原文注明"未启用 pgvector 前此列不建"，
pgvector 扩展要等 M9 才评估是否启用。

Revision ID: 0001
Revises:
Create Date: 2026-07-04

"""
from typing import Sequence, Union

from alembic import op

revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE articles (
          url TEXT PRIMARY KEY,
          source_name TEXT NOT NULL,
          batch_id TEXT NOT NULL,
          fetched_title TEXT,
          fetched_summary TEXT,
          status TEXT NOT NULL DEFAULT 'pending',
          original_text TEXT,
          translation_needed BOOLEAN,
          translated_title TEXT,
          translated_summary TEXT,
          gist TEXT,
          entities JSONB,
          content_type TEXT,
          novelty_signal JSONB,
          content_hash TEXT,
          enriched_at TIMESTAMPTZ
        )
        """
    )
    op.execute("CREATE INDEX articles_batch_status_idx ON articles (batch_id, status)")

    op.execute(
        """
        CREATE TABLE documents (
          id TEXT PRIMARY KEY,
          doc_type TEXT NOT NULL,
          title TEXT,
          doc_date DATE,
          frontmatter JSONB NOT NULL,
          body_md TEXT NOT NULL,
          body_tsv TSVECTOR
            GENERATED ALWAYS AS (to_tsvector('simple', coalesce(title, '') || ' ' || body_md)) STORED,
          content_hash TEXT NOT NULL,
          updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX documents_frontmatter_gin ON documents USING GIN (frontmatter)")
    op.execute("CREATE INDEX documents_body_tsv_gin ON documents USING GIN (body_tsv)")
    op.execute("CREATE INDEX documents_doc_type_idx ON documents (doc_type, doc_date DESC)")

    op.execute(
        """
        CREATE TABLE links (
          from_id TEXT REFERENCES documents(id) ON DELETE CASCADE,
          to_id TEXT REFERENCES documents(id) ON DELETE CASCADE,
          PRIMARY KEY (from_id, to_id)
        )
        """
    )

    op.execute(
        """
        CREATE TABLE tags (
          doc_id TEXT REFERENCES documents(id) ON DELETE CASCADE,
          tag TEXT NOT NULL,
          PRIMARY KEY (doc_id, tag)
        )
        """
    )
    op.execute("CREATE INDEX tags_tag_idx ON tags (tag)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS tags CASCADE")
    op.execute("DROP TABLE IF EXISTS links CASCADE")
    op.execute("DROP TABLE IF EXISTS documents CASCADE")
    op.execute("DROP TABLE IF EXISTS articles CASCADE")

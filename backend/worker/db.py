"""Postgres 访问：SQLAlchemy engine + text() 参数化 SQL，不建 ORM 模型层。

延续 migrations/versions/0001_initial_schema.py 的风格——DDL/DML 都贴近原始 SQL，
不引入 ORM Column/declarative 抽象转译，核对着 03-architecture-proposal.md §3 的表结构写。
"""

from __future__ import annotations

import json
import os
from datetime import date
from functools import lru_cache

from sqlalchemy import Engine, create_engine, text


@lru_cache(maxsize=1)
def get_engine() -> Engine:
    return create_engine(os.environ["DATABASE_URL"], pool_pre_ping=True)


def upsert_enriched_article(
    *,
    url: str,
    source_name: str,
    batch_id: str,
    fetched_title: str,
    fetched_summary: str,
    original_text: str,
    translation_needed: bool,
    translated_title: str | None,
    translated_summary: str | None,
    gist: str,
    content_hash: str,
    fetch_channel: str,
    published_at: date | None,
) -> None:
    """enrich 阶段完成后一次性 upsert 整条记录，status 固定写 'enriched'。

    articles.url 是天然去重键（03 §3），跨批次重复抓到同一 URL 时直接覆盖为最新一次结果。
    fetch_channel（'direct'/'jina'）供 aggregate 阶段回填 documents.frontmatter 的
    fallback_notice 字段（04 §2.6）。
    """
    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO articles (
                    url, source_name, batch_id, fetched_title, fetched_summary,
                    status, original_text, translation_needed, translated_title,
                    translated_summary, gist, content_hash, fetch_channel, published_at,
                    enriched_at
                ) VALUES (
                    :url, :source_name, :batch_id, :fetched_title, :fetched_summary,
                    'enriched', :original_text, :translation_needed, :translated_title,
                    :translated_summary, :gist, :content_hash, :fetch_channel, :published_at,
                    now()
                )
                ON CONFLICT (url) DO UPDATE SET
                    source_name = EXCLUDED.source_name,
                    batch_id = EXCLUDED.batch_id,
                    fetched_title = EXCLUDED.fetched_title,
                    fetched_summary = EXCLUDED.fetched_summary,
                    status = 'enriched',
                    original_text = EXCLUDED.original_text,
                    translation_needed = EXCLUDED.translation_needed,
                    translated_title = EXCLUDED.translated_title,
                    translated_summary = EXCLUDED.translated_summary,
                    gist = EXCLUDED.gist,
                    content_hash = EXCLUDED.content_hash,
                    fetch_channel = EXCLUDED.fetch_channel,
                    published_at = EXCLUDED.published_at,
                    enriched_at = now()
                """
            ),
            {
                "url": url,
                "source_name": source_name,
                "batch_id": batch_id,
                "fetched_title": fetched_title,
                "fetched_summary": fetched_summary,
                "original_text": original_text,
                "translation_needed": translation_needed,
                "translated_title": translated_title,
                "translated_summary": translated_summary,
                "gist": gist,
                "content_hash": content_hash,
                "fetch_channel": fetch_channel,
                "published_at": published_at,
            },
        )


def fetch_enriched_articles(batch_id: str) -> list[dict]:
    """aggregate_activity 的入口查询：取本批次全部已完成富化的文章（03 §3 注释里给的样例查询）。"""
    engine = get_engine()
    with engine.begin() as conn:
        rows = conn.execute(
            text("SELECT * FROM articles WHERE batch_id = :batch_id AND status = 'enriched'"),
            {"batch_id": batch_id},
        ).mappings().all()
    return [dict(row) for row in rows]


def document_id_exists(doc_id: str) -> bool:
    """给 write 阶段的 ID 冲突顺延逻辑（04 §2.6：同 HHMM 冲突顺延）用。"""
    engine = get_engine()
    with engine.begin() as conn:
        row = conn.execute(text("SELECT 1 FROM documents WHERE id = :id"), {"id": doc_id}).first()
    return row is not None


def upsert_zettel_document(
    *,
    doc_id: str,
    title: str,
    doc_date: date,
    frontmatter: dict,
    body_md: str,
    content_hash: str,
) -> None:
    """write_activity：直接 upsert 进 documents 表，doc_type 固定 'zettel'（M1 scope）。"""
    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO documents (id, doc_type, title, doc_date, frontmatter, body_md, content_hash, updated_at)
                VALUES (:id, 'zettel', :title, :doc_date, CAST(:frontmatter AS JSONB), :body_md, :content_hash, now())
                ON CONFLICT (id) DO UPDATE SET
                    title = EXCLUDED.title,
                    doc_date = EXCLUDED.doc_date,
                    frontmatter = EXCLUDED.frontmatter,
                    body_md = EXCLUDED.body_md,
                    content_hash = EXCLUDED.content_hash,
                    updated_at = now()
                """
            ),
            {
                "id": doc_id,
                "title": title,
                "doc_date": doc_date,
                "frontmatter": json.dumps(frontmatter, ensure_ascii=False, default=str),
                "body_md": body_md,
                "content_hash": content_hash,
            },
        )

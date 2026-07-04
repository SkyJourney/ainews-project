"""Postgres 访问：SQLAlchemy engine + text() 参数化 SQL，不建 ORM 模型层。

延续 migrations/versions/0001_initial_schema.py 的风格——DDL/DML 都贴近原始 SQL，
不引入 ORM Column/declarative 抽象转译，核对着 03-architecture-proposal.md §3 的表结构写。

`url_index` 表的访问权限约束（04 §2.3：仅 filter_activity 全权限读写，write_activity
仅能回填 zettel_id）通过函数命名前缀体现——`filter_*` 系列只应被 worker/filter.py 调用，
`write_backfill_zettel_id` 只应被 worker/write.py 调用，不做数据库层面的权限控制。
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


# 源健康状态机阈值（04 §2.1：先标 degraded，再连续失败才考虑 dead）
DEGRADED_AFTER_FAILURES = 1
DEAD_AFTER_FAILURES = 3


def get_source_reliability(source_name: str) -> str | None:
    """返回 source_health 表里记录的当前 reliability；None 表示这个源还没有运行时记录
    （preflight_activity 据此决定是否用 sources.yaml 的静态值做初始化）。
    """
    engine = get_engine()
    with engine.begin() as conn:
        row = conn.execute(
            text("SELECT reliability FROM source_health WHERE source_name = :name"),
            {"name": source_name},
        ).first()
    return row[0] if row else None


def init_source_health(source_name: str, initial_reliability: str) -> None:
    """首次见到这个源：用 sources.yaml 的静态值播种一行运行时状态。"""
    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO source_health (source_name, reliability, consecutive_failures)
                VALUES (:name, :reliability, 0)
                ON CONFLICT (source_name) DO NOTHING
                """
            ),
            {"name": source_name, "reliability": initial_reliability},
        )


def record_fetch_success(source_name: str) -> None:
    """fetch 成功：连续失败计数清零，reliability 恢复 alive。"""
    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                UPDATE source_health
                SET consecutive_failures = 0, reliability = 'alive', last_success_at = now()
                WHERE source_name = :name
                """
            ),
            {"name": source_name},
        )


def record_fetch_failure(source_name: str, reason: str) -> None:
    """fetch 失败：连续失败计数 +1，按阈值升级 reliability（04 §2.1 状态机）。"""
    engine = get_engine()
    with engine.begin() as conn:
        row = conn.execute(
            text("SELECT consecutive_failures FROM source_health WHERE source_name = :name"),
            {"name": source_name},
        ).first()
        failures = (row[0] if row else 0) + 1
        reliability = "dead" if failures >= DEAD_AFTER_FAILURES else "degraded" if failures >= DEGRADED_AFTER_FAILURES else "alive"
        conn.execute(
            text(
                """
                UPDATE source_health
                SET consecutive_failures = :failures, reliability = :reliability,
                    last_failure_at = now(), last_failure_reason = :reason
                WHERE source_name = :name
                """
            ),
            {"name": source_name, "failures": failures, "reliability": reliability, "reason": reason},
        )


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
    entities: list | None = None,
    content_type: str | None = None,
    novelty_signal: dict | None = None,
    word_count: int | None = None,
    translation_fallback_notice: str | None = None,
) -> None:
    """enrich 阶段完成后一次性 upsert 整条记录，status 固定写 'enriched'。

    articles.url 是天然去重键（03 §3），跨批次重复抓到同一 URL 时直接覆盖为最新一次结果。
    fetch_channel（'direct'/'jina'/'playwright'/'placeholder'）+ translation_fallback_notice
    供 aggregate 阶段合并组装 documents.frontmatter 的 fallback_notice 字段（04 §2.6）。
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
                    entities, content_type, novelty_signal, word_count, translation_fallback_notice,
                    enriched_at
                ) VALUES (
                    :url, :source_name, :batch_id, :fetched_title, :fetched_summary,
                    'enriched', :original_text, :translation_needed, :translated_title,
                    :translated_summary, :gist, :content_hash, :fetch_channel, :published_at,
                    CAST(:entities AS JSONB), :content_type, CAST(:novelty_signal AS JSONB),
                    :word_count, :translation_fallback_notice,
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
                    entities = EXCLUDED.entities,
                    content_type = EXCLUDED.content_type,
                    novelty_signal = EXCLUDED.novelty_signal,
                    word_count = EXCLUDED.word_count,
                    translation_fallback_notice = EXCLUDED.translation_fallback_notice,
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
                "entities": json.dumps(entities if entities is not None else []),
                "content_type": content_type,
                "novelty_signal": json.dumps(novelty_signal if novelty_signal is not None else {}),
                "word_count": word_count,
                "translation_fallback_notice": translation_fallback_notice,
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


def filter_lookup_url_index(normalized_url: str) -> dict | None:
    """filter_activity 专用：查询归一化 URL 是否已在跨日索引里（04 §2.3）。"""
    engine = get_engine()
    with engine.begin() as conn:
        row = conn.execute(
            text("SELECT * FROM url_index WHERE normalized_url = :url"),
            {"url": normalized_url},
        ).mappings().first()
    return dict(row) if row else None


def filter_upsert_url_index(
    *,
    normalized_url: str,
    first_seen_date: date,
    first_seen_run: str,
    title: str,
    source_name: str,
    raw_summary_excerpt: str,
    batch_id_to_append: str,
) -> None:
    """filter_activity 专用：新 URL 插入索引，已存在则追加 kept_in_daily（04 §2.3：
    仅 filter_activity 对这张表有全字段读写权限，见模块顶部说明）。
    """
    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO url_index (
                    normalized_url, first_seen_date, first_seen_run, title,
                    source_name, kept_in_daily, raw_summary_excerpt
                ) VALUES (
                    :normalized_url, :first_seen_date, :first_seen_run, :title,
                    :source_name, CAST(:batch_json AS JSONB), :raw_summary_excerpt
                )
                ON CONFLICT (normalized_url) DO UPDATE SET
                    kept_in_daily = url_index.kept_in_daily || CAST(:batch_json AS JSONB)
                """
            ),
            {
                "normalized_url": normalized_url,
                "first_seen_date": first_seen_date,
                "first_seen_run": first_seen_run,
                "title": title,
                "source_name": source_name,
                "raw_summary_excerpt": raw_summary_excerpt,
                "batch_json": json.dumps([batch_id_to_append]),
            },
        )


def filter_cleanup_url_index(cutoff_date: date) -> int:
    """filter_activity 专用：清理 first_seen_date 超过 30 天的旧条目（04 §2.3）。"""
    engine = get_engine()
    with engine.begin() as conn:
        result = conn.execute(
            text("DELETE FROM url_index WHERE first_seen_date < :cutoff"),
            {"cutoff": cutoff_date},
        )
        return result.rowcount


def write_backfill_zettel_id(normalized_url: str, zettel_id: str) -> None:
    """write_activity 专用：对 url_index 唯一允许的写操作——只回填 zettel_id 这一列
    （04 §2.3：Write 阶段严禁碰这张表的其他字段）。
    """
    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE url_index SET zettel_id = :zettel_id WHERE normalized_url = :url"),
            {"zettel_id": zettel_id, "url": normalized_url},
        )


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

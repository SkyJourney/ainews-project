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
    arxiv_fulltext_pending: bool | None = None,
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
                    arxiv_fulltext_pending, enriched_at
                ) VALUES (
                    :url, :source_name, :batch_id, :fetched_title, :fetched_summary,
                    'enriched', :original_text, :translation_needed, :translated_title,
                    :translated_summary, :gist, :content_hash, :fetch_channel, :published_at,
                    CAST(:entities AS JSONB), :content_type, CAST(:novelty_signal AS JSONB),
                    :word_count, :translation_fallback_notice,
                    :arxiv_fulltext_pending, now()
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
                    arxiv_fulltext_pending = EXCLUDED.arxiv_fulltext_pending,
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
                "arxiv_fulltext_pending": arxiv_fulltext_pending,
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


def fetch_enriched_article_by_url(url: str) -> dict | None:
    """`worker/arxiv_backfill.py` 专用：回补 workflow 里子 workflow 重新 enrich 完成后，
    读回这一条最新结果（不按 batch_id 查，因为回补批次不参与常规聚合）。"""
    engine = get_engine()
    with engine.begin() as conn:
        row = conn.execute(
            text("SELECT * FROM articles WHERE url = :url AND status = 'enriched'"), {"url": url}
        ).mappings().first()
    return dict(row) if row else None


def list_arxiv_fulltext_backfill_candidates(cutoff_date: date) -> list[dict]:
    """`worker/arxiv_backfill.py` 专用：查询"当前只抓到摘要页、全文还没渲染出来"且发布
    时间在 `cutoff_date` 之后（14 天回补窗口内）的 arxiv 原文文档，附带回补时需要保持
    不变的 doc_date/topic_slug/tags/related_zettel_id，以及重新 enrich 需要的原始（未
    翻译）标题。doc_date 必须原样带回去覆盖 build_original_record 的重新计算结果——
    如果重新 enrich 时 articles.published_at 恰好是 None（理论上可能，见 fetch.py 里
    对畸形/缺失发布时间的兜底逻辑），build_original_record 会用 date.today() 兜底，
    回补当天执行会把文档的 doc_date 悄悄改到回补当天，不是文档真实的发布日期。
    """
    engine = get_engine()
    with engine.begin() as conn:
        rows = conn.execute(
            text(
                """
                SELECT
                    d.id AS doc_id,
                    d.doc_date,
                    d.frontmatter->>'source_url' AS url,
                    d.frontmatter->>'topic_slug' AS topic_slug,
                    d.frontmatter->>'related_zettel_id' AS related_zettel_id,
                    a.fetched_title,
                    a.published_at,
                    COALESCE(
                        (SELECT json_agg(t.tag) FROM tags t WHERE t.doc_id = d.id), '[]'::json
                    ) AS tags
                FROM documents d
                JOIN articles a ON a.url = d.frontmatter->>'source_url'
                WHERE d.doc_type = 'original'
                  AND a.arxiv_fulltext_pending IS TRUE
                  AND d.doc_date >= :cutoff_date
                """
            ),
            {"cutoff_date": cutoff_date},
        ).mappings().all()
    return [dict(row) for row in rows]


def aggregate_list_topic_slugs() -> list[str]:
    """aggregate_activity 专用：查询当前实际存在的全部 topic slug（04 §2.5 `is_new` 强制
    规则——唯一依据是这里查出来的实际存储状态，不允许凭模型自己的判断推断）。
    """
    engine = get_engine()
    with engine.begin() as conn:
        rows = conn.execute(text("SELECT id FROM documents WHERE doc_type = 'topic'")).all()
    return [row[0] for row in rows]


def aggregate_lookup_url_index_entry(normalized_url: str) -> dict | None:
    """只读查询，一次拿到 aggregate_activity 需要的两个字段：`zettel_id`（Zettel 三级
    复用判断①用）+ `first_seen_date`（Daily"复盘"情形判定用，04 §2.5）。不越权碰这张表
    的其他字段——写操作仍然只属于 filter_* 系列和 write_backfill_zettel_id。
    """
    engine = get_engine()
    with engine.begin() as conn:
        row = conn.execute(
            text("SELECT zettel_id, first_seen_date FROM url_index WHERE normalized_url = :url"),
            {"url": normalized_url},
        ).mappings().first()
    return dict(row) if row else None


def aggregate_search_zettel_by_slug(slug: str) -> str | None:
    """Zettel 三级复用判断②：索引没有记录时，按 slug 后缀搜索现有 Zettel 文档（04 §2.5）。
    Zettel id 固定格式 `<12位时间戳>-<slug>`，同一 slug 可能匹配多条历史记录，取最近更新的一条。
    """
    engine = get_engine()
    with engine.begin() as conn:
        row = conn.execute(
            text(
                "SELECT id FROM documents WHERE doc_type = 'zettel' AND id LIKE :pattern "
                "ORDER BY updated_at DESC LIMIT 1"
            ),
            {"pattern": f"%-{slug}"},
        ).first()
    return row[0] if row else None


def aggregate_get_document(doc_id: str) -> dict | None:
    """读取已有文档（Topic 追加铁律需要先读旧文档才能决定怎么合并写回，04 §2.5/§2.6）。"""
    engine = get_engine()
    with engine.begin() as conn:
        row = conn.execute(text("SELECT * FROM documents WHERE id = :id"), {"id": doc_id}).mappings().first()
    return dict(row) if row else None


def aggregate_get_daily_by_date(doc_date: date) -> dict | None:
    """Daily"昨日回顾"需要查昨天的 Daily 文档（04 §2.5）。"""
    engine = get_engine()
    with engine.begin() as conn:
        row = conn.execute(
            text("SELECT * FROM documents WHERE doc_type = 'daily' AND doc_date = :d"),
            {"d": doc_date},
        ).mappings().first()
    return dict(row) if row else None


def document_id_exists(doc_id: str) -> bool:
    """给 write 阶段的 ID 冲突顺延逻辑（04 §2.6：同 HHMM 冲突顺延）用。"""
    engine = get_engine()
    with engine.begin() as conn:
        row = conn.execute(text("SELECT 1 FROM documents WHERE id = :id"), {"id": doc_id}).first()
    return row is not None


def deep_dive_list_original_documents_in_window(window_start: date, window_end: date) -> list[dict]:
    """`worker/deep_dive.py` 专用：取窗口内全部原文归档的 topic_slug 分布明细，供纯 Python
    侧机械聚合"热门 topic"统计（M10：不重新调用聚类 LLM，只统计既有分类结果）。只做原始行
    读取，不在 SQL 里 GROUP BY——聚合判断这类跨行逻辑照本文件既有分工留给调用方（对齐
    aggregate.py 里 `_compute_daily_stats` 等统计全部在 Python 侧完成的写法）。
    """
    engine = get_engine()
    with engine.begin() as conn:
        rows = conn.execute(
            text(
                """
                SELECT
                    id AS doc_id,
                    doc_date,
                    title,
                    frontmatter->>'topic_slug' AS topic_slug,
                    frontmatter->>'source_name' AS source_name,
                    frontmatter->>'gist' AS gist
                FROM documents
                WHERE doc_type = 'original' AND doc_date BETWEEN :window_start AND :window_end
                """
            ),
            {"window_start": window_start, "window_end": window_end},
        ).mappings().all()
    return [dict(row) for row in rows]


def deep_dive_list_digest_documents_in_window(window_start: date, window_end: date) -> list[dict]:
    """`worker/deep_dive.py` 专用：取窗口内逐日 Digest 原文，供 LLM 生成周叙事导语时当
    素材（原样使用 body_md，不重新解析 blurb 结构）。"""
    engine = get_engine()
    with engine.begin() as conn:
        rows = conn.execute(
            text(
                """
                SELECT id AS doc_id, doc_date, body_md
                FROM documents
                WHERE doc_type = 'digest' AND doc_date BETWEEN :window_start AND :window_end
                ORDER BY doc_date
                """
            ),
            {"window_start": window_start, "window_end": window_end},
        ).mappings().all()
    return [dict(row) for row in rows]


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


def upsert_document(
    *,
    doc_id: str,
    doc_type: str,
    title: str,
    doc_date: date | None,
    frontmatter: dict,
    body_md: str,
    content_hash: str,
) -> None:
    """write_activity：直接 upsert 进 documents 表，doc_type 由调用方指定——M5 起五类文档
    （original/zettel/topic/daily/digest）统一走这一个函数，不再各写各的（04 §2.6）。
    """
    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO documents (id, doc_type, title, doc_date, frontmatter, body_md, content_hash, updated_at)
                VALUES (:id, :doc_type, :title, :doc_date, CAST(:frontmatter AS JSONB), :body_md, :content_hash, now())
                ON CONFLICT (id) DO UPDATE SET
                    doc_type = EXCLUDED.doc_type,
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
                "doc_type": doc_type,
                "title": title,
                "doc_date": doc_date,
                "frontmatter": json.dumps(frontmatter, ensure_ascii=False, default=str),
                "body_md": body_md,
                "content_hash": content_hash,
            },
        )


def sync_document_tags(doc_id: str, tags: list[str]) -> None:
    """幂等重建某文档的 `tags` 行（先删后插）：tags 反映的是"这次打标判断"的最新结果，
    不需要跨批次累积，全量重建即可（04 §2.5 Tags 四轴策略落地）。
    """
    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM tags WHERE doc_id = :doc_id"), {"doc_id": doc_id})
        for tag in tags:
            conn.execute(
                text("INSERT INTO tags (doc_id, tag) VALUES (:doc_id, :tag) ON CONFLICT DO NOTHING"),
                {"doc_id": doc_id, "tag": tag},
            )


def sync_document_links(from_id: str, to_ids: list[str]) -> None:
    """新增 wikilink 出边（04 §2.5/§2.7）：只增量插入，不做先删后插的全量重建——Topic/Daily
    这类追加型文档的历史出边需要长期保留，不能因为某次调用只传入"本批次新增的链接"就把
    历史边冲掉。`links.to_id` 有外键约束，调用方需保证 to_id 对应的文档已经存在。
    """
    engine = get_engine()
    with engine.begin() as conn:
        for to_id in to_ids:
            conn.execute(
                text("INSERT INTO links (from_id, to_id) VALUES (:from_id, :to_id) ON CONFLICT DO NOTHING"),
                {"from_id": from_id, "to_id": to_id},
            )

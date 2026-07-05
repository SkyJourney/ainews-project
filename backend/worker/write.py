"""write_activity（见 04 §2.6）：直接 upsert 进 documents 表，涵盖五类文档
（original/zettel/topic/daily/digest），并同步 tags/links 两张表。

aggregate_activity 已经把每条记录组装成统一形状：
{doc_id, doc_type, title, doc_date, frontmatter, body_md, content_hash, tags, link_targets}
本模块只负责持久化，不做任何业务判断（分桶/复用/追加合并等判断全部在 aggregate_activity
完成）。

links 同步顺序很关键：必须等本批次全部文档都 upsert 完成后才能同步链接边——Daily/Topic
这类文档经常引用同一批次里刚创建的 Zettel/Original，`links.to_id` 有外键约束，提前插入
会报违反外键错误。
"""

from __future__ import annotations

from temporalio import activity

from worker.db import (
    sync_document_links,
    sync_document_tags,
    upsert_document,
    write_backfill_zettel_id,
)
from worker.filter import normalize_url_for_index


@activity.defn
def write_activity(records: list[dict]) -> int:
    for record in records:
        upsert_document(
            doc_id=record["doc_id"],
            doc_type=record["doc_type"],
            title=record["title"],
            doc_date=record["doc_date"],
            frontmatter=record["frontmatter"],
            body_md=record["body_md"],
            content_hash=record["content_hash"],
        )

    for record in records:
        sync_document_tags(record["doc_id"], record.get("tags") or [])
        sync_document_links(record["doc_id"], record.get("link_targets") or [])

        # 仅 Original 文档的 frontmatter 带 related_zettel_id（04 §2.5：这篇文章本批次新建
        # 或复用了 zettel 时才有值）；其余 doc_type 没有这个字段，.get 安全跳过不回填。
        zettel_id = record["frontmatter"].get("related_zettel_id")
        source_url = record["frontmatter"].get("source_url")
        if zettel_id and source_url:
            write_backfill_zettel_id(normalize_url_for_index(source_url), zettel_id)

    activity.logger.info(f"write_activity: 写入 {len(records)} 条文档")
    return len(records)

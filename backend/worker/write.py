"""write_activity（M1 最简版，见 04 §2.6）：直接 upsert 进 documents 表，仅 doc_type='zettel'。

tags/links 表本阶段不写——四轴打标策略是 M5 才有意义去实现，硬编一套假分类没有价值。
"""

from __future__ import annotations

from temporalio import activity

from worker.db import upsert_zettel_document


@activity.defn
def write_activity(records: list[dict]) -> int:
    for record in records:
        upsert_zettel_document(
            doc_id=record["doc_id"],
            title=record["title"],
            doc_date=record["doc_date"],
            frontmatter=record["frontmatter"],
            body_md=record["body_md"],
            content_hash=record["content_hash"],
        )
    activity.logger.info(f"write_activity: 写入 {len(records)} 条 zettel 文档")
    return len(records)

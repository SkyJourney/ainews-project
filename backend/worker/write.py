"""write_activity（见 04 §2.6）：直接 upsert 进 documents 表，仅 doc_type='zettel'。

tags/links 表本阶段不写——四轴打标策略是 M5 才有意义去实现，硬编一套假分类没有价值。
"""

from __future__ import annotations

from temporalio import activity

from worker.db import upsert_zettel_document, write_backfill_zettel_id
from worker.filter import normalize_url_for_index


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
        # url_index 的 zettel_id 回填：M1-M4 每篇文章都会新建 zettel（还没有 M5 的复用
        # 判断），先把这一列填上，M5 起 aggregate 会开始读它做复用判断（04 §2.3/§2.5）。
        source_url = record["frontmatter"].get("source_url")
        if source_url:
            write_backfill_zettel_id(normalize_url_for_index(source_url), record["doc_id"])

    activity.logger.info(f"write_activity: 写入 {len(records)} 条 zettel 文档")
    return len(records)

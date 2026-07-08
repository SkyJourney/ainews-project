"""arxiv 全文回补专用 activity（2026-07-08 新增，详见 .claude/memory/decisions.md）。

背景：arxiv 论文提交后 HTML 全文渲染是 arxiv 自己后台的异步批处理（约 88% 在 3 天内
可用，一小部分永远转换失败），主流水线当天抓取新文章时如果全文还没渲染出来，只能
先用摘要页兜底（`fetch_original_activity` 设置 `documents.original.frontmatter.
arxiv_fulltext_pending=True`，见 enrich.py/aggregate.py）。`ArxivFulltextBackfillWorkflow`
（worker/workflows.py）每天独立调度一次，用这里的 activity 查候选、写回结果——只更新
documents.original 本身（保留原有 topic_slug/tags/related_zettel_id），完全不碰
Topic/Daily/Digest（这是内容质量回补，不是"今天的新闻"，不应该在 Topic/Daily 里
产生新的当天条目）。
"""

from __future__ import annotations

from datetime import date, timedelta

from temporalio import activity

from worker.aggregate import build_original_record
from worker.db import fetch_enriched_article_by_url, list_arxiv_fulltext_backfill_candidates
from worker.write import write_activity

# 超过这个窗口还标记着 pending 的文章不再回补检查——arxiv 全文渲染失败的那一小部分
# 论文可能永远不会转换成功，无限期重试没有意义（04 §2.4 同类"降级即完成"的精神延伸）。
BACKFILL_WINDOW_DAYS = 14


@activity.defn
def list_arxiv_fulltext_backfill_candidates_activity() -> list[dict]:
    cutoff = date.today() - timedelta(days=BACKFILL_WINDOW_DAYS)
    return list_arxiv_fulltext_backfill_candidates(cutoff)


@activity.defn
def refresh_original_document_activity(payload: dict) -> None:
    """候选文章重新 enrich（复用 EnrichArticleWorkflow）成功后，读回 articles 表最新
    结果，复用 `build_original_record` 组装记录，只更新这一条 documents.original——
    保留原有 topic_slug/tags/related_zettel_id，不重新聚类，不新建/追加任何 Topic/
    Daily/Digest 记录。"""
    article = fetch_enriched_article_by_url(payload["url"])
    if article is None:
        activity.logger.warning(f"arxiv 回补：{payload['url']} 重新 enrich 后查不到 articles 记录，跳过写回")
        return
    decision = {"topic_slug": payload["topic_slug"]}
    record = build_original_record(
        article, payload["doc_id"], payload["related_zettel_id"], decision, payload["tags"]
    )
    # doc_date 固定用候选查询时读到的既有文档发布日期，不用 build_original_record 重新
    # 计算的结果——如果重新 enrich 后 articles.published_at 恰好是 None（畸形/缺失发布
    # 时间），build_original_record 会用 date.today() 兜底，这里如果不覆盖就会把文档的
    # 发布日期悄悄改到回补执行当天。
    record["doc_date"] = payload["doc_date"]
    write_activity([record])
    activity.logger.info(f"arxiv 回补：{payload['doc_id']} 全文已就绪，正文已更新（{payload['url']}）")

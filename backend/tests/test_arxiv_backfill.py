"""arxiv 全文回补 activity 纯逻辑分支单测（2026-07-08）。全部 mock 掉 worker.db 的数据库
函数和 worker.aggregate.build_original_record/worker.write.write_activity，不连真实
Postgres/LiteLLM 网关。
"""

from __future__ import annotations

from datetime import date

from worker import arxiv_backfill


def test_refresh_preserves_existing_doc_date_even_if_rebuild_drifts(mocker):
    """2026-07-08 修复：build_original_record 在 article["published_at"] 为 None 时会用
    date.today() 兜底重新计算 doc_date——回补场景下这会把已发布文档的 doc_date 悄悄改到
    回补执行当天。refresh_original_document_activity 必须固定用候选查询时读到的既有
    doc_date 覆盖掉这个重新计算的结果，不能让文档的发布日期身份漂移。
    """
    mocker.patch.object(
        arxiv_backfill,
        "fetch_enriched_article_by_url",
        return_value={"published_at": None, "url": "http://arxiv.org/abs/2607.00001v1"},
    )
    # 模拟 build_original_record 因 published_at=None 而用 date.today() 兜底出一个
    # "错误"的 doc_date（今天），验证调用方会用 payload 里的既有 doc_date 覆盖它。
    drifted_record = {"doc_id": "original-abc", "doc_date": date(2026, 7, 8), "body_md": "正文"}
    build_record_mock = mocker.patch.object(
        arxiv_backfill, "build_original_record", return_value=drifted_record
    )
    write_activity_mock = mocker.patch.object(arxiv_backfill, "write_activity")

    payload = {
        "doc_id": "original-abc",
        "url": "http://arxiv.org/abs/2607.00001v1",
        "topic_slug": "research-papers",
        "related_zettel_id": None,
        "tags": ["arxiv"],
        "doc_date": date(2026, 6, 20),  # 文档真实的发布日期，早于回补执行当天
    }
    arxiv_backfill.refresh_original_document_activity(payload)

    assert build_record_mock.called
    written_records = write_activity_mock.call_args[0][0]
    assert written_records[0]["doc_date"] == date(2026, 6, 20)


def test_refresh_skips_write_when_article_missing(mocker):
    mocker.patch.object(arxiv_backfill, "fetch_enriched_article_by_url", return_value=None)
    write_activity_mock = mocker.patch.object(arxiv_backfill, "write_activity")

    arxiv_backfill.refresh_original_document_activity(
        {
            "doc_id": "original-missing",
            "url": "http://arxiv.org/abs/2607.99999v1",
            "topic_slug": "research-papers",
            "related_zettel_id": None,
            "tags": [],
            "doc_date": date(2026, 6, 20),
        }
    )

    write_activity_mock.assert_not_called()

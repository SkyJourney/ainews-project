"""write_activity 纯编排逻辑单测：mock 掉 worker.db 的持久化函数，验证调用顺序与
"仅 Original 文档才回填 zettel_id" 这条边界规则（04 §2.3/§2.5）。
"""

from __future__ import annotations

from worker import write


def _make_record(**overrides) -> dict:
    base = {
        "doc_id": "original-abc123",
        "doc_type": "original",
        "title": "标题",
        "doc_date": None,
        "frontmatter": {"source_url": "https://example.com/a", "related_zettel_id": None},
        "body_md": "正文",
        "content_hash": "hash",
        "tags": ["model-releases", "openai"],
        "link_targets": ["202607050931-some-note"],
    }
    base.update(overrides)
    return base


def test_write_activity_upserts_every_record(mocker):
    upsert = mocker.patch.object(write, "upsert_document")
    mocker.patch.object(write, "sync_document_tags")
    mocker.patch.object(write, "sync_document_links")
    mocker.patch.object(write, "write_backfill_zettel_id")

    records = [_make_record(doc_id="a"), _make_record(doc_id="b", frontmatter={})]
    written = write.write_activity(records)

    assert written == 2
    assert upsert.call_count == 2


def test_write_activity_syncs_tags_and_links_for_every_record(mocker):
    mocker.patch.object(write, "upsert_document")
    sync_tags = mocker.patch.object(write, "sync_document_tags")
    sync_links = mocker.patch.object(write, "sync_document_links")
    mocker.patch.object(write, "write_backfill_zettel_id")

    record = _make_record(tags=["agents"], link_targets=["some-original-id"])
    write.write_activity([record])

    sync_tags.assert_called_once_with("original-abc123", ["agents"])
    sync_links.assert_called_once_with("original-abc123", ["some-original-id"])


def test_write_activity_backfills_zettel_id_only_when_present(mocker):
    mocker.patch.object(write, "upsert_document")
    mocker.patch.object(write, "sync_document_tags")
    mocker.patch.object(write, "sync_document_links")
    backfill = mocker.patch.object(write, "write_backfill_zettel_id")

    with_zettel = _make_record(
        doc_id="original-1",
        frontmatter={"source_url": "https://example.com/a", "related_zettel_id": "202607050931-note"},
    )
    without_zettel = _make_record(
        doc_id="original-2",
        frontmatter={"source_url": "https://example.com/b", "related_zettel_id": None},
    )
    topic_doc = _make_record(doc_id="agents", doc_type="topic", frontmatter={"title": "Agents"})

    write.write_activity([with_zettel, without_zettel, topic_doc])

    assert backfill.call_count == 1
    backfill.assert_called_once_with("example.com/a", "202607050931-note")


def test_write_activity_handles_missing_optional_fields(mocker):
    """tags/link_targets 缺省时不应报错（.get 兜底为空列表）。"""
    mocker.patch.object(write, "upsert_document")
    sync_tags = mocker.patch.object(write, "sync_document_tags")
    sync_links = mocker.patch.object(write, "sync_document_links")
    mocker.patch.object(write, "write_backfill_zettel_id")

    record = _make_record()
    del record["tags"]
    del record["link_targets"]

    write.write_activity([record])
    sync_tags.assert_called_once_with("original-abc123", [])
    sync_links.assert_called_once_with("original-abc123", [])

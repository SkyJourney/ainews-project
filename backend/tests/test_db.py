"""db.py 幂等/语义正确性测试：mock 掉 SQLAlchemy engine/connection，断言实际执行的
SQL 语句形状，而不是连真实 Postgres（04 §2.6/§2.7，见 .claude/memory/known_issues.md
里"db.py 零测试覆盖"的缺口）。

重点覆盖 sync_document_links（只增不删）与 sync_document_tags（先删后插）这两种
故意设计成不对称的语义——Topic/Daily 这类追加型文档的历史反链不能被冲掉，这条规则
如果被误改成对称，不会有任何真实批次验证以外的手段发现。
"""

from __future__ import annotations

import json

from worker import db


def _mock_engine(mocker):
    """构造一个假的 get_engine()，返回值支持 `with engine.begin() as conn: conn.execute(...)`
    这种用法，调用方可以通过 conn.execute.call_args_list 检查实际执行过的 SQL/参数。
    """
    conn = mocker.MagicMock()
    conn.execute = mocker.MagicMock()
    begin_cm = mocker.MagicMock()
    begin_cm.__enter__ = mocker.MagicMock(return_value=conn)
    begin_cm.__exit__ = mocker.MagicMock(return_value=False)
    engine = mocker.MagicMock()
    engine.begin.return_value = begin_cm
    mocker.patch.object(db, "get_engine", return_value=engine)
    return conn


def _executed_sql(conn) -> list[str]:
    return [str(call.args[0]) for call in conn.execute.call_args_list]


# ---------------------------------------------------------------------------
# sync_document_links：只增量插入，绝不删除历史出边
# ---------------------------------------------------------------------------

def test_sync_document_links_never_issues_delete(mocker):
    conn = _mock_engine(mocker)
    db.sync_document_links("topic-agents", ["original-a", "original-b"])

    sql_statements = _executed_sql(conn)
    assert sql_statements, "应该至少执行过一次 INSERT"
    assert not any("DELETE" in sql.upper() for sql in sql_statements), (
        "sync_document_links 语义上只增不删——Topic/Daily 的历史反链不能被冲掉"
    )
    assert all("INSERT" in sql.upper() for sql in sql_statements)


def test_sync_document_links_inserts_one_row_per_target(mocker):
    conn = _mock_engine(mocker)
    db.sync_document_links("topic-agents", ["original-a", "original-b", "original-c"])
    assert conn.execute.call_count == 3


def test_sync_document_links_no_targets_executes_nothing(mocker):
    conn = _mock_engine(mocker)
    db.sync_document_links("topic-agents", [])
    conn.execute.assert_not_called()


# ---------------------------------------------------------------------------
# sync_document_tags：先删后插，全量重建（tags 反映的是"最新一次判断"，不需要累积）
# ---------------------------------------------------------------------------

def test_sync_document_tags_deletes_before_inserting(mocker):
    conn = _mock_engine(mocker)
    db.sync_document_tags("original-a", ["model-releases", "openai"])

    sql_statements = _executed_sql(conn)
    assert "DELETE" in sql_statements[0].upper(), "必须先删除该文档的旧 tags，全量重建"
    assert all("INSERT" in sql.upper() for sql in sql_statements[1:])
    assert len(sql_statements) == 1 + 2  # 1 次 delete + 每个 tag 一次 insert


def test_sync_document_tags_empty_list_still_deletes_old_tags(mocker):
    """打标结果变成空列表时，旧 tags 也应该被清空——不是"跳过不处理"。"""
    conn = _mock_engine(mocker)
    db.sync_document_tags("original-a", [])

    sql_statements = _executed_sql(conn)
    assert len(sql_statements) == 1
    assert "DELETE" in sql_statements[0].upper()


# ---------------------------------------------------------------------------
# upsert_document：整行覆盖式 upsert，frontmatter 走 JSON 序列化
# ---------------------------------------------------------------------------

def test_upsert_document_serializes_frontmatter_as_json(mocker):
    conn = _mock_engine(mocker)
    db.upsert_document(
        doc_id="original-abc",
        doc_type="original",
        title="标题",
        doc_date=None,
        frontmatter={"word_count": 100, "tags": ["a"]},
        body_md="正文",
        content_hash="hash123",
    )

    params = conn.execute.call_args.args[1]
    assert json.loads(params["frontmatter"]) == {"word_count": 100, "tags": ["a"]}
    assert params["body_md"] == "正文"
    assert params["content_hash"] == "hash123"


def test_upsert_document_sql_is_on_conflict_do_update(mocker):
    conn = _mock_engine(mocker)
    db.upsert_document(
        doc_id="original-abc", doc_type="original", title="标题", doc_date=None,
        frontmatter={}, body_md="正文", content_hash="hash123",
    )
    sql = str(conn.execute.call_args.args[0]).upper()
    assert "ON CONFLICT" in sql
    assert "DO UPDATE" in sql


# ---------------------------------------------------------------------------
# 通用读取：document_id_exists / aggregate_get_document
# ---------------------------------------------------------------------------

def test_document_id_exists_true_when_row_found(mocker):
    conn = _mock_engine(mocker)
    conn.execute.return_value.first.return_value = (1,)
    assert db.document_id_exists("original-abc") is True


def test_document_id_exists_false_when_no_row(mocker):
    conn = _mock_engine(mocker)
    conn.execute.return_value.first.return_value = None
    assert db.document_id_exists("original-missing") is False


def test_aggregate_get_document_returns_dict_or_none(mocker):
    conn = _mock_engine(mocker)
    conn.execute.return_value.mappings.return_value.first.return_value = {"id": "topic-agents", "article_count": 43}
    result = db.aggregate_get_document("topic-agents")
    assert result == {"id": "topic-agents", "article_count": 43}

    conn.execute.return_value.mappings.return_value.first.return_value = None
    assert db.aggregate_get_document("topic-missing") is None

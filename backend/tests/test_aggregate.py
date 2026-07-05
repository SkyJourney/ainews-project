"""aggregate_activity 纯逻辑分支单测（04 §2.5）。全部 mock 掉 worker.db 的数据库函数和
worker.llm_client.call_structured，不连真实 Postgres/LiteLLM 网关。
"""

from __future__ import annotations

from datetime import date

import pytest

from tests.conftest import make_enriched_article
from worker import aggregate


# ---------------------------------------------------------------------------
# is_new 机械核验 + 分桶粒度规则（_apply_granularity_rules）
# ---------------------------------------------------------------------------

def test_granularity_existing_topic_no_minimum_count():
    """已存在的 topic：本批次哪怕只有 1 条也不受"新领域"最低条数限制。"""
    cluster_by_url = {"u1": {"topic_slug": "model-releases", "zettel_worthy": False, "rationale": "r"}}
    decisions = aggregate._apply_granularity_rules(cluster_by_url, existing_topics={"model-releases"})
    assert decisions["u1"]["topic_slug"] == "model-releases"
    assert decisions["u1"]["is_new_topic"] is False


def test_granularity_new_topic_below_threshold_merged_to_uncategorized():
    """新 topic 候选本批次仅 2 条（< 3），应降级并入 uncategorized。"""
    cluster_by_url = {
        "u1": {"topic_slug": "quantum-computing", "zettel_worthy": False, "rationale": "r"},
        "u2": {"topic_slug": "quantum-computing", "zettel_worthy": False, "rationale": "r"},
    }
    decisions = aggregate._apply_granularity_rules(cluster_by_url, existing_topics=set())
    assert decisions["u1"]["topic_slug"] == aggregate.PLACEHOLDER_TOPIC
    assert decisions["u2"]["topic_slug"] == aggregate.PLACEHOLDER_TOPIC


def test_granularity_new_topic_meets_threshold_kept():
    """新 topic 候选本批次达到 3 条，允许创建新 topic，is_new 为 True。"""
    cluster_by_url = {
        f"u{i}": {"topic_slug": "quantum-computing", "zettel_worthy": False, "rationale": "r"} for i in range(3)
    }
    decisions = aggregate._apply_granularity_rules(cluster_by_url, existing_topics=set())
    assert all(d["topic_slug"] == "quantum-computing" for d in decisions.values())
    assert all(d["is_new_topic"] is True for d in decisions.values())


def test_granularity_is_new_derived_only_from_existing_topics_snapshot():
    """is_new 的唯一依据是传入的 existing_topics 快照，不受 zettel_worthy/rationale 影响。"""
    cluster_by_url = {"u1": {"topic_slug": "agents", "zettel_worthy": True, "rationale": "首次出现"}}
    decisions = aggregate._apply_granularity_rules(cluster_by_url, existing_topics={"agents"})
    assert decisions["u1"]["is_new_topic"] is False
    decisions2 = aggregate._apply_granularity_rules(cluster_by_url, existing_topics=set())
    # 单条也不足 3，会被降级，但降级后的 uncategorized 同样不在 existing_topics 里，仍是 is_new
    assert decisions2["u1"]["topic_slug"] == aggregate.PLACEHOLDER_TOPIC
    assert decisions2["u1"]["is_new_topic"] is True


# ---------------------------------------------------------------------------
# Zettel 三级复用判断（_decide_zettel）
# ---------------------------------------------------------------------------

def test_decide_zettel_tier1_reuse_from_url_index(mocker):
    mocker.patch.object(aggregate, "aggregate_search_zettel_by_slug", side_effect=AssertionError("不应该走到 tier②"))
    mocker.patch.object(aggregate, "document_id_exists", return_value=True)
    index_entry = {"zettel_id": "202607010900-existing-note", "first_seen_date": date(2026, 7, 1)}
    result = aggregate._decide_zettel(index_entry, "some-slug", used_ids=set())
    assert result == {"action": "reuse", "zettel_id": "202607010900-existing-note"}


def test_decide_zettel_tier1_stale_reference_falls_through_to_tier2(mocker):
    """url_index 记得 zettel_id，但对应文档已不存在（如两张表不同步）——不能无条件信任
    索引值，否则 write_activity 阶段会因 links 外键违反导致整批写入失败。"""
    mocker.patch.object(aggregate, "document_id_exists", return_value=False)
    mocker.patch.object(aggregate, "aggregate_search_zettel_by_slug", return_value="202607020900-slug-hit")
    index_entry = {"zettel_id": "202607010900-stale-note", "first_seen_date": date(2026, 7, 1)}
    result = aggregate._decide_zettel(index_entry, "slug-hit", used_ids=set())
    assert result == {"action": "reuse", "zettel_id": "202607020900-slug-hit"}


def test_decide_zettel_tier2_reuse_by_slug_search(mocker):
    mocker.patch.object(aggregate, "aggregate_search_zettel_by_slug", return_value="202607010900-some-slug")
    result = aggregate._decide_zettel(None, "some-slug", used_ids=set())
    assert result == {"action": "reuse", "zettel_id": "202607010900-some-slug"}


def test_decide_zettel_tier2_index_entry_present_but_no_zettel_id(mocker):
    """url_index 里有记录但 zettel_id 字段为空（首次被跨日去重命中但从未升级过），仍要走②。"""
    mocker.patch.object(aggregate, "aggregate_search_zettel_by_slug", return_value="202607010900-some-slug")
    index_entry = {"zettel_id": None, "first_seen_date": date(2026, 7, 1)}
    result = aggregate._decide_zettel(index_entry, "some-slug", used_ids=set())
    assert result == {"action": "reuse", "zettel_id": "202607010900-some-slug"}


def test_decide_zettel_tier3_create_new(mocker):
    mocker.patch.object(aggregate, "aggregate_search_zettel_by_slug", return_value=None)
    mocker.patch.object(aggregate, "_generate_doc_id", return_value="202607050931-brand-new")
    result = aggregate._decide_zettel(None, "brand-new", used_ids=set())
    assert result == {"action": "create", "zettel_id": "202607050931-brand-new"}


# ---------------------------------------------------------------------------
# Topic 追加铁律（_insert_topic_block）
# ---------------------------------------------------------------------------

def test_insert_topic_block_new_date_inserted_before_latest():
    """当天区块不存在：新区块应插入到最新（已存在）区块之前，倒序排列。"""
    existing_body = "# Model Releases\n\n## 2026-07-04\n\n- [[old-id]] 旧文章：旧摘要\n"
    result = aggregate._insert_topic_block(existing_body, date(2026, 7, 5), ["- [[new-id]] 新文章：新摘要"])
    idx_new = result.index("## 2026-07-05")
    idx_old = result.index("## 2026-07-04")
    assert idx_new < idx_old
    assert "- [[new-id]] 新文章：新摘要" in result
    assert "- [[old-id]] 旧文章：旧摘要" in result  # 历史内容没有丢失


def test_insert_topic_block_same_date_appends_within_block():
    """当天区块已存在（同一天跑了两次批次）：新增条目追加进该区块内部，不新建区块。"""
    existing_body = (
        "# Agents\n\n## 2026-07-05\n\n- [[id-1]] 文章一：摘要一\n\n## 2026-07-04\n\n- [[id-0]] 昨天：摘要\n"
    )
    result = aggregate._insert_topic_block(existing_body, date(2026, 7, 5), ["- [[id-2]] 文章二：摘要二"])
    assert result.count("## 2026-07-05") == 1
    assert result.index("[[id-1]]") < result.index("[[id-2]]") < result.index("## 2026-07-04")


def test_build_topic_record_first_creation_writes_full_frontmatter(mocker):
    mocker.patch.object(aggregate, "aggregate_get_document", return_value=None)
    article = make_enriched_article()
    record = aggregate._build_topic_record("agents", [(article, None, "original-abc123")], date(2026, 7, 5))
    assert record["frontmatter"]["created_date"] == "2026-07-05"
    assert record["frontmatter"]["article_count"] == 1
    assert "## 2026-07-05" in record["body_md"]


def test_build_topic_record_append_never_overwrites_history(mocker):
    """追加铁律核心断言：旧内容必须原样保留在新 body_md 里，article_count 累加而不是重置。"""
    existing_doc = {
        "body_md": "# Agents\n\n## 2026-07-04\n\n- [[old]] 历史条目：历史摘要\n",
        "frontmatter": {"title": "Agents", "created_date": "2026-07-04", "last_updated_date": "2026-07-04", "article_count": 1},
    }
    mocker.patch.object(aggregate, "aggregate_get_document", return_value=existing_doc)
    article = make_enriched_article(url="https://example.com/new")
    record = aggregate._build_topic_record("agents", [(article, None, "original-def456")], date(2026, 7, 5))
    assert "历史条目：历史摘要" in record["body_md"]
    assert record["frontmatter"]["article_count"] == 2
    assert record["frontmatter"]["created_date"] == "2026-07-04"  # 首建日期不应被覆盖
    assert record["frontmatter"]["last_updated_date"] == "2026-07-05"


# ---------------------------------------------------------------------------
# Daily 五种情形分类（_classify_daily_entry）
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "fetch_channel,zettel_id,is_recap,expected",
    [
        ("direct", "zid", False, "has_zettel_has_original"),
        ("direct", None, False, "no_zettel_has_original"),
        ("placeholder", "zid", False, "has_zettel_original_missing"),
        ("direct", "zid", True, "recap_with_zettel"),
        ("direct", None, True, "recap_not_upgraded"),
    ],
)
def test_classify_daily_entry(fetch_channel, zettel_id, is_recap, expected):
    article = make_enriched_article(fetch_channel=fetch_channel)
    ctx = {"zettel_id": zettel_id, "is_recap": is_recap}
    assert aggregate._classify_daily_entry(article, ctx) == expected


# ---------------------------------------------------------------------------
# Digest 五项自检（_build_digest_entries / _truncate_blurb）
# ---------------------------------------------------------------------------

def test_truncate_blurb_short_text_unchanged():
    assert aggregate._truncate_blurb("短句子。") == "短句子。"


def test_truncate_blurb_truncates_at_sentence_boundary():
    text = "第一句。" + "填充内容" * 40 + "。最后一句超出上限的内容。"
    result = aggregate._truncate_blurb(text, max_chars=20)
    assert len(result) <= 21  # 允许含标点
    assert result.endswith("。")


def test_truncate_blurb_hard_cut_when_no_punctuation():
    text = "a" * 200
    result = aggregate._truncate_blurb(text, max_chars=20)
    assert result.endswith("…")
    assert len(result) == 21


def test_digest_entries_skip_unknown_source(mocker):
    mocker.patch.object(aggregate, "load_sources", return_value={"openai-rss": object()})
    articles = [
        make_enriched_article(url="https://a.com/1", source_name="openai-rss"),
        make_enriched_article(url="https://b.com/2", source_name="not-in-registry"),
    ]
    entries = aggregate._build_digest_entries(articles)
    assert len(entries) == 1
    assert entries[0]["source_name"] == "openai-rss"


def test_digest_entries_dedup_same_url(mocker):
    mocker.patch.object(aggregate, "load_sources", return_value={"openai-rss": object()})
    articles = [
        make_enriched_article(url="https://a.com/1", source_name="openai-rss"),
        make_enriched_article(url="https://a.com/1", source_name="openai-rss"),
    ]
    entries = aggregate._build_digest_entries(articles)
    assert len(entries) == 1


def test_digest_entries_no_synthesized_entries(mocker):
    """禁止合成条目：输出条目数不能超过输入文章数。"""
    mocker.patch.object(aggregate, "load_sources", return_value={"openai-rss": object()})
    articles = [make_enriched_article(url=f"https://a.com/{i}", source_name="openai-rss") for i in range(5)]
    entries = aggregate._build_digest_entries(articles)
    assert len(entries) == 5
    assert {e["url"] for e in entries} == {a["url"] for a in articles}


# ---------------------------------------------------------------------------
# Original doc id 稳定性（同一 URL 跨批次应生成相同 id）
# ---------------------------------------------------------------------------

def test_original_doc_id_stable_across_calls():
    url = "https://example.com/article"
    assert aggregate._original_doc_id(url) == aggregate._original_doc_id(url)


def test_original_doc_id_differs_for_different_urls():
    assert aggregate._original_doc_id("https://a.com/1") != aggregate._original_doc_id("https://a.com/2")


# ---------------------------------------------------------------------------
# 回归测试：body_md 不应该重复拼一遍 title（M6 真实页面验证发现的 bug——
# title 已经是独立字段，frontend 详情页会单独渲染一次，body_md 里再重复一次
# "# {title}" 会导致页面标题渲染两遍）
# ---------------------------------------------------------------------------

def _first_nonblank_line(body_md: str) -> str:
    return next(line for line in body_md.split("\n") if line.strip())


def test_original_record_body_does_not_repeat_title():
    article = make_enriched_article(translated_title="示例标题", translated_summary="正文第一行\n\n正文第二行")
    decision = {"topic_slug": "agents", "is_new_topic": False, "zettel_worthy": False, "rationale": "r"}
    record = aggregate._build_original_record(article, "original-abc", None, decision, [])
    assert record["title"] == "示例标题"
    assert _first_nonblank_line(record["body_md"]) != f"# {record['title']}"
    assert "示例标题" not in record["body_md"]  # 标题文本完全不该出现在正文里


def test_zettel_record_body_does_not_repeat_title():
    article = make_enriched_article(translated_title="示例标题", gist="这是摘要")
    decision = {"topic_slug": "agents", "is_new_topic": False, "zettel_worthy": True, "rationale": "r"}
    record = aggregate._build_zettel_record(article, "202607050931-example", "original-abc", decision, [])
    assert _first_nonblank_line(record["body_md"]) != f"# {record['title']}"
    assert "示例标题" not in record["body_md"]


def test_original_and_zettel_records_have_distinct_doc_type():
    """M1-M4 曾经用 zettel 顶替过 Original 的角色（见 .claude/memory/decisions.md），
    这条断言直接防止两者的 doc_type 被写反/合并——不只测"两个函数存在"，还测"值不一样"。
    """
    article = make_enriched_article(translated_title="示例标题", gist="这是摘要")
    decision = {"topic_slug": "agents", "is_new_topic": False, "zettel_worthy": True, "rationale": "r"}

    original = aggregate._build_original_record(article, "original-abc", "202607050931-example", decision, [])
    zettel = aggregate._build_zettel_record(article, "202607050931-example", "original-abc", decision, [])

    assert original["doc_type"] == "original"
    assert original["frontmatter"]["doc_type"] == "original"
    assert zettel["doc_type"] == "zettel"
    assert zettel["frontmatter"]["doc_type"] == "zettel"
    assert original["doc_type"] != zettel["doc_type"]


def test_topic_record_first_creation_body_does_not_repeat_title(mocker):
    mocker.patch.object(aggregate, "aggregate_get_document", return_value=None)
    article = make_enriched_article()
    record = aggregate._build_topic_record("agents", [(article, None, "original-abc123")], date(2026, 7, 5))
    assert _first_nonblank_line(record["body_md"]) == "## 2026-07-05"  # 直接是日期区块，不是标题
    assert "Agents" not in record["body_md"].split("\n")[0]


def test_daily_record_body_does_not_repeat_title(mocker):
    mocker.patch.object(aggregate, "aggregate_get_daily_by_date", return_value=None)
    mocker.patch.object(aggregate, "call_structured")  # 避免真实 LLM 调用（<=5 篇会跳过，这里保险起见也 mock 掉）
    articles = [make_enriched_article(url="https://a.com/1")]
    decisions = {"https://a.com/1": {"topic_slug": "agents", "is_new_topic": False}}
    per_article_ctx = {
        "https://a.com/1": {
            "original_id": "original-abc",
            "zettel_id": None,
            "is_new_zettel": False,
            "topic_slug": "agents",
            "is_recap": False,
        }
    }
    record = aggregate._build_daily_record(articles, decisions, per_article_ctx, date(2026, 7, 5))
    assert _first_nonblank_line(record["body_md"]) == "## TL;DR"
    assert "AI 日报" not in record["body_md"].split("\n\n")[0]


def test_daily_record_topic_heading_has_emoji_and_wikilink(mocker):
    mocker.patch.object(aggregate, "aggregate_get_daily_by_date", return_value=None)
    mocker.patch.object(aggregate, "call_structured")
    articles = [make_enriched_article(url="https://a.com/1")]
    decisions = {"https://a.com/1": {"topic_slug": "agents", "is_new_topic": False}}
    per_article_ctx = {
        "https://a.com/1": {
            "original_id": "original-abc",
            "zettel_id": None,
            "is_new_zettel": False,
            "topic_slug": "agents",
            "is_recap": False,
        }
    }
    record = aggregate._build_daily_record(articles, decisions, per_article_ctx, date(2026, 7, 5))
    assert "## 🤖 Agent [[agents]]" in record["body_md"]
    assert "agents" in record["link_targets"]  # Topic 反链要看到这条 Daily 引用过它


def test_digest_record_body_does_not_repeat_title(mocker):
    mocker.patch.object(aggregate, "load_sources", return_value={"openai-rss": object()})
    articles = [make_enriched_article(url="https://a.com/1", source_name="openai-rss")]
    record = aggregate._build_digest_record(articles, date(2026, 7, 5))
    assert not record["body_md"].startswith("#")

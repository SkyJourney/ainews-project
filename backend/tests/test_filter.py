"""filter.py 既有纯函数回溯测试（M5 工程化收敛补项：M2 完成时这些函数只靠真实批次
验证过，从未有过针对特定输入/输出的单测）。"""

from __future__ import annotations

from worker import filter as filter_module


def test_jaccard_overlap_identical_text_is_one():
    assert filter_module._jaccard_overlap("OpenAI releases GPT-5", "OpenAI releases GPT-5") == 1.0


def test_jaccard_overlap_empty_string_is_zero_not_exempted():
    """04 §2.3 硬约束：任一方摘要为空则 overlap=0，不豁免。"""
    assert filter_module._jaccard_overlap("", "some text") == 0.0
    assert filter_module._jaccard_overlap("some text", "") == 0.0


def test_jaccard_overlap_chinese_tokenizes_by_character():
    a = "谷歌发布新模型"
    b = "谷歌发布新产品"
    overlap = filter_module._jaccard_overlap(a, b)
    assert 0 < overlap < 1


def test_normalize_url_for_index_strips_www_query_and_trailing_slash():
    assert (
        filter_module.normalize_url_for_index("https://WWW.Example.com/path/?utm_source=x#frag")
        == "example.com/path"
    )


def test_normalize_host_path_ignores_query_but_keeps_scheme():
    assert (
        filter_module._normalize_host_path("https://example.com/a?x=1")
        == filter_module._normalize_host_path("https://example.com/a?x=2")
    )


def test_dedup_same_batch_keeps_more_complete_entry():
    from worker.schemas import Entry

    a = Entry(title="OpenAI releases GPT-5", url="https://openai.com/a", source_name="openai-rss", raw_summary="")
    b = Entry(
        title="OpenAI releases GPT-5",
        url="https://openai.com/a",
        source_name="openai-rss",
        raw_summary="a longer, more complete summary",
    )
    kept, dropped = filter_module._dedup_same_batch([a, b])
    assert dropped == 1
    assert kept[0].raw_summary == "a longer, more complete summary"


def test_filter_staleness_drops_entries_older_than_14_days():
    from datetime import date, timedelta

    from worker.schemas import Entry

    fresh = Entry(title="fresh", url="https://a.com/1", source_name="s", published=date.today())
    stale = Entry(
        title="stale", url="https://a.com/2", source_name="s", published=date.today() - timedelta(days=15)
    )
    kept, dropped = filter_module._filter_staleness([fresh, stale])
    assert dropped == 1
    assert kept == [fresh]


# ---------------------------------------------------------------------------
# ② 跨源同论文去重：范围限论文类源，保留 arxiv.org 规范链接
# ---------------------------------------------------------------------------

def test_dedup_cross_source_papers_prefers_arxiv_canonical_link():
    from worker.schemas import Entry

    hf = Entry(
        title="A Great Paper About Agents",
        url="https://huggingface.co/papers/2607.01234",
        source_name="huggingface-daily-papers",
    )
    arxiv = Entry(
        title="A Great Paper About Agents",
        url="https://arxiv.org/abs/2607.01234",
        source_name="arxiv-api",
    )
    kept, dropped = filter_module._dedup_cross_source_papers([hf, arxiv])
    assert dropped == 1
    assert len(kept) == 1
    assert kept[0].url == "https://arxiv.org/abs/2607.01234"


def test_dedup_cross_source_papers_ignores_non_paper_sources():
    """跨源去重只作用于 PAPER_SOURCES，其余来源即使标题撞了也不合并（这是同批次去重①的职责）。"""
    from worker.schemas import Entry

    a = Entry(title="Same Title Different Source", url="https://a.com/1", source_name="openai-rss")
    b = Entry(title="Same Title Different Source", url="https://b.com/2", source_name="anthropic-news")
    kept, dropped = filter_module._dedup_cross_source_papers([a, b])
    assert dropped == 0
    assert len(kept) == 2


def test_dedup_cross_source_papers_same_source_internal_collision_drops_without_swap():
    """同一个源自己内部撞车（如 arxiv-api 按不同分类查询同一篇论文）不算跨源，
    直接丢弃，不做任何合并动作。"""
    from worker.schemas import Entry

    first = Entry(title="Duplicate Within Source", url="https://arxiv.org/abs/1", source_name="arxiv-api")
    second = Entry(title="Duplicate Within Source", url="https://arxiv.org/abs/2", source_name="arxiv-api")
    kept, dropped = filter_module._dedup_cross_source_papers([first, second])
    assert dropped == 1
    assert kept == [first]  # 保留先出现的那条，第二条原样丢弃


# ---------------------------------------------------------------------------
# ④ 信噪比过滤：命中丢弃模式默认丢弃，命中保留信号（域名/关键词）则覆盖保留
# ---------------------------------------------------------------------------

def test_filter_noise_drops_recruitment_ads():
    from worker.schemas import Entry

    entry = Entry(title="We're hiring engineers!", url="https://a.com/1", source_name="s", raw_summary="join our team")
    kept, dropped = filter_module._filter_noise([entry])
    assert dropped == 1
    assert kept == []


def test_filter_noise_keep_signal_overrides_drop_pattern():
    """命中丢弃模式（融资类）但域名是 arxiv.org（保留域名），信噪比过滤应该放行。"""
    from worker.schemas import Entry

    entry = Entry(
        title="Company raises $10M funding round",
        url="https://arxiv.org/abs/2607.99999",
        source_name="arxiv-api",
    )
    kept, dropped = filter_module._filter_noise([entry])
    assert dropped == 0
    assert kept == [entry]


def test_filter_noise_keeps_entries_with_no_drop_signal():
    from worker.schemas import Entry

    entry = Entry(title="A normal industry news item", url="https://a.com/1", source_name="s")
    kept, dropped = filter_module._filter_noise([entry])
    assert dropped == 0
    assert kept == [entry]


# ---------------------------------------------------------------------------
# filter_activity：全流程统计自检（单调递减 + 丢弃计数对账），mock 掉 url_index 依赖
# ---------------------------------------------------------------------------

def test_filter_activity_end_to_end_with_no_prior_url_index(mocker):
    """mock 跨日去重依赖的 db 函数：url_index 里都是新 URL，全部条目应该原样通过。"""
    from worker.schemas import Entry

    mocker.patch.object(filter_module, "filter_cleanup_url_index")
    mocker.patch.object(filter_module, "filter_lookup_url_index", return_value=None)
    mocker.patch.object(filter_module, "filter_upsert_url_index")

    entries = [
        Entry(title="Fresh Article One", url="https://a.com/1", source_name="openai-rss", raw_summary="正文摘要一"),
        Entry(title="Fresh Article Two", url="https://a.com/2", source_name="openai-rss", raw_summary="正文摘要二"),
    ]
    kept = filter_module.filter_activity(entries, "batch-1")
    assert len(kept) == 2

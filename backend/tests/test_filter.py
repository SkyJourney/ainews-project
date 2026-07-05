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

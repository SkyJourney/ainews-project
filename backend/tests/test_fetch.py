"""fetch.py 纯逻辑分支测试：聚焦最容易随源站结构变化静默失效的正则/日期解析代码
（04 §2.2，见 .claude/memory/known_issues.md 里"fetch.py 零测试覆盖"的缺口）。

只测不需要真实网络请求的纯函数——_fetch_rss/_fetch_arxiv/_fetch_webfetch/_fetch_a16z
等顶层函数会真的发 httpx 请求，不在本文件覆盖范围（真实批次验证兜底）。
"""

from __future__ import annotations

from datetime import date, timedelta

from worker import fetch
from worker.fetch import (
    _hf_paper_to_entry,
    _parse_a16z_list,
    _parse_arxiv_atom,
    _parse_flexible_date,
    _rss_entry_to_entry,
    preflight_activity,
)


# ---------------------------------------------------------------------------
# _parse_flexible_date：相对日期优先于绝对日期，绝对日期做合理性校验
# ---------------------------------------------------------------------------

FETCHED_AT = date(2026, 7, 5)


def test_parse_flexible_date_relative_chinese_days():
    assert _parse_flexible_date("3 天前", FETCHED_AT) == FETCHED_AT - timedelta(days=3)


def test_parse_flexible_date_relative_english_hours():
    assert _parse_flexible_date("5 hours ago", FETCHED_AT) == FETCHED_AT


def test_parse_flexible_date_yesterday_and_today():
    assert _parse_flexible_date("昨天", FETCHED_AT) == FETCHED_AT - timedelta(days=1)
    assert _parse_flexible_date("today", FETCHED_AT) == FETCHED_AT


def test_parse_flexible_date_absolute_valid():
    assert _parse_flexible_date("2026-06-30", FETCHED_AT) == date(2026, 6, 30)


def test_parse_flexible_date_empty_returns_none():
    assert _parse_flexible_date("", FETCHED_AT) is None


def test_parse_flexible_date_unparsable_returns_none():
    assert _parse_flexible_date("not a date at all !!!", FETCHED_AT) is None


def test_parse_flexible_date_absurdly_old_fails_sanity_check():
    # 相差超过 _DATE_SANITY_MAX_DAYS（3650 天 ≈ 10 年），视为不可信
    assert _parse_flexible_date("1990-01-01", FETCHED_AT) is None


# ---------------------------------------------------------------------------
# _parse_arxiv_atom：defusedxml 解析 arXiv Atom feed
# ---------------------------------------------------------------------------

_ARXIV_ATOM_SAMPLE = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>http://arxiv.org/abs/2607.00001v1</id>
    <title>  A Great Paper
      About Agents  </title>
    <summary>  This paper studies agents.  </summary>
    <published>2026-07-01T00:00:00Z</published>
  </entry>
  <entry>
    <id>http://arxiv.org/abs/2607.00002v1</id>
    <title>Missing Date Paper</title>
    <summary>No published date here.</summary>
  </entry>
</feed>
"""


def test_parse_arxiv_atom_normalizes_whitespace_and_date():
    entries = _parse_arxiv_atom(_ARXIV_ATOM_SAMPLE, source_name="arxiv-api")
    assert len(entries) == 2
    first = entries[0]
    assert first.title == "A Great Paper About Agents"
    assert first.published == date(2026, 7, 1)
    assert first.low_confidence is False


def test_parse_arxiv_atom_missing_published_is_low_confidence():
    entries = _parse_arxiv_atom(_ARXIV_ATOM_SAMPLE, source_name="arxiv-api")
    second = entries[1]
    assert second.published is None
    assert second.low_confidence is True


# ---------------------------------------------------------------------------
# _hf_paper_to_entry：huggingface-daily-papers 单条转换
# ---------------------------------------------------------------------------

def test_hf_paper_to_entry_builds_arxiv_abs_url():
    item = {
        "paper": {
            "id": "2607.01234",
            "title": "A HF Paper",
            "summary": "summary text",
            "publishedAt": "2026-07-02T10:00:00.000Z",
            "upvotes": 42,
        }
    }
    entry = _hf_paper_to_entry(item)
    assert entry.url == "https://arxiv.org/abs/2607.01234"
    assert entry.published == date(2026, 7, 2)
    assert entry.low_confidence is False
    assert entry.extra["upvotes"] == 42
    # 2026-07-08：HF 本身没有全文，只是社区筛选信号——entry.source_name 统一改标成
    # arxiv-api，复用 arxiv 现有的全文抓取/去重/分级逻辑，见 decisions.md 的核查记录。
    assert entry.source_name == "arxiv-api"
    assert entry.extra["hf_recommended"] is True


def test_hf_paper_to_entry_missing_id_is_low_confidence():
    item = {"paper": {"title": "No ID Paper", "summary": "summary"}}
    entry = _hf_paper_to_entry(item)
    assert entry.url == ""
    assert entry.low_confidence is True


# ---------------------------------------------------------------------------
# _rss_entry_to_entry：feedparser 条目转换 + low_confidence 判定
# ---------------------------------------------------------------------------

def test_rss_entry_to_entry_complete_fields():
    raw = {
        "link": "https://example.com/a",
        "title": "标题",
        "summary": "摘要",
        "published_parsed": (2026, 7, 1, 9, 0, 0, 0, 0, 0),
        "id": "guid-1",
    }
    entry = _rss_entry_to_entry(raw, source_name="openai-rss")
    assert entry.published == date(2026, 7, 1)
    assert entry.low_confidence is False


def test_rss_entry_to_entry_missing_summary_is_low_confidence():
    raw = {"link": "https://example.com/a", "title": "标题", "published_parsed": (2026, 7, 1, 0, 0, 0, 0, 0, 0)}
    entry = _rss_entry_to_entry(raw, source_name="openai-rss")
    assert entry.low_confidence is True


def test_rss_entry_to_entry_future_date_is_low_confidence():
    far_future = date.today() + timedelta(days=10)
    raw = {
        "link": "https://example.com/a",
        "title": "标题",
        "summary": "摘要",
        "published_parsed": (far_future.year, far_future.month, far_future.day, 0, 0, 0, 0, 0, 0),
    }
    entry = _rss_entry_to_entry(raw, source_name="openai-rss")
    assert entry.low_confidence is True


# ---------------------------------------------------------------------------
# _parse_a16z_list：列表页正则解析（无结构化 API，最容易随页面改版失效）
# ---------------------------------------------------------------------------

_A16Z_LIST_SAMPLE = """
<div class="feed">
  <div data-feed-item="1"><a href="https://a16z.com/posts/great-article/">
    Great Article Title</a> <span class="cat">AI</span></div>
  <div data-feed-item="2"><a href="https://a16z.com/author/jane-doe/">Jane Doe</a></div>
  <div data-feed-item="3"><a href="https://a16z.com/posts/second-article/">Second Article</a></div>
  <div data-feed-item="4"><a href="https://a16z.com/posts/great-article/">Duplicate Entry</a></div>
</div>
"""


def test_parse_a16z_list_extracts_title_and_url():
    entries = _parse_a16z_list(_A16Z_LIST_SAMPLE, limit=20)
    urls = [e["url"] for e in entries]
    assert "https://a16z.com/posts/great-article" in urls
    assert "https://a16z.com/posts/second-article" in urls


def test_parse_a16z_list_skips_author_pages():
    entries = _parse_a16z_list(_A16Z_LIST_SAMPLE, limit=20)
    assert all("/author/" not in e["url"] for e in entries)


def test_parse_a16z_list_dedupes_repeated_urls():
    entries = _parse_a16z_list(_A16Z_LIST_SAMPLE, limit=20)
    urls = [e["url"] for e in entries]
    assert len(urls) == len(set(urls))


def test_parse_a16z_list_respects_limit():
    entries = _parse_a16z_list(_A16Z_LIST_SAMPLE, limit=1)
    assert len(entries) == 1


# ---------------------------------------------------------------------------
# preflight_activity：last_verified 陈旧告警 + reliability 首次播种
# ---------------------------------------------------------------------------

class _FakeSourceConfig:
    def __init__(self, last_verified: date, reliability: str = "healthy"):
        self.last_verified = last_verified
        self.reliability = reliability


def test_preflight_activity_flags_stale_source(mocker):
    old_date = date.today() - timedelta(days=40)
    mocker.patch.object(fetch, "get_source", return_value=_FakeSourceConfig(old_date))
    mocker.patch.object(fetch, "get_source_reliability", return_value="healthy")

    result = preflight_activity("some-source")
    assert result.stale is True


def test_preflight_activity_not_stale_within_30_days(mocker):
    recent_date = date.today() - timedelta(days=10)
    mocker.patch.object(fetch, "get_source", return_value=_FakeSourceConfig(recent_date))
    mocker.patch.object(fetch, "get_source_reliability", return_value="healthy")

    result = preflight_activity("some-source")
    assert result.stale is False


def test_preflight_activity_seeds_reliability_on_first_sight(mocker):
    mocker.patch.object(fetch, "get_source", return_value=_FakeSourceConfig(date.today(), reliability="tier1"))
    mocker.patch.object(fetch, "get_source_reliability", return_value=None)
    init_source_health = mocker.patch.object(fetch, "init_source_health")

    result = preflight_activity("new-source")
    init_source_health.assert_called_once_with("new-source", "tier1")
    assert result.reliability == "tier1"

"""pytest 公共 fixture。

本目录下测试只覆盖纯逻辑分支（M5 起 aggregate/write 引入跨批次累积状态，
用真实 Temporal 批次验证的边际成本变高，见 .claude/memory/decisions.md）。
全部 mock 掉 worker.db 的数据库函数与 worker.llm_client.call_structured，
不连真实 Postgres/LiteLLM 网关——这类真实环境验证仍然保留，但作为最终验收
手段单独跑，不是每次 `pytest` 都要连基础设施。
"""

from __future__ import annotations


def make_enriched_article(**overrides) -> dict:
    """构造一条 articles 表行的 dict 形态（aggregate_activity 的典型输入单元），
    覆盖常用字段的合理默认值，测试里按需覆盖。
    """
    base = {
        "url": "https://example.com/a",
        "source_name": "openai-rss",
        "batch_id": "test-batch",
        "fetched_title": "Example Title",
        "fetched_summary": "example summary",
        "original_text": "原文内容",
        "translation_needed": False,
        "translated_title": None,
        "translated_summary": None,
        "gist": "这是一段测试摘要。",
        "content_hash": "deadbeef",
        "fetch_channel": "direct",
        "published_at": None,
        "entities": [],
        "content_type": "industry_news",
        "novelty_signal": {"keywords": []},
        "word_count": 100,
        "translation_fallback_notice": None,
    }
    base.update(overrides)
    return base

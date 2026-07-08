"""AInewsPipelineWorkflow / EnrichArticleWorkflow 编排逻辑测试。

用 Temporal 官方推荐的 time-skipping WorkflowEnvironment（不是纯 mock）——这两个
workflow 本身没有可抽取的纯函数逻辑，全部是 `workflow.execute_activity`/
`execute_child_workflow` 调用序列，只有真正驱动一次 workflow 执行才能验证：

1. filter_activity 必须在 per-article fan-out（EnrichArticleWorkflow）之前完成
   （04 §2.3 硬约束，见 .claude/memory/known_issues.md 里"该测但没测"的缺口）。
2. 单个信息源 fetch 失败 / 单篇文章 enrich 失败都不应影响其余源/文章（04 §2.1/§2.4
   设计初衷）。

真实的 activity 实现（连数据库/LLM 网关）全部替换成同名的轻量测试替身——
Temporal 按活动类型名（而非 Python 对象本身）路由，注册同名替身即可让
workflows.py 里 import 的真实函数引用透明地路由到这里的替身。
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from temporalio import activity
from temporalio.client import Client
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from worker.schemas import Entry, EnrichArticleParams, PipelineParams
from worker.workflows import AInewsPipelineWorkflow, ArxivFulltextBackfillWorkflow, EnrichArticleWorkflow

TASK_QUEUE = "test-task-queue"


def _make_entry(url: str, source_name: str = "openai-rss") -> Entry:
    return Entry(title="标题", url=url, source_name=source_name, published=date(2026, 7, 1))


@pytest.fixture
async def env():
    async with await WorkflowEnvironment.start_time_skipping(data_converter=pydantic_data_converter) as e:
        yield e


async def _client(env: WorkflowEnvironment) -> Client:
    return env.client


class _Recorder:
    """跨多个 mock activity 共享的调用顺序记录器（同一测试进程内闭包引用）。"""

    def __init__(self) -> None:
        self.calls: list[str] = []


async def test_enrich_article_workflow_skips_translation_when_not_needed(env):
    recorder = _Recorder()

    @activity.defn(name="fetch_original_activity")
    async def fake_fetch(url: str) -> dict:
        recorder.calls.append(f"fetch:{url}")
        return {"body_md": "这是一段不需要翻译的中文正文。", "fetch_channel": "direct", "arxiv_fulltext_pending": None}

    @activity.defn(name="translate_activity")
    async def fake_translate(title: str, body: str) -> dict:
        recorder.calls.append("translate")
        return {"translated_title": None, "translated_body_md": None, "translation_fallback_notice": None}

    @activity.defn(name="gist_activity")
    async def fake_gist(title: str, body: str) -> str:
        recorder.calls.append("gist")
        return "一句话摘要"

    @activity.defn(name="metadata_activity")
    async def fake_metadata(title: str, body: str) -> dict:
        recorder.calls.append("metadata")
        return {"entities": [], "content_type": "industry_news", "novelty_keywords": []}

    @activity.defn(name="upsert_article_activity")
    async def fake_upsert(payload: dict) -> None:
        recorder.calls.append("upsert")
        assert payload["translation_needed"] is False
        assert payload["translated_title"] is None

    async with Worker(
        env.client,
        task_queue=TASK_QUEUE,
        workflows=[EnrichArticleWorkflow],
        activities=[fake_fetch, fake_translate, fake_gist, fake_metadata, fake_upsert],
    ):
        await env.client.execute_workflow(
            EnrichArticleWorkflow.run,
            EnrichArticleParams(entry=_make_entry("https://example.com/a"), batch_id="b1"),
            id="enrich-1",
            task_queue=TASK_QUEUE,
        )

    assert "translate" not in recorder.calls
    assert recorder.calls == ["fetch:https://example.com/a", "gist", "metadata", "upsert"]


def _pipeline_activities(recorder: _Recorder, *, failing_source: str | None = None, failing_url: str | None = None):
    """组装 AInewsPipelineWorkflow + EnrichArticleWorkflow 全链路需要的 mock activities。"""

    @activity.defn(name="list_active_sources_activity")
    async def list_sources() -> list[str]:
        return ["source-a", "source-b"]

    @activity.defn(name="preflight_activity")
    async def preflight(source_name: str) -> dict:
        return {"source_name": source_name, "reliability": "healthy", "stale": False}

    @activity.defn(name="fetch_activity")
    async def fetch(source_name: str) -> list[Entry]:
        recorder.calls.append(f"fetch_activity:{source_name}")
        if source_name == failing_source:
            raise RuntimeError(f"{source_name} 抓取失败（模拟）")
        return [_make_entry(f"https://example.com/{source_name}", source_name)]

    @activity.defn(name="record_source_health_activity")
    async def record_health(source_name: str, ok: bool, error: str | None) -> None:
        recorder.calls.append(f"record_health:{source_name}:{ok}")

    @activity.defn(name="filter_activity")
    async def filter_(entries: list[Entry], batch_id: str) -> list[Entry]:
        recorder.calls.append("filter_activity")
        return entries

    @activity.defn(name="fetch_original_activity")
    async def fetch_original(url: str) -> dict:
        recorder.calls.append(f"fetch_original:{url}")
        if url == failing_url:
            raise RuntimeError(f"{url} 原文抓取持续失败（模拟）")
        return {"body_md": "这是一段中文正文，不需要翻译。", "fetch_channel": "direct", "arxiv_fulltext_pending": None}

    @activity.defn(name="translate_activity")
    async def translate(title: str, body: str) -> dict:
        return {"translated_title": None, "translated_body_md": None, "translation_fallback_notice": None}

    @activity.defn(name="gist_activity")
    async def gist(title: str, body: str) -> str:
        return "摘要"

    @activity.defn(name="metadata_activity")
    async def metadata(title: str, body: str) -> dict:
        return {"entities": [], "content_type": "industry_news", "novelty_keywords": []}

    @activity.defn(name="upsert_article_activity")
    async def upsert_article(payload: dict) -> None:
        recorder.calls.append("upsert_article")

    @activity.defn(name="aggregate_activity")
    async def aggregate(batch_id: str) -> dict:
        # 2026-07-06 起 aggregate_activity 内部直接调用 write_activity（不再是
        # workflow 调度的独立 activity，见 aggregate.py 顶部说明），mock 版本只需要
        # 反映新的返回形状。
        recorder.calls.append("aggregate_activity")
        return {"written": 0, "new_topics": 0, "new_zettels": 0}

    return [
        list_sources, preflight, fetch, record_health, filter_,
        fetch_original, translate, gist, metadata, upsert_article,
        aggregate,
    ]


async def test_filter_activity_runs_before_enrich_fanout(env):
    recorder = _Recorder()
    activities = _pipeline_activities(recorder)

    async with Worker(
        env.client,
        task_queue=TASK_QUEUE,
        workflows=[AInewsPipelineWorkflow, EnrichArticleWorkflow],
        activities=activities,
    ):
        await env.client.execute_workflow(
            AInewsPipelineWorkflow.run,
            PipelineParams(batch_id="batch-order"),
            id="pipeline-order",
            task_queue=TASK_QUEUE,
            execution_timeout=timedelta(seconds=30),
        )

    filter_index = recorder.calls.index("filter_activity")
    fetch_original_indices = [i for i, c in enumerate(recorder.calls) if c.startswith("fetch_original:")]
    assert fetch_original_indices, "应该至少触发过一次 fetch_original_activity"
    assert filter_index < min(fetch_original_indices), (
        f"filter_activity 必须在 per-article fan-out 前完成，实际调用顺序：{recorder.calls}"
    )


async def test_single_source_failure_does_not_block_other_sources(env):
    recorder = _Recorder()
    activities = _pipeline_activities(recorder, failing_source="source-a")

    async with Worker(
        env.client,
        task_queue=TASK_QUEUE,
        workflows=[AInewsPipelineWorkflow, EnrichArticleWorkflow],
        activities=activities,
    ):
        result = await env.client.execute_workflow(
            AInewsPipelineWorkflow.run,
            PipelineParams(batch_id="batch-source-fail"),
            id="pipeline-source-fail",
            task_queue=TASK_QUEUE,
            execution_timeout=timedelta(seconds=30),
        )

    assert result["sources_attempted"] == 2
    assert result["sources_failed"] == 1
    # source-b 仍然应该成功抓取并进入后续 filter/enrich 流程
    assert any(c.startswith("fetch_original:https://example.com/source-b") for c in recorder.calls)


async def test_single_article_enrich_failure_does_not_block_others(env):
    recorder = _Recorder()
    failing_url = "https://example.com/source-a"
    activities = _pipeline_activities(recorder, failing_url=failing_url)

    async with Worker(
        env.client,
        task_queue=TASK_QUEUE,
        workflows=[AInewsPipelineWorkflow, EnrichArticleWorkflow],
        activities=activities,
    ):
        result = await env.client.execute_workflow(
            AInewsPipelineWorkflow.run,
            PipelineParams(batch_id="batch-enrich-fail"),
            id="pipeline-enrich-fail",
            task_queue=TASK_QUEUE,
            execution_timeout=timedelta(seconds=30),
        )

    assert result["enrich_failed"] == 1
    assert result["kept"] == 2
    # 另一篇文章（source-b）应该正常走完 upsert
    assert "upsert_article" in recorder.calls
    # aggregate（内部含 write）依然要跑完整个批次，不因为一篇文章失败而中断整条流水线
    assert "aggregate_activity" in recorder.calls
    assert result["written"] == 0  # mock aggregate 固定返回 written=0


# ---------------------------------------------------------------------------
# ArxivFulltextBackfillWorkflow：只对"现在真的有全文了"的候选走完整 enrich + 写回，
# 仍然只有摘要的候选直接跳过（不浪费翻译/摘要/元数据这几个 LLM 调用），2026-07-08 新增。
# ---------------------------------------------------------------------------

async def test_arxiv_backfill_only_processes_candidates_with_fulltext_available(env):
    recorder = _Recorder()

    @activity.defn(name="list_arxiv_fulltext_backfill_candidates_activity")
    async def list_candidates() -> list[dict]:
        return [
            {
                "doc_id": "original-ready",
                "url": "http://arxiv.org/abs/1111.11111",
                "topic_slug": "research-papers",
                "related_zettel_id": None,
                "fetched_title": "Ready Paper",
                "published_at": date(2026, 7, 1),
                "tags": ["arxiv"],
            },
            {
                "doc_id": "original-not-ready",
                "url": "http://arxiv.org/abs/2222.22222",
                "topic_slug": "research-papers",
                "related_zettel_id": None,
                "fetched_title": "Not Ready Paper",
                "published_at": date(2026, 7, 1),
                "tags": ["arxiv"],
            },
        ]

    @activity.defn(name="check_arxiv_fulltext_activity")
    async def check_fulltext(url: str) -> bool:
        recorder.calls.append(f"check:{url}")
        return url == "http://arxiv.org/abs/1111.11111"

    @activity.defn(name="fetch_original_activity")
    async def fake_fetch(url: str) -> dict:
        recorder.calls.append(f"fetch:{url}")
        return {"body_md": "这是一段中文正文，不需要翻译。", "fetch_channel": "direct", "arxiv_fulltext_pending": False}

    @activity.defn(name="translate_activity")
    async def fake_translate(title: str, body: str) -> dict:
        return {"translated_title": None, "translated_body_md": None, "translation_fallback_notice": None}

    @activity.defn(name="gist_activity")
    async def fake_gist(title: str, body: str) -> str:
        return "一句话摘要"

    @activity.defn(name="metadata_activity")
    async def fake_metadata(title: str, body: str) -> dict:
        return {"entities": [], "content_type": "research_paper", "novelty_keywords": []}

    @activity.defn(name="upsert_article_activity")
    async def fake_upsert(payload: dict) -> None:
        recorder.calls.append("upsert")

    @activity.defn(name="refresh_original_document_activity")
    async def fake_refresh(payload: dict) -> None:
        recorder.calls.append(f"refresh:{payload['doc_id']}")

    async with Worker(
        env.client,
        task_queue=TASK_QUEUE,
        workflows=[ArxivFulltextBackfillWorkflow, EnrichArticleWorkflow],
        activities=[
            list_candidates,
            check_fulltext,
            fake_fetch,
            fake_translate,
            fake_gist,
            fake_metadata,
            fake_upsert,
            fake_refresh,
        ],
    ):
        result = await env.client.execute_workflow(
            ArxivFulltextBackfillWorkflow.run,
            id="arxiv-backfill-test",
            task_queue=TASK_QUEUE,
            execution_timeout=timedelta(seconds=30),
        )

    assert result == {"checked": 2, "ready": 1, "upgraded": 1}
    # 两篇都做过便宜的可用性检查
    assert "check:http://arxiv.org/abs/1111.11111" in recorder.calls
    assert "check:http://arxiv.org/abs/2222.22222" in recorder.calls
    # 只有真的有全文的那篇走完了 enrich + 写回
    assert "fetch:http://arxiv.org/abs/1111.11111" in recorder.calls
    assert "refresh:original-ready" in recorder.calls
    # 仍然只有摘要的那篇完全没有触发 enrich（不浪费 LLM 调用）
    assert "fetch:http://arxiv.org/abs/2222.22222" not in recorder.calls
    assert "refresh:original-not-ready" not in recorder.calls


async def test_arxiv_backfill_survives_check_activity_exception(env):
    """2026-07-08 修复：可用性检查本身报错（耗尽重试后）不应该让整个 workflow 崩溃，
    也不应该被无声当成"未就绪"——该候选被排除在 ready 之外，其余候选正常处理。"""
    recorder = _Recorder()

    @activity.defn(name="list_arxiv_fulltext_backfill_candidates_activity")
    async def list_candidates() -> list[dict]:
        return [
            {
                "doc_id": "original-ready",
                "url": "http://arxiv.org/abs/1111.11111",
                "topic_slug": "research-papers",
                "related_zettel_id": None,
                "fetched_title": "Ready Paper",
                "published_at": date(2026, 7, 1),
                "tags": ["arxiv"],
            },
            {
                "doc_id": "original-check-fails",
                "url": "http://arxiv.org/abs/3333.33333",
                "topic_slug": "research-papers",
                "related_zettel_id": None,
                "fetched_title": "Flaky Check Paper",
                "published_at": date(2026, 7, 1),
                "tags": ["arxiv"],
            },
        ]

    @activity.defn(name="check_arxiv_fulltext_activity")
    async def check_fulltext(url: str) -> bool:
        if url == "http://arxiv.org/abs/3333.33333":
            raise RuntimeError("arxiv 探测请求超时")
        return True

    @activity.defn(name="fetch_original_activity")
    async def fake_fetch(url: str) -> dict:
        recorder.calls.append(f"fetch:{url}")
        return {"body_md": "这是一段中文正文，不需要翻译。", "fetch_channel": "direct", "arxiv_fulltext_pending": False}

    @activity.defn(name="translate_activity")
    async def fake_translate(title: str, body: str) -> dict:
        return {"translated_title": None, "translated_body_md": None, "translation_fallback_notice": None}

    @activity.defn(name="gist_activity")
    async def fake_gist(title: str, body: str) -> str:
        return "一句话摘要"

    @activity.defn(name="metadata_activity")
    async def fake_metadata(title: str, body: str) -> dict:
        return {"entities": [], "content_type": "research_paper", "novelty_keywords": []}

    @activity.defn(name="upsert_article_activity")
    async def fake_upsert(payload: dict) -> None:
        recorder.calls.append("upsert")

    @activity.defn(name="refresh_original_document_activity")
    async def fake_refresh(payload: dict) -> None:
        recorder.calls.append(f"refresh:{payload['doc_id']}")

    async with Worker(
        env.client,
        task_queue=TASK_QUEUE,
        workflows=[ArxivFulltextBackfillWorkflow, EnrichArticleWorkflow],
        activities=[
            list_candidates,
            check_fulltext,
            fake_fetch,
            fake_translate,
            fake_gist,
            fake_metadata,
            fake_upsert,
            fake_refresh,
        ],
    ):
        result = await env.client.execute_workflow(
            ArxivFulltextBackfillWorkflow.run,
            id="arxiv-backfill-check-exception-test",
            task_queue=TASK_QUEUE,
            execution_timeout=timedelta(seconds=30),
        )

    # 探测报错的那篇被排除在 ready 之外，但不影响另一篇正常走完 enrich + 写回；
    # workflow 本身不崩溃、正常返回。
    assert result == {"checked": 2, "ready": 1, "upgraded": 1}
    assert "refresh:original-ready" in recorder.calls
    assert "refresh:original-check-fails" not in recorder.calls


async def test_arxiv_backfill_returns_zero_when_no_candidates(env):
    @activity.defn(name="list_arxiv_fulltext_backfill_candidates_activity")
    async def list_candidates() -> list[dict]:
        return []

    async with Worker(
        env.client,
        task_queue=TASK_QUEUE,
        workflows=[ArxivFulltextBackfillWorkflow],
        activities=[list_candidates],
    ):
        result = await env.client.execute_workflow(
            ArxivFulltextBackfillWorkflow.run,
            id="arxiv-backfill-empty-test",
            task_queue=TASK_QUEUE,
            execution_timeout=timedelta(seconds=30),
        )

    assert result == {"checked": 0, "ready": 0, "upgraded": 0}

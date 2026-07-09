"""Deep Dive 周报纯逻辑分支单测（M10）。全部 mock 掉 worker.db 的数据库函数和
worker.llm_client.call_structured，不连真实 Postgres/LiteLLM 网关。
"""

from __future__ import annotations

from datetime import date

from worker import deep_dive


def make_original_row(
    *,
    doc_id: str,
    doc_date: date,
    topic_slug: str,
    title: str = "标题",
    source_name: str = "openai-rss",
    gist: str = "一段摘要",
) -> dict:
    return {
        "doc_id": doc_id,
        "doc_date": doc_date,
        "title": title,
        "topic_slug": topic_slug,
        "source_name": source_name,
        "gist": gist,
    }


# ---------------------------------------------------------------------------
# _compute_topic_trends
# ---------------------------------------------------------------------------

def test_compute_topic_trends_counts_total_and_active_days():
    """同一 topic 同一天多条文章：total_count 按条目数算，active_days 按 distinct 日期算。"""
    rows = [
        make_original_row(doc_id="o1", doc_date=date(2026, 7, 1), topic_slug="agents"),
        make_original_row(doc_id="o2", doc_date=date(2026, 7, 1), topic_slug="agents"),
        make_original_row(doc_id="o3", doc_date=date(2026, 7, 2), topic_slug="agents"),
    ]
    trends = deep_dive._compute_topic_trends(rows)
    assert len(trends) == 1
    assert trends[0]["total_count"] == 3
    assert trends[0]["active_days"] == 2


def test_compute_topic_trends_filters_below_total_count_threshold():
    """active_days 达标但 total_count 不足 3 条：不入选。"""
    rows = [
        make_original_row(doc_id="o1", doc_date=date(2026, 7, 1), topic_slug="agents"),
        make_original_row(doc_id="o2", doc_date=date(2026, 7, 2), topic_slug="agents"),
    ]
    assert deep_dive._compute_topic_trends(rows) == []


def test_compute_topic_trends_filters_below_active_days_threshold():
    """total_count 达标但全部集中在同一天（active_days=1）：不入选，排除单日爆发噪声。"""
    rows = [
        make_original_row(doc_id=f"o{i}", doc_date=date(2026, 7, 1), topic_slug="agents") for i in range(3)
    ]
    assert deep_dive._compute_topic_trends(rows) == []


def test_compute_topic_trends_excludes_uncategorized():
    """uncategorized 溢出桶不是真实主题聚类结果，不参与热门话题评选。"""
    rows = [
        make_original_row(doc_id=f"o{i}", doc_date=date(2026, 7, 1 + i), topic_slug="uncategorized")
        for i in range(3)
    ]
    assert deep_dive._compute_topic_trends(rows) == []


def test_compute_topic_trends_sorted_and_capped_at_top_8():
    """10 个都达标的 topic，按 total_count 降序只保留前 8 个。"""
    rows = []
    for i in range(10):
        slug = f"topic-{i}"
        count = 10 - i  # topic-0 最多（10条），topic-9 最少（1条，实际达不到门槛）
        for j in range(count):
            rows.append(make_original_row(doc_id=f"{slug}-{j}", doc_date=date(2026, 7, 1 + j % 3), topic_slug=slug))
    trends = deep_dive._compute_topic_trends(rows)
    assert len(trends) == deep_dive.MAX_TRENDING_TOPICS
    counts = [t["total_count"] for t in trends]
    assert counts == sorted(counts, reverse=True)
    assert trends[0]["slug"] == "topic-0"


# ---------------------------------------------------------------------------
# _select_representative_docs
# ---------------------------------------------------------------------------

def test_select_representative_docs_orders_by_recency_and_caps_limit():
    docs = [
        make_original_row(doc_id="old", doc_date=date(2026, 7, 1), topic_slug="agents"),
        make_original_row(doc_id="new", doc_date=date(2026, 7, 5), topic_slug="agents"),
        make_original_row(doc_id="mid", doc_date=date(2026, 7, 3), topic_slug="agents"),
        make_original_row(doc_id="extra", doc_date=date(2026, 7, 4), topic_slug="agents"),
    ]
    reps = deep_dive._select_representative_docs(docs, limit=3)
    assert [r["doc_id"] for r in reps] == ["new", "extra", "mid"]


# ---------------------------------------------------------------------------
# _generate_intro
# ---------------------------------------------------------------------------

def test_generate_intro_skips_llm_when_no_trending_and_no_digests(mocker):
    call_structured_mock = mocker.patch.object(deep_dive, "call_structured")
    intro = deep_dive._generate_intro([], [])
    call_structured_mock.assert_not_called()
    assert intro == deep_dive._NO_MATERIAL_INTRO


def test_generate_intro_calls_llm_with_material(mocker):
    result = mocker.Mock(intro="这是生成的导语")
    call_structured_mock = mocker.patch.object(deep_dive, "call_structured", return_value=result)
    trending = [
        {
            "slug": "agents",
            "total_count": 5,
            "active_days": 3,
            "representatives": [{"doc_id": "o1", "title": "标题1", "source_name": "openai-rss", "gist": "摘要1"}],
        }
    ]
    digests = [{"doc_id": "digest-2026-07-01", "doc_date": date(2026, 7, 1), "body_md": "今日内容"}]
    intro = deep_dive._generate_intro(trending, digests)
    assert intro == "这是生成的导语"
    call_structured_mock.assert_called_once()
    assert call_structured_mock.call_args.kwargs["response_model"] is deep_dive.DeepDiveIntro


# ---------------------------------------------------------------------------
# _compute_daily_counts
# ---------------------------------------------------------------------------

def test_compute_daily_counts_fills_gaps_with_zero():
    """窗口内没有原文的日子也要出现在结果里（count=0），保证柱状图 x 轴天数固定。"""
    rows = [
        make_original_row(doc_id="o1", doc_date=date(2026, 7, 1), topic_slug="agents"),
        make_original_row(doc_id="o2", doc_date=date(2026, 7, 1), topic_slug="agents"),
        make_original_row(doc_id="o3", doc_date=date(2026, 7, 3), topic_slug="agents"),
    ]
    result = deep_dive._compute_daily_counts(rows, date(2026, 7, 1), date(2026, 7, 4))
    assert result == [
        {"date": "2026-07-01", "count": 2},
        {"date": "2026-07-02", "count": 0},
        {"date": "2026-07-03", "count": 1},
        {"date": "2026-07-04", "count": 0},
    ]


# ---------------------------------------------------------------------------
# 图表机械生成（不经过 LLM）
# ---------------------------------------------------------------------------

def test_build_trend_pie_chart_uses_real_counts_not_llm():
    """饼图机械拼接自结构化统计数据，不经过 LLM——数值必须和输入完全对应。"""
    trending = [
        {"slug": "agents", "total_count": 19, "active_days": 5, "representatives": []},
        {"slug": "unknown-slug", "total_count": 3, "active_days": 2, "representatives": []},
    ]
    chart = deep_dive._build_trend_pie_chart(trending)
    assert chart.startswith("```mermaid\npie showData\n")
    assert chart.endswith("```")
    assert '"🤖 Agent" : 19' in chart
    # 未知 slug（不在 TOPIC_LABEL 映射表里）应回退用 slug 本身作 label，不报错
    assert '"📌 unknown-slug" : 3' in chart


def test_build_daily_volume_bar_chart_uses_real_counts_not_llm():
    daily_counts = [
        {"date": "2026-07-01", "count": 10},
        {"date": "2026-07-02", "count": 0},
        {"date": "2026-07-03", "count": 5},
    ]
    chart = deep_dive._build_daily_volume_bar_chart(daily_counts)
    assert chart.startswith("```mermaid\nxychart-beta\n")
    assert chart.endswith("```")
    assert '["07-01", "07-02", "07-03"]' in chart
    assert "bar [10, 0, 5]" in chart


def test_build_trend_quadrant_chart_normalizes_coordinates():
    """x = active_days/WINDOW_DAYS，y = total_count/本批次最大 total_count，不经过 LLM。
    整数坐标（如最大值算出的 1.0）必须格式化成不带小数点的 "1"——mermaid quadrantChart
    真实解析器无法处理 "1.0" 这种字面量（见 _format_quadrant_coord 的说明）。"""
    trending = [
        {"slug": "agents", "total_count": 20, "active_days": 7, "representatives": []},
        {"slug": "model-releases", "total_count": 10, "active_days": 2, "representatives": []},
    ]
    chart = deep_dive._build_trend_quadrant_chart(trending)
    assert chart.startswith("```mermaid\nquadrantChart\n")
    assert chart.endswith("```")
    assert '"Agent" : [1, 1]' in chart
    assert '"1.0"' not in chart
    assert f'"模型发布" : [{round(2 / deep_dive.WINDOW_DAYS, 2)}, 0.5]' in chart


def test_format_quadrant_coord_avoids_trailing_zero_float_literal():
    """mermaid quadrantChart 词法分析器实测无法解析 "1.0"/"0.0" 这类字面量，
    整数值必须格式化成裸整数，非整数值保持小数不变。"""
    assert deep_dive._format_quadrant_coord(1.0) == "1"
    assert deep_dive._format_quadrant_coord(0.0) == "0"
    assert deep_dive._format_quadrant_coord(0.86) == "0.86"
    assert deep_dive._format_quadrant_coord(0.5) == "0.5"


# ---------------------------------------------------------------------------
# _build_deep_dive_record
# ---------------------------------------------------------------------------

def test_build_deep_dive_record_omits_all_charts_when_no_data():
    """0 命中热门 topic 且窗口内没有任何原文时不应该插入任何空图表（没有数据可画）。"""
    record = deep_dive._build_deep_dive_record(date(2026, 7, 1), date(2026, 7, 7), [], 0, [], "导语", [])
    assert "```mermaid" not in record["body_md"]


def test_build_deep_dive_record_includes_bar_and_pie_but_not_quadrant_for_single_topic():
    """只有 1 个热门 topic 时象限图没有对比意义（归一化后 y 必为 1.0），只画柱状图+饼图。"""
    trending = [
        {
            "slug": "agents",
            "total_count": 5,
            "active_days": 3,
            "representatives": [{"doc_id": "o1", "title": "t", "source_name": "s", "gist": "g"}],
        }
    ]
    daily_counts = [{"date": "2026-07-01", "count": 5}]
    record = deep_dive._build_deep_dive_record(date(2026, 7, 1), date(2026, 7, 7), trending, 5, [], "导语", daily_counts)
    assert "xychart-beta" in record["body_md"]
    assert "pie showData" in record["body_md"]
    assert "quadrantChart" not in record["body_md"]


def test_build_deep_dive_record_includes_all_three_charts_for_multiple_topics():
    trending = [
        {
            "slug": "agents",
            "total_count": 5,
            "active_days": 3,
            "representatives": [{"doc_id": "o1", "title": "t", "source_name": "s", "gist": "g"}],
        },
        {
            "slug": "model-releases",
            "total_count": 3,
            "active_days": 2,
            "representatives": [{"doc_id": "o2", "title": "t2", "source_name": "s", "gist": "g"}],
        },
    ]
    daily_counts = [{"date": "2026-07-01", "count": 8}]
    record = deep_dive._build_deep_dive_record(date(2026, 7, 1), date(2026, 7, 7), trending, 8, [], "导语", daily_counts)
    assert "xychart-beta" in record["body_md"]
    assert "pie showData" in record["body_md"]
    assert "quadrantChart" in record["body_md"]


def test_build_deep_dive_record_doc_id_and_doc_date():
    window_start, window_end = date(2026, 7, 1), date(2026, 7, 7)
    record = deep_dive._build_deep_dive_record(window_start, window_end, [], 0, [], "导语", [])
    assert record["doc_id"] == "deep-dive-2026-07-07"
    assert record["doc_date"] == window_end
    assert record["doc_type"] == "deep_dive"


def test_build_deep_dive_record_zero_trending_still_produces_document():
    """命中 0 个热门 topic 也要正常产出文档，不能跳过整周。"""
    window_start, window_end = date(2026, 7, 1), date(2026, 7, 7)
    record = deep_dive._build_deep_dive_record(window_start, window_end, [], 12, [], "导语", [])
    assert deep_dive._NO_TREND_NOTE in record["body_md"]
    assert record["frontmatter"]["trending_topics"] == []
    assert record["link_targets"] == []


def test_build_deep_dive_record_link_targets_exclude_digest_ids():
    """source_digest_ids 只进 frontmatter 做可追溯字段，不进 link_targets（Digest 本身
    明确不用 wikilink，给它建反链边是死边）。"""
    trending = [
        {
            "slug": "agents",
            "total_count": 5,
            "active_days": 3,
            "representatives": [{"doc_id": "original-abc", "title": "t", "source_name": "s", "gist": "g"}],
        }
    ]
    digests = [{"doc_id": "digest-2026-07-01", "doc_date": date(2026, 7, 1), "body_md": "内容"}]
    record = deep_dive._build_deep_dive_record(date(2026, 7, 1), date(2026, 7, 7), trending, 5, digests, "导语", [])
    assert record["frontmatter"]["source_digest_ids"] == ["digest-2026-07-01"]
    assert "digest-2026-07-01" not in record["link_targets"]
    assert record["link_targets"] == ["agents", "original-abc"]


# ---------------------------------------------------------------------------
# activity 入口
# ---------------------------------------------------------------------------

def test_compute_deep_dive_trends_activity_derives_window_and_returns_summary(mocker):
    rows = [
        make_original_row(doc_id="o1", doc_date=date(2026, 7, 1), topic_slug="agents"),
        make_original_row(doc_id="o2", doc_date=date(2026, 7, 3), topic_slug="agents"),
        make_original_row(doc_id="o3", doc_date=date(2026, 7, 5), topic_slug="agents"),
    ]
    list_mock = mocker.patch.object(deep_dive, "deep_dive_list_original_documents_in_window", return_value=rows)

    result = deep_dive.compute_deep_dive_trends_activity(date(2026, 7, 7))

    list_mock.assert_called_once_with(date(2026, 7, 1), date(2026, 7, 7))
    assert result["window_start"] == date(2026, 7, 1)
    assert result["window_end"] == date(2026, 7, 7)
    assert result["entry_count"] == 3
    assert len(result["trending"]) == 1
    assert result["trending"][0]["slug"] == "agents"
    assert len(result["trending"][0]["representatives"]) == 3
    assert result["daily_counts"] == [
        {"date": "2026-07-01", "count": 1},
        {"date": "2026-07-02", "count": 0},
        {"date": "2026-07-03", "count": 1},
        {"date": "2026-07-04", "count": 0},
        {"date": "2026-07-05", "count": 1},
        {"date": "2026-07-06", "count": 0},
        {"date": "2026-07-07", "count": 0},
    ]


def test_generate_deep_dive_activity_coerces_string_window_dates(mocker):
    """回归测试：真实 Temporal 执行时 compute_deep_dive_trends_activity 的 dict 返回值
    经序列化后，window_start/window_end 会退化成 ISO 字符串（未指定字段类型的 dict
    没有类型信息可依据解码回 date），generate_deep_dive_activity 必须能兼容两种输入，
    不能在 _build_deep_dive_record 调用 .isoformat() 时报 AttributeError。"""
    mocker.patch.object(deep_dive, "deep_dive_list_digest_documents_in_window", return_value=[])
    mocker.patch.object(deep_dive, "call_structured")
    write_activity_mock = mocker.patch.object(deep_dive, "write_activity", return_value=1)

    payload = {
        "window_start": "2026-07-01",
        "window_end": "2026-07-07",
        "entry_count": 0,
        "trending": [],
        "daily_counts": [],
    }
    result = deep_dive.generate_deep_dive_activity(payload)

    written_records = write_activity_mock.call_args[0][0]
    assert written_records[0]["doc_id"] == "deep-dive-2026-07-07"
    assert result["doc_id"] == "deep-dive-2026-07-07"


def test_generate_deep_dive_activity_writes_single_record(mocker):
    mocker.patch.object(deep_dive, "deep_dive_list_digest_documents_in_window", return_value=[])
    mocker.patch.object(deep_dive, "call_structured")
    write_activity_mock = mocker.patch.object(deep_dive, "write_activity", return_value=1)

    payload = {
        "window_start": date(2026, 7, 1),
        "window_end": date(2026, 7, 7),
        "entry_count": 0,
        "trending": [],
        "daily_counts": [],
    }
    result = deep_dive.generate_deep_dive_activity(payload)

    write_activity_mock.assert_called_once()
    written_records = write_activity_mock.call_args[0][0]
    assert len(written_records) == 1
    assert written_records[0]["doc_id"] == "deep-dive-2026-07-07"
    assert result == {"written": 1, "doc_id": "deep-dive-2026-07-07", "trending_topic_count": 0}

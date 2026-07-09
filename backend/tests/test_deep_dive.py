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


def make_zettel_row(
    *,
    doc_id: str,
    doc_date: date,
    title: str = "笔记标题",
    gist: str = "一段摘要",
    original_id: str = "original-abc",
) -> dict:
    return {"doc_id": doc_id, "doc_date": doc_date, "title": title, "gist": gist, "original_id": original_id}


def make_fulltext_row(*, doc_id: str, doc_date: date, title: str = "原文标题", body_md: str = "原文全文内容") -> dict:
    return {"doc_id": doc_id, "doc_date": doc_date, "title": title, "body_md": body_md}


def make_analysis(
    *,
    deep_summary: str = "深度总结",
    continuity: str = "延续性",
    cross_validation: str = "交叉验证",
    tensions: str = "分歧",
    emerging: str = "新兴信号",
    relationships: list[dict] | None = None,
) -> dict:
    return {
        "deep_summary": deep_summary,
        "continuity": continuity,
        "cross_validation": cross_validation,
        "tensions": tensions,
        "emerging": emerging,
        "relationships": relationships or [],
    }


def make_analysis_result_mock(mocker, **kwargs):
    """构造 call_structured 返回值的 mock，字段对齐 TopicNarrativeAnalysis。"""
    defaults = dict(
        deep_summary="深度总结", continuity="延续性", cross_validation="交叉验证", tensions="分歧",
        emerging="新兴信号", relationships=[],
    )
    defaults.update(kwargs)
    return mocker.Mock(**defaults)


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
            "zettels": [],
            "fulltext_ids": [],
            "analysis": make_analysis(),
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
            "zettels": [],
            "fulltext_ids": [],
            "analysis": make_analysis(),
        },
        {
            "slug": "model-releases",
            "total_count": 3,
            "active_days": 2,
            "representatives": [{"doc_id": "o2", "title": "t2", "source_name": "s", "gist": "g"}],
            "zettels": [],
            "fulltext_ids": [],
            "analysis": make_analysis(),
        },
    ]
    daily_counts = [{"date": "2026-07-01", "count": 8}]
    record = deep_dive._build_deep_dive_record(date(2026, 7, 1), date(2026, 7, 7), trending, 8, [], "导语", daily_counts)
    assert "xychart-beta" in record["body_md"]
    assert "pie showData" in record["body_md"]
    assert "quadrantChart" in record["body_md"]
    # 每个热门 topic 都应该有五维度深度分析小节，不是机械 bullet 列表
    assert "**延续性**：延续性" in record["body_md"]
    assert "**交叉验证**：交叉验证" in record["body_md"]
    assert "**分歧**：分歧" in record["body_md"]
    assert "**新兴信号**：新兴信号" in record["body_md"]


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
            "zettels": [],
            "fulltext_ids": [],
            "analysis": make_analysis(),
        }
    ]
    digests = [{"doc_id": "digest-2026-07-01", "doc_date": date(2026, 7, 1), "body_md": "内容"}]
    record = deep_dive._build_deep_dive_record(date(2026, 7, 1), date(2026, 7, 7), trending, 5, digests, "导语", [])
    assert record["frontmatter"]["source_digest_ids"] == ["digest-2026-07-01"]
    assert "digest-2026-07-01" not in record["link_targets"]
    assert record["link_targets"] == ["agents", "original-abc"]


def test_build_deep_dive_record_link_targets_include_cited_analysis_material():
    """五维度分析正文里引用到的候选素材（zettel/深挖原文）应该进 link_targets，未引用
    的和编造的（不在候选素材里的）都不应该进。"""
    zettels = [make_zettel_row(doc_id="z1", doc_date=date(2026, 7, 1))]
    trending = [
        {
            "slug": "agents",
            "total_count": 5,
            "active_days": 3,
            "representatives": [],
            "zettels": zettels,
            "fulltext_ids": ["original-x"],
            "analysis": make_analysis(
                deep_summary="本周关注 [[z1]] 的进展",
                cross_validation="[[original-x]] 与 [[z1]] 相互印证",
                emerging="[[not-in-material]] 是编造的引用",
            ),
        }
    ]
    record = deep_dive._build_deep_dive_record(date(2026, 7, 1), date(2026, 7, 7), trending, 5, [], "导语", [])
    assert record["link_targets"] == ["agents", "z1", "original-x"]


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


def test_generate_deep_dive_activity_generates_per_topic_analysis(mocker):
    """2026-07-09 深度改版：每个热门 topic 独立查 zettel/上周同期zettel/深挖全文，调
    _generate_topic_analysis 生成深度分析，取代早期机械 bullet 列表。验证每个 topic 都
    触发了一次 LLM 调用（1次整体导语 + N次topic分析），且深挖全文篇数上限用周报专属的
    WEEKLY_TOPIC_FULLTEXT_LIMIT（不是月报每子主题的 CLUSTER_FULLTEXT_LIMIT）。"""
    mocker.patch.object(deep_dive, "deep_dive_list_digest_documents_in_window", return_value=[])
    zettels = [make_zettel_row(doc_id=f"z{i}", doc_date=date(2026, 7, 1 + i), original_id=f"original-{i}") for i in range(8)]
    zettel_mock = mocker.patch.object(deep_dive, "topic_deep_dive_list_zettel_documents_in_window", return_value=zettels)
    fulltext_mock = mocker.patch.object(deep_dive, "topic_deep_dive_fetch_original_fulltext", return_value=[])
    # 第 1 次调用是 _generate_intro（要求 .intro 字段），后续每个热门 topic 各一次是
    # _generate_topic_analysis（要求 .deep_summary/.continuity 等字段）——两种 mock 形状不同，
    # 用 side_effect 按调用顺序区分，不能用同一个 return_value 糊弄过去。
    call_structured_mock = mocker.patch.object(
        deep_dive,
        "call_structured",
        side_effect=[mocker.Mock(intro="整体导语"), make_analysis_result_mock(mocker)],
    )
    mocker.patch.object(deep_dive, "write_activity", return_value=1)

    payload = {
        "window_start": date(2026, 7, 1),
        "window_end": date(2026, 7, 7),
        "entry_count": 10,
        "trending": [
            {
                "slug": "agents",
                "total_count": 5,
                "active_days": 3,
                "representatives": [{"doc_id": "o1", "title": "t", "source_name": "s", "gist": "g"}],
            }
        ],
        "daily_counts": [],
    }
    deep_dive.generate_deep_dive_activity(payload)

    # 1 次整体导语调用 + 1 次该热门 topic 的深度分析调用
    assert call_structured_mock.call_count == 2
    # 该 topic 当前窗口 + 上周同期窗口，各查一次 zettel
    assert zettel_mock.call_count == 2
    zettel_mock.assert_any_call("agents", date(2026, 7, 1), date(2026, 7, 7))
    zettel_mock.assert_any_call("agents", date(2026, 6, 24), date(2026, 6, 30))
    # 深挖全文篇数上限用周报专属的 5，不是月报的 10
    called_ids = fulltext_mock.call_args[0][0]
    assert len(called_ids) == deep_dive.WEEKLY_TOPIC_FULLTEXT_LIMIT


# ===========================================================================
# 专题月报（M11）：固定 1 个 topic 桶 × 自然月窗口的纵向深挖，正交于上面的周报
# ===========================================================================

# ---------------------------------------------------------------------------
# _compute_monthly_topic_candidates
# ---------------------------------------------------------------------------

def test_compute_monthly_topic_candidates_counts_total_and_active_days():
    rows = [
        make_original_row(doc_id="o1", doc_date=date(2026, 7, 1), topic_slug="agents"),
        make_original_row(doc_id="o2", doc_date=date(2026, 7, 1), topic_slug="agents"),
        make_original_row(doc_id="o3", doc_date=date(2026, 7, 2), topic_slug="agents"),
        make_original_row(doc_id="o4", doc_date=date(2026, 7, 3), topic_slug="agents"),
        make_original_row(doc_id="o5", doc_date=date(2026, 7, 4), topic_slug="agents"),
        make_original_row(doc_id="o6", doc_date=date(2026, 7, 5), topic_slug="agents"),
        make_original_row(doc_id="o7", doc_date=date(2026, 7, 5), topic_slug="agents"),
        make_original_row(doc_id="o8", doc_date=date(2026, 7, 5), topic_slug="agents"),
    ]
    candidates = deep_dive._compute_monthly_topic_candidates(rows)
    assert len(candidates) == 1
    assert candidates[0] == {"slug": "agents", "total_count": 8, "active_days": 5}


def test_compute_monthly_topic_candidates_filters_below_total_count_threshold():
    """active_days 达标（4天）但 total_count 只有 4 条，不满足 MONTHLY_MIN_TOTAL_COUNT=8。"""
    rows = [
        make_original_row(doc_id=f"o{i}", doc_date=date(2026, 7, 1 + i), topic_slug="agents") for i in range(4)
    ]
    assert deep_dive._compute_monthly_topic_candidates(rows) == []


def test_compute_monthly_topic_candidates_filters_below_active_days_threshold():
    """total_count 达标（8条）但集中在 2 天，不满足 MONTHLY_MIN_ACTIVE_DAYS=4。"""
    rows = [make_original_row(doc_id=f"o{i}", doc_date=date(2026, 7, 1 + i % 2), topic_slug="agents") for i in range(8)]
    assert deep_dive._compute_monthly_topic_candidates(rows) == []


def test_compute_monthly_topic_candidates_excludes_uncategorized():
    rows = [
        make_original_row(doc_id=f"o{i}", doc_date=date(2026, 7, 1 + i), topic_slug="uncategorized") for i in range(8)
    ]
    assert deep_dive._compute_monthly_topic_candidates(rows) == []


def test_compute_monthly_topic_candidates_no_top_n_cap():
    """月报不像周报那样截断 top-8——预设 topic 桶数量本身有限，fan-out 规模天然有界；
    构造 9 个都达标的 topic，全部应该入选，不截断。"""
    rows = []
    for i in range(9):
        slug = f"topic-{i}"
        for j in range(8):
            rows.append(make_original_row(doc_id=f"{slug}-{j}", doc_date=date(2026, 7, 1 + j % 4), topic_slug=slug))
    candidates = deep_dive._compute_monthly_topic_candidates(rows)
    assert len(candidates) == 9


def test_compute_monthly_topic_candidates_sorted_descending():
    rows = []
    for i, count in enumerate([8, 12, 10]):
        slug = f"topic-{i}"
        for j in range(count):
            rows.append(make_original_row(doc_id=f"{slug}-{j}", doc_date=date(2026, 7, 1 + j % 4), topic_slug=slug))
    candidates = deep_dive._compute_monthly_topic_candidates(rows)
    assert [c["total_count"] for c in candidates] == [12, 10, 8]


# ---------------------------------------------------------------------------
# _select_monthly_zettel_material
# ---------------------------------------------------------------------------

def test_select_monthly_zettel_material_orders_by_recency_and_caps_limit():
    zettels = [make_zettel_row(doc_id=f"z{i}", doc_date=date(2026, 7, 1 + i)) for i in range(5)]
    selected = deep_dive._select_monthly_zettel_material(zettels, limit=3)
    assert [z["doc_id"] for z in selected] == ["z4", "z3", "z2"]


def test_select_monthly_zettel_material_no_truncation_when_under_limit():
    zettels = [make_zettel_row(doc_id="z1", doc_date=date(2026, 7, 1))]
    assert deep_dive._select_monthly_zettel_material(zettels, limit=30) == zettels


# ---------------------------------------------------------------------------
# _select_fulltext_original_ids
# ---------------------------------------------------------------------------

def test_select_fulltext_original_ids_orders_by_zettel_recency_and_dedupes():
    zettels = [
        make_zettel_row(doc_id="z1", doc_date=date(2026, 7, 1), original_id="original-a"),
        make_zettel_row(doc_id="z2", doc_date=date(2026, 7, 5), original_id="original-b"),
        make_zettel_row(doc_id="z3", doc_date=date(2026, 7, 3), original_id="original-a"),  # 重复 original_id
    ]
    ids = deep_dive._select_fulltext_original_ids(zettels, limit=10)
    assert ids == ["original-b", "original-a"]


def test_select_fulltext_original_ids_caps_at_limit():
    zettels = [make_zettel_row(doc_id=f"z{i}", doc_date=date(2026, 7, 1 + i), original_id=f"original-{i}") for i in range(15)]
    ids = deep_dive._select_fulltext_original_ids(zettels, limit=10)
    assert len(ids) == 10
    assert ids[0] == "original-14"  # 最新的 zettel 对应的 original 排在最前


# ---------------------------------------------------------------------------
# _cluster_topic_articles（2026-07-09 深度改版二：子主题聚类，取代 zettel 门控素材）
# ---------------------------------------------------------------------------

def make_cluster_mock(mocker, heading: str, doc_ids: list[str]):
    return mocker.Mock(heading=heading, doc_ids=doc_ids)


def test_cluster_topic_articles_returns_empty_when_no_articles(mocker):
    call_structured_mock = mocker.patch.object(deep_dive, "call_structured")
    assert deep_dive._cluster_topic_articles("agents", []) == []
    call_structured_mock.assert_not_called()


def test_cluster_topic_articles_calls_llm_with_all_articles_not_zettel_gated(mocker):
    """聚类素材是该 topic 本月全部原文（title+gist），不是 zettel 命中的子集——这是
    这次改版的核心：zettel 不是深挖池的准入门槛。"""
    cluster_mock = make_cluster_mock(mocker, "线索A", ["o1", "o2"])
    result_mock = mocker.Mock(clusters=[cluster_mock])
    call_structured_mock = mocker.patch.object(deep_dive, "call_structured", return_value=result_mock)

    articles = [
        make_original_row(doc_id="o1", doc_date=date(2026, 7, 1), topic_slug="agents", title="文章一", gist="摘要一"),
        make_original_row(doc_id="o2", doc_date=date(2026, 7, 2), topic_slug="agents", title="文章二", gist="摘要二"),
    ]
    clusters = deep_dive._cluster_topic_articles("agents", articles)

    assert clusters == [{"heading": "线索A", "doc_ids": ["o1", "o2"]}]
    call_structured_mock.assert_called_once()
    assert call_structured_mock.call_args.kwargs["response_model"] is deep_dive.TopicClusterResult
    user_content = call_structured_mock.call_args.kwargs["user_content"]
    assert "o1" in user_content and "文章一" in user_content
    assert "o2" in user_content and "文章二" in user_content


def test_cluster_topic_articles_filters_fabricated_doc_ids(mocker):
    """LLM 编造的、不在候选文章里的 doc_id 要被机械过滤掉；过滤后 doc_ids 为空的
    cluster 整体丢弃。"""
    valid_cluster = make_cluster_mock(mocker, "线索A", ["o1", "not-real"])
    empty_after_filter = make_cluster_mock(mocker, "线索B", ["not-real-2"])
    result_mock = mocker.Mock(clusters=[valid_cluster, empty_after_filter])
    mocker.patch.object(deep_dive, "call_structured", return_value=result_mock)

    articles = [make_original_row(doc_id="o1", doc_date=date(2026, 7, 1), topic_slug="agents")]
    clusters = deep_dive._cluster_topic_articles("agents", articles)

    assert clusters == [{"heading": "线索A", "doc_ids": ["o1"]}]


def test_cluster_topic_articles_truncates_skeleton_at_limit(mocker):
    """喂给聚类 LLM 的原文数量超过 CLUSTER_SKELETON_LIMIT 时机械截断（防御性上限，
    正常月份远低于这个数字不会真的触发）。"""
    result_mock = mocker.Mock(clusters=[])
    call_structured_mock = mocker.patch.object(deep_dive, "call_structured", return_value=result_mock)

    articles = [
        make_original_row(doc_id=f"o{i}", doc_date=date(2026, 7, 1), topic_slug="agents") for i in range(300)
    ]
    deep_dive._cluster_topic_articles("agents", articles)

    user_content = call_structured_mock.call_args.kwargs["user_content"]
    assert user_content.count("- [[o") == deep_dive.CLUSTER_SKELETON_LIMIT


# ---------------------------------------------------------------------------
# _select_cluster_fulltext_ids
# ---------------------------------------------------------------------------

def test_select_cluster_fulltext_ids_orders_by_recency_and_caps_limit():
    articles_by_id = {
        "o1": make_original_row(doc_id="o1", doc_date=date(2026, 7, 1), topic_slug="agents"),
        "o2": make_original_row(doc_id="o2", doc_date=date(2026, 7, 5), topic_slug="agents"),
        "o3": make_original_row(doc_id="o3", doc_date=date(2026, 7, 3), topic_slug="agents"),
    }
    ids = deep_dive._select_cluster_fulltext_ids(["o1", "o2", "o3"], articles_by_id, limit=2)
    assert ids == ["o2", "o3"]


# ---------------------------------------------------------------------------
# _generate_topic_analysis（周报/月报共用深度分析引擎，2026-07-09 深度改版）
# ---------------------------------------------------------------------------

def test_generate_topic_analysis_skips_llm_when_no_material(mocker):
    call_structured_mock = mocker.patch.object(deep_dive, "call_structured")
    analysis = deep_dive._generate_topic_analysis("agents", "本月", [], [], [], "600-1200字")
    call_structured_mock.assert_not_called()
    assert analysis["deep_summary"] == "本月该专题暂无可用于生成分析的素材。"
    assert analysis["relationships"] == []


def test_generate_topic_analysis_calls_llm_with_material(mocker):
    result_mock = make_analysis_result_mock(
        mocker,
        deep_summary="总览内容", continuity="延续性内容", cross_validation="交叉验证内容",
        tensions="分歧内容", emerging="新兴信号内容", relationships=[],
    )
    call_structured_mock = mocker.patch.object(deep_dive, "call_structured", return_value=result_mock)

    zettels = [make_zettel_row(doc_id="z1", doc_date=date(2026, 7, 1))]
    fulltexts = [make_fulltext_row(doc_id="original-abc", doc_date=date(2026, 7, 1))]
    analysis = deep_dive._generate_topic_analysis("agents", "本月", zettels, fulltexts, [], "600-1200字")

    assert analysis == {
        "deep_summary": "总览内容", "continuity": "延续性内容", "cross_validation": "交叉验证内容",
        "tensions": "分歧内容", "emerging": "新兴信号内容", "relationships": [],
    }
    call_structured_mock.assert_called_once()
    assert call_structured_mock.call_args.kwargs["response_model"] is deep_dive.TopicNarrativeAnalysis


def test_generate_topic_analysis_passes_length_hint_into_prompt(mocker):
    """summary_length_hint 是调用方算好直接传入的字符串，本函数只负责原样塞进
    system_prompt，不做二次判断。"""
    call_structured_mock = mocker.patch.object(
        deep_dive, "call_structured", return_value=make_analysis_result_mock(mocker)
    )
    materials = [make_zettel_row(doc_id="o1", doc_date=date(2026, 7, 1))]
    deep_dive._generate_topic_analysis("agents", "本月", materials, [], [], "1000-1800字")

    system_prompt = call_structured_mock.call_args.kwargs["system_prompt"]
    assert "1000-1800字" in system_prompt


def test_generate_topic_analysis_prioritizes_fulltext_over_gist_skeleton_in_prompt(mocker):
    """用户反馈"全文只是补充参考"的措辞把全文降级成了装饰——验证全文内容排在素材列表
    前面且措辞强调"主要依据/精读"，不是"供补充细节参考"。"""
    call_structured_mock = mocker.patch.object(
        deep_dive, "call_structured", return_value=make_analysis_result_mock(mocker)
    )
    materials = [make_zettel_row(doc_id="z1", doc_date=date(2026, 7, 1), title="骨架标题")]
    fulltexts = [make_fulltext_row(doc_id="original-abc", doc_date=date(2026, 7, 1), title="全文标题", body_md="全文正文内容")]
    deep_dive._generate_topic_analysis("agents", "本月", materials, fulltexts, [], "600-1200字")

    user_content = call_structured_mock.call_args.kwargs["user_content"]
    assert user_content.index("全文标题") < user_content.index("骨架标题")
    assert "供补充细节参考" not in user_content
    assert "主要依据" in call_structured_mock.call_args.kwargs["system_prompt"]


def test_generate_topic_analysis_includes_previous_period_material_in_prompt(mocker):
    """延续性分析的关键机制：上一期同话题素材要真的喂进 user_content，不能是摆设。"""
    result_mock = make_analysis_result_mock(mocker)
    call_structured_mock = mocker.patch.object(deep_dive, "call_structured", return_value=result_mock)

    zettels = [make_zettel_row(doc_id="z1", doc_date=date(2026, 7, 1))]
    previous_zettels = [make_zettel_row(doc_id="z0", doc_date=date(2026, 6, 20), title="上期笔记标题")]
    deep_dive._generate_topic_analysis("agents", "本周", zettels, [], previous_zettels, "150-300字")

    user_content = call_structured_mock.call_args.kwargs["user_content"]
    assert "上期笔记标题" in user_content
    assert "z0" in user_content


def test_generate_topic_analysis_notes_absence_when_no_previous_material(mocker):
    """没有上一期素材（比如本话题本期新出现）时如实说明，不能编造延续性对比。"""
    result_mock = make_analysis_result_mock(mocker)
    call_structured_mock = mocker.patch.object(deep_dive, "call_structured", return_value=result_mock)

    zettels = [make_zettel_row(doc_id="z1", doc_date=date(2026, 7, 1))]
    deep_dive._generate_topic_analysis("agents", "本周", zettels, [], [], "150-300字")

    user_content = call_structured_mock.call_args.kwargs["user_content"]
    assert "上一期没有该话题的素材" in user_content


def test_generate_topic_analysis_filters_relationships_to_valid_material_ids(mocker):
    """relationships 里 from_id/to_id 必须都在候选素材（zettels ∪ fulltexts）里，
    LLM 编造的、指向不存在素材的边要被丢弃；自环（from_id==to_id）也要丢弃。"""
    valid_edge = mocker.Mock(from_id="z1", to_id="original-abc", relation="corroborates", label="均指向同一信号")
    invalid_target_edge = mocker.Mock(from_id="z1", to_id="not-real", relation="conflicts", label="编造的边")
    self_loop_edge = mocker.Mock(from_id="z1", to_id="z1", relation="corroborates", label="自己指向自己")
    result_mock = make_analysis_result_mock(mocker, relationships=[valid_edge, invalid_target_edge, self_loop_edge])
    mocker.patch.object(deep_dive, "call_structured", return_value=result_mock)

    zettels = [make_zettel_row(doc_id="z1", doc_date=date(2026, 7, 1))]
    fulltexts = [make_fulltext_row(doc_id="original-abc", doc_date=date(2026, 7, 1))]
    analysis = deep_dive._generate_topic_analysis("agents", "本周", zettels, fulltexts, [], "150-300字")

    assert analysis["relationships"] == [
        {
            "from_id": "z1", "from_title": "笔记标题",
            "to_id": "original-abc", "to_title": "原文标题",
            "relation": "corroborates", "label": "均指向同一信号",
        }
    ]


# ---------------------------------------------------------------------------
# _render_topic_analysis_section
# ---------------------------------------------------------------------------

def test_render_topic_analysis_section_includes_all_dimensions():
    analysis = make_analysis(
        deep_summary="总览文本", continuity="延续文本", cross_validation="交叉文本",
        tensions="分歧文本", emerging="新兴文本",
    )
    section = deep_dive._render_topic_analysis_section("agents", analysis)
    assert deep_dive.topic_heading("agents") in section
    assert "总览文本" in section
    assert "**延续性**：延续文本" in section
    assert "**交叉验证**：交叉文本" in section
    assert "**分歧**：分歧文本" in section
    assert "**新兴信号**：新兴文本" in section


def test_render_topic_analysis_section_appends_representatives_when_provided():
    analysis = make_analysis()
    representatives = [{"doc_id": "o1", "title": "标题", "source_name": "openai-rss", "gist": "摘要"}]
    section = deep_dive._render_topic_analysis_section("agents", analysis, representatives)
    assert "**参考文章**" in section
    assert "[[o1]] 标题（来源：openai-rss）：摘要" in section


def test_render_topic_analysis_section_omits_representatives_when_not_provided():
    """月报调用不传 representatives，不应该出现"参考文章"小标题。"""
    analysis = make_analysis()
    section = deep_dive._render_topic_analysis_section("agents", analysis)
    assert "**参考文章**" not in section


def test_render_topic_analysis_section_includes_relationship_chart_when_present():
    analysis = make_analysis(
        relationships=[
            {"from_id": "z1", "from_title": "笔记一", "to_id": "z2", "to_title": "笔记二", "relation": "corroborates", "label": "相互印证"}
        ]
    )
    section = deep_dive._render_topic_analysis_section("agents", analysis)
    assert "```mermaid" in section
    assert "flowchart LR" in section


# ---------------------------------------------------------------------------
# _build_relationship_chart
# ---------------------------------------------------------------------------

def test_build_relationship_chart_empty_when_no_relationships():
    assert deep_dive._build_relationship_chart([]) == ""


def test_build_relationship_chart_uses_titles_not_doc_ids_for_node_labels():
    """节点标签用文章真实标题，不是 doc_id 字符串——doc_id 对读者没有信息量。"""
    relationships = [
        {"from_id": "z1", "from_title": "出口管制新规发布", "to_id": "z2", "to_title": "芯片禁令影响分析", "relation": "corroborates", "label": "均提及出口管制"},
        {"from_id": "z2", "from_title": "芯片禁令影响分析", "to_id": "z3", "to_title": "行业反应不一", "relation": "conflicts", "label": "对影响范围判断不同"},
    ]
    chart = deep_dive._build_relationship_chart(relationships)
    assert chart.startswith("```mermaid\nflowchart LR\n")
    assert chart.endswith("```")
    assert '-->|"✅ 均提及出口管制"|' in chart
    assert '-.->|"⚡ 对影响范围判断不同"|' in chart
    assert 'n0["出口管制新规发布"]' in chart
    assert 'n1["芯片禁令影响分析"]' in chart
    assert 'n2["行业反应不一"]' in chart
    assert "z1" not in chart  # doc_id 不应该出现在渲染结果里
    assert "z2" not in chart


def test_build_relationship_chart_reuses_node_alias_for_repeated_doc_id():
    """同一个 doc_id 在多条边里出现，应该复用同一个节点别名，不重复定义节点。"""
    relationships = [
        {"from_id": "z1", "from_title": "笔记一", "to_id": "z2", "to_title": "笔记二", "relation": "corroborates", "label": "A"},
        {"from_id": "z1", "from_title": "笔记一", "to_id": "z3", "to_title": "笔记三", "relation": "conflicts", "label": "B"},
    ]
    chart = deep_dive._build_relationship_chart(relationships)
    assert chart.count('n0["笔记一"]') == 1


# ---------------------------------------------------------------------------
# _sanitize_mermaid_label
# ---------------------------------------------------------------------------

def test_sanitize_mermaid_label_strips_unsafe_characters():
    """双引号/竖线/方括号/换行会破坏 mermaid 边标签语法（`|"label"|`），必须清洗掉。"""
    assert deep_dive._sanitize_mermaid_label('含"引号"和|竖线|以及[方括号]') == "含引号和竖线以及方括号"
    assert deep_dive._sanitize_mermaid_label("换行\n也要处理") == "换行也要处理"


def test_sanitize_mermaid_label_truncates_long_text():
    long_text = "字" * 50
    result = deep_dive._sanitize_mermaid_label(long_text, max_length=24)
    assert len(result) == 24


# ---------------------------------------------------------------------------
# _previous_weekly_window / _previous_monthly_window
# ---------------------------------------------------------------------------

def test_previous_weekly_window_shifts_back_seven_days():
    prev_start, prev_end = deep_dive._previous_weekly_window(date(2026, 7, 1))
    assert prev_start == date(2026, 6, 24)
    assert prev_end == date(2026, 6, 30)


def test_previous_monthly_window_returns_prior_calendar_month():
    prev_start, prev_end = deep_dive._previous_monthly_window(date(2026, 7, 1))
    assert prev_start == date(2026, 6, 1)
    assert prev_end == date(2026, 6, 30)


def test_previous_monthly_window_handles_january_rollover():
    prev_start, prev_end = deep_dive._previous_monthly_window(date(2026, 1, 1))
    assert prev_start == date(2025, 12, 1)
    assert prev_end == date(2025, 12, 31)


# ---------------------------------------------------------------------------
# _dynamic_summary_length_hint（2026-07-09 追加：篇幅随素材篇数动态调整）
# ---------------------------------------------------------------------------

def test_dynamic_summary_length_hint_picks_lowest_tier_for_sparse_material():
    hint = deep_dive._dynamic_summary_length_hint(2, deep_dive.MONTHLY_SUMMARY_LENGTH_TIERS)
    assert hint == "400-700字"


def test_dynamic_summary_length_hint_picks_middle_tier_at_exact_threshold():
    """篇数正好等于某档下限时取该档（>= 判断，不是 >）。"""
    hint = deep_dive._dynamic_summary_length_hint(5, deep_dive.MONTHLY_SUMMARY_LENGTH_TIERS)
    assert hint == "600-1200字"


def test_dynamic_summary_length_hint_picks_highest_tier_for_rich_material():
    hint = deep_dive._dynamic_summary_length_hint(50, deep_dive.MONTHLY_SUMMARY_LENGTH_TIERS)
    assert hint == "1000-1800字"


def test_dynamic_summary_length_hint_weekly_tiers_are_shorter_than_monthly():
    """同样篇数下，周报的篇幅目标应该始终短于月报——周报单份报告要覆盖最多8个热门
    topic，不能跟"固定1个topic纵向深挖"的月报用一样的篇幅基调。"""
    for count in (2, 5, 10, 50):
        weekly_hint = deep_dive._dynamic_summary_length_hint(count, deep_dive.WEEKLY_SUMMARY_LENGTH_TIERS)
        monthly_hint = deep_dive._dynamic_summary_length_hint(count, deep_dive.MONTHLY_SUMMARY_LENGTH_TIERS)
        assert weekly_hint != monthly_hint


# ---------------------------------------------------------------------------
# _extract_cited_doc_ids
# ---------------------------------------------------------------------------

def test_extract_cited_doc_ids_only_returns_cited_and_valid():
    text_content = "本月关注 [[z1]] 与 [[original-x]]，以及一个 LLM 编造出的 [[not-real]]。"
    valid_ids = ["z1", "z2", "original-x"]
    cited = deep_dive._extract_cited_doc_ids(text_content, valid_ids)
    assert cited == ["z1", "original-x"]


def test_extract_cited_doc_ids_preserves_valid_ids_order():
    text_content = "[[z2]] 在前面提到，[[z1]] 后面才提到。"
    valid_ids = ["z1", "z2"]
    assert deep_dive._extract_cited_doc_ids(text_content, valid_ids) == ["z1", "z2"]


def test_extract_cited_doc_ids_empty_when_nothing_cited():
    assert deep_dive._extract_cited_doc_ids("正文完全没有引用任何素材。", ["z1", "z2"]) == []


# ---------------------------------------------------------------------------
# _coerce_date
# ---------------------------------------------------------------------------

def test_coerce_date_passes_through_date_object():
    d = date(2026, 7, 1)
    assert deep_dive._coerce_date(d) is d


def test_coerce_date_parses_iso_string():
    assert deep_dive._coerce_date("2026-07-01") == date(2026, 7, 1)


# ---------------------------------------------------------------------------
# _build_topic_deep_dive_record
# ---------------------------------------------------------------------------

def make_cluster_section(*, heading: str = "子主题标题", doc_ids: list[str] | None = None, analysis: dict | None = None) -> dict:
    return {"heading": heading, "doc_ids": doc_ids if doc_ids is not None else ["original-abc"], "analysis": analysis or make_analysis()}


def test_build_topic_deep_dive_record_doc_id_title_and_date():
    window_start, window_end = date(2026, 7, 1), date(2026, 7, 31)
    record = deep_dive._build_topic_deep_dive_record(
        "model-releases", window_start, window_end, 10, [], [], []
    )
    assert record["doc_id"] == "deep-dive-model-releases-2026-07-31"
    assert record["doc_date"] == window_end
    assert record["doc_type"] == "deep_dive"
    assert record["title"] == "模型发布专题月报 · 2026年07月"


def test_build_topic_deep_dive_record_frontmatter_fields():
    cluster_sections = [make_cluster_section(heading="子主题A", doc_ids=["original-a", "original-b"])]
    record = deep_dive._build_topic_deep_dive_record(
        "agents", date(2026, 7, 1), date(2026, 7, 31), 12, [], cluster_sections, ["original-a"]
    )
    fm = record["frontmatter"]
    assert fm["topic_slug"] == "agents"
    assert fm["window_start"] == "2026-07-01"
    assert fm["window_end"] == "2026-07-31"
    assert fm["entry_count"] == 12
    assert fm["cluster_count"] == 1
    assert fm["cluster_headings"] == ["子主题A"]
    assert fm["deep_dive_original_ids"] == ["original-a"]


def test_build_topic_deep_dive_record_body_contains_heading_toc_and_all_cluster_dimensions():
    cluster_sections = [
        make_cluster_section(
            heading="闭源模型竞速",
            doc_ids=["original-a"],
            analysis=make_analysis(
                deep_summary="这是总览**加粗内容**", continuity="延续内容", cross_validation="交叉内容",
                tensions="分歧内容", emerging="新兴内容",
            ),
        )
    ]
    record = deep_dive._build_topic_deep_dive_record(
        "agents", date(2026, 7, 1), date(2026, 7, 31), 5, [], cluster_sections, []
    )
    body = record["body_md"]
    assert deep_dive.topic_heading("agents") in body
    assert "识别出 1 条主要线索" in body
    assert "「闭源模型竞速」" in body
    assert "### 闭源模型竞速" in body
    assert "这是总览**加粗内容**" in body
    assert "**延续性**：延续内容" in body
    assert "**交叉验证**：交叉内容" in body
    assert "**分歧**：分歧内容" in body
    assert "**新兴信号**：新兴内容" in body
    assert "## 本月数据统计" in body
    assert "子主题数：1" in body


def test_build_topic_deep_dive_record_multiple_clusters_each_get_own_section():
    cluster_sections = [
        make_cluster_section(heading="线索A", doc_ids=["original-a"], analysis=make_analysis(deep_summary="A的总览")),
        make_cluster_section(heading="线索B", doc_ids=["original-b"], analysis=make_analysis(deep_summary="B的总览")),
    ]
    record = deep_dive._build_topic_deep_dive_record(
        "agents", date(2026, 7, 1), date(2026, 7, 31), 10, [], cluster_sections, []
    )
    body = record["body_md"]
    assert "### 线索A" in body
    assert "A的总览" in body
    assert "### 线索B" in body
    assert "B的总览" in body
    assert "识别出 2 条主要线索" in body


def test_build_topic_deep_dive_record_notes_absence_when_no_clusters():
    """0 个子主题（聚类失败且调用方没有兜底成单一线索）也要正常产出文档，不能开天窗。"""
    record = deep_dive._build_topic_deep_dive_record(
        "agents", date(2026, 7, 1), date(2026, 7, 31), 0, [], [], []
    )
    assert deep_dive._NO_CLUSTER_NOTE in record["body_md"]
    assert record["frontmatter"]["cluster_count"] == 0
    assert record["link_targets"] == ["agents"]


def test_build_topic_deep_dive_record_omits_chart_when_no_daily_data():
    record = deep_dive._build_topic_deep_dive_record(
        "agents", date(2026, 7, 1), date(2026, 7, 31), 0, [], [], []
    )
    assert "```mermaid" not in record["body_md"]


def test_build_topic_deep_dive_record_includes_chart_with_topic_label_when_data_present():
    daily_counts = [{"date": "2026-07-01", "count": 3}]
    record = deep_dive._build_topic_deep_dive_record(
        "agents", date(2026, 7, 1), date(2026, 7, 31), 3, daily_counts, [], []
    )
    assert "xychart-beta" in record["body_md"]
    assert "Agent · 本月每日产出量" in record["body_md"]


def test_build_topic_deep_dive_record_includes_relationship_chart_when_present():
    cluster_sections = [
        make_cluster_section(
            doc_ids=["z1", "z2"],
            analysis=make_analysis(
                relationships=[
                    {"from_id": "z1", "from_title": "笔记一", "to_id": "z2", "to_title": "笔记二", "relation": "conflicts", "label": "结论矛盾"}
                ]
            ),
        )
    ]
    record = deep_dive._build_topic_deep_dive_record(
        "agents", date(2026, 7, 1), date(2026, 7, 31), 5, [], cluster_sections, []
    )
    assert "flowchart LR" in record["body_md"]


def test_build_topic_deep_dive_record_link_targets_always_includes_topic_slug():
    cluster_sections = [make_cluster_section(doc_ids=["original-a"], analysis=make_analysis(deep_summary="无引用的总览"))]
    record = deep_dive._build_topic_deep_dive_record(
        "agents", date(2026, 7, 1), date(2026, 7, 31), 0, [], cluster_sections, []
    )
    assert record["link_targets"] == ["agents"]


def test_build_topic_deep_dive_record_link_targets_only_cited_material_within_own_cluster():
    """每个子主题只能引用自己 cluster 内的素材——不能借用别的子主题的候选素材当有效引用，
    即使那个 id 确实存在于文档库里（防止张冠李戴，边的语义要真的对应这条线索）。"""
    cluster_a = make_cluster_section(
        heading="线索A",
        doc_ids=["z1"],
        analysis=make_analysis(
            deep_summary="本月重点关注 [[z1]] 提到的进展",
            cross_validation="详见 [[z2]]（这是线索B的素材，不该在这里生效），另外 [[not-in-material]] 是编造的",
        ),
    )
    cluster_b = make_cluster_section(heading="线索B", doc_ids=["z2"], analysis=make_analysis())
    record = deep_dive._build_topic_deep_dive_record(
        "agents", date(2026, 7, 1), date(2026, 7, 31), 5, [], [cluster_a, cluster_b], []
    )
    assert record["link_targets"] == ["agents", "z1"]


def test_build_topic_deep_dive_record_unknown_topic_slug_falls_back_gracefully():
    """topic_slug 不在 TOPIC_LABEL 映射表里（历史遗留桶）时不应该报错，直接用 slug 本身。"""
    record = deep_dive._build_topic_deep_dive_record(
        "some-legacy-bucket", date(2026, 7, 1), date(2026, 7, 31), 8, [], [], []
    )
    assert "some-legacy-bucket" in record["title"]


# ---------------------------------------------------------------------------
# activity 入口：compute_topic_deep_dive_candidates_activity
# ---------------------------------------------------------------------------

def test_compute_topic_deep_dive_candidates_activity_queries_full_month_window(mocker):
    rows = [
        make_original_row(doc_id=f"o{i}", doc_date=date(2026, 6, 1 + i), topic_slug="agents") for i in range(8)
    ]
    list_mock = mocker.patch.object(deep_dive, "deep_dive_list_original_documents_in_window", return_value=rows)

    result = deep_dive.compute_topic_deep_dive_candidates_activity(date(2026, 6, 1), date(2026, 6, 30))

    list_mock.assert_called_once_with(date(2026, 6, 1), date(2026, 6, 30))
    assert len(result) == 1
    assert result[0]["slug"] == "agents"


# ---------------------------------------------------------------------------
# activity 入口：compute_topic_deep_dive_stats_activity
# ---------------------------------------------------------------------------

def test_compute_topic_deep_dive_stats_activity_queries_by_topic_and_window(mocker):
    from worker.schemas import TopicDeepDiveParams

    rows = [make_original_row(doc_id="o1", doc_date=date(2026, 6, 5), topic_slug="agents")]
    list_mock = mocker.patch.object(
        deep_dive, "topic_deep_dive_list_original_documents_in_window", return_value=rows
    )
    cluster_mock = make_cluster_mock(mocker, "线索A", ["o1"])
    mocker.patch.object(deep_dive, "call_structured", return_value=mocker.Mock(clusters=[cluster_mock]))

    params = TopicDeepDiveParams(topic_slug="agents", window_start=date(2026, 6, 1), window_end=date(2026, 6, 30))
    result = deep_dive.compute_topic_deep_dive_stats_activity(params)

    list_mock.assert_called_once_with("agents", date(2026, 6, 1), date(2026, 6, 30))
    assert result["topic_slug"] == "agents"
    assert result["window_start"] == date(2026, 6, 1)
    assert result["window_end"] == date(2026, 6, 30)
    assert result["entry_count"] == 1
    assert len(result["daily_counts"]) == 30
    assert result["clusters"] == [{"heading": "线索A", "doc_ids": ["o1"]}]


def test_compute_topic_deep_dive_stats_activity_falls_back_to_single_cluster_when_clustering_empty(mocker):
    """聚类返回 0 条有效线索时（LLM 判断失误或素材过于同质），退化成"整个 topic 当一条
    线索"，不能因为聚类失败就让报告开天窗。"""
    from worker.schemas import TopicDeepDiveParams

    rows = [
        make_original_row(doc_id="o1", doc_date=date(2026, 6, 5), topic_slug="agents"),
        make_original_row(doc_id="o2", doc_date=date(2026, 6, 6), topic_slug="agents"),
    ]
    mocker.patch.object(deep_dive, "topic_deep_dive_list_original_documents_in_window", return_value=rows)
    mocker.patch.object(deep_dive, "call_structured", return_value=mocker.Mock(clusters=[]))

    params = TopicDeepDiveParams(topic_slug="agents", window_start=date(2026, 6, 1), window_end=date(2026, 6, 30))
    result = deep_dive.compute_topic_deep_dive_stats_activity(params)

    assert result["clusters"] == [{"heading": "Agent", "doc_ids": ["o1", "o2"]}]


# ---------------------------------------------------------------------------
# activity 入口：generate_topic_deep_dive_activity
# ---------------------------------------------------------------------------

def test_generate_topic_deep_dive_activity_writes_single_record(mocker):
    articles = [make_original_row(doc_id="original-abc", doc_date=date(2026, 6, 5), topic_slug="agents")]
    fulltexts = [make_fulltext_row(doc_id="original-abc", doc_date=date(2026, 6, 5))]
    mocker.patch.object(deep_dive, "topic_deep_dive_list_original_documents_in_window", return_value=articles)
    mocker.patch.object(deep_dive, "topic_deep_dive_fetch_original_fulltext", return_value=fulltexts)
    mocker.patch.object(deep_dive, "call_structured", return_value=make_analysis_result_mock(mocker))
    write_activity_mock = mocker.patch.object(deep_dive, "write_activity", return_value=1)

    payload = {
        "topic_slug": "agents",
        "window_start": date(2026, 6, 1),
        "window_end": date(2026, 6, 30),
        "entry_count": 8,
        "daily_counts": [],
        "clusters": [{"heading": "线索A", "doc_ids": ["original-abc"]}],
    }
    result = deep_dive.generate_topic_deep_dive_activity(payload)

    write_activity_mock.assert_called_once()
    written_records = write_activity_mock.call_args[0][0]
    assert len(written_records) == 1
    assert written_records[0]["doc_id"] == "deep-dive-agents-2026-06-30"
    assert result == {
        "written": 1,
        "doc_id": "deep-dive-agents-2026-06-30",
        "topic_slug": "agents",
        "cluster_count": 1,
    }


def test_generate_topic_deep_dive_activity_queries_previous_month_for_continuity(mocker):
    """延续性分析需要上一个月同 topic 的原文素材——验证真的查了上个月窗口（当月查一次
    取子主题素材，上月查一次取延续性对比素材），不是只查当月就完事。"""
    articles = [make_original_row(doc_id="original-abc", doc_date=date(2026, 6, 5), topic_slug="agents")]
    list_mock = mocker.patch.object(
        deep_dive, "topic_deep_dive_list_original_documents_in_window", return_value=articles
    )
    mocker.patch.object(deep_dive, "topic_deep_dive_fetch_original_fulltext", return_value=[])
    mocker.patch.object(deep_dive, "call_structured", return_value=make_analysis_result_mock(mocker))
    mocker.patch.object(deep_dive, "write_activity", return_value=1)

    payload = {
        "topic_slug": "agents",
        "window_start": date(2026, 6, 1),
        "window_end": date(2026, 6, 30),
        "entry_count": 8,
        "daily_counts": [],
        "clusters": [{"heading": "线索A", "doc_ids": ["original-abc"]}],
    }
    deep_dive.generate_topic_deep_dive_activity(payload)

    assert list_mock.call_count == 2
    list_mock.assert_any_call("agents", date(2026, 6, 1), date(2026, 6, 30))
    list_mock.assert_any_call("agents", date(2026, 5, 1), date(2026, 5, 31))


def test_generate_topic_deep_dive_activity_coerces_string_window_dates(mocker):
    """回归测试：compute_topic_deep_dive_stats_activity 的 dict 返回值经 Temporal 序列化
    后 window_start/window_end 会退化成 ISO 字符串（同 M10 周报踩过的坑），必须兼容两种
    输入，不能在组装记录时报 AttributeError。"""
    mocker.patch.object(deep_dive, "topic_deep_dive_list_original_documents_in_window", return_value=[])
    mocker.patch.object(deep_dive, "topic_deep_dive_fetch_original_fulltext", return_value=[])
    mocker.patch.object(deep_dive, "call_structured")
    write_activity_mock = mocker.patch.object(deep_dive, "write_activity", return_value=1)

    payload = {
        "topic_slug": "agents",
        "window_start": "2026-06-01",
        "window_end": "2026-06-30",
        "entry_count": 0,
        "daily_counts": [],
        "clusters": [],
    }
    result = deep_dive.generate_topic_deep_dive_activity(payload)

    written_records = write_activity_mock.call_args[0][0]
    assert written_records[0]["doc_id"] == "deep-dive-agents-2026-06-30"
    assert result["doc_id"] == "deep-dive-agents-2026-06-30"


def test_generate_topic_deep_dive_activity_limits_fulltext_fetch_per_cluster(mocker):
    """每个子主题各自独立挑选最多 CLUSTER_FULLTEXT_LIMIT 篇深挖全文，不是整个 topic
    共用一个固定上限——验证 topic_deep_dive_fetch_original_fulltext 每次调用收到的都是
    该子主题内截断后的 id 列表。"""
    articles = [
        make_original_row(doc_id=f"o{i}", doc_date=date(2026, 6, 1 + i), topic_slug="agents") for i in range(15)
    ]
    mocker.patch.object(deep_dive, "topic_deep_dive_list_original_documents_in_window", return_value=articles)
    fulltext_mock = mocker.patch.object(deep_dive, "topic_deep_dive_fetch_original_fulltext", return_value=[])
    mocker.patch.object(deep_dive, "call_structured", return_value=make_analysis_result_mock(mocker))
    mocker.patch.object(deep_dive, "write_activity", return_value=1)

    payload = {
        "topic_slug": "agents",
        "window_start": date(2026, 6, 1),
        "window_end": date(2026, 6, 30),
        "entry_count": 15,
        "daily_counts": [],
        "clusters": [{"heading": "唯一线索", "doc_ids": [f"o{i}" for i in range(15)]}],
    }
    deep_dive.generate_topic_deep_dive_activity(payload)

    called_ids = fulltext_mock.call_args[0][0]
    assert len(called_ids) == deep_dive.CLUSTER_FULLTEXT_LIMIT


def test_generate_topic_deep_dive_activity_generates_one_section_per_cluster(mocker):
    """多个子主题时，总深挖篇数随子主题数自然放大（不是整个 topic 固定 10 篇上限）；
    每个子主题各调一次深度分析。"""
    articles = [make_original_row(doc_id=f"o{i}", doc_date=date(2026, 6, 1 + i), topic_slug="agents") for i in range(6)]
    mocker.patch.object(deep_dive, "topic_deep_dive_list_original_documents_in_window", return_value=articles)
    fulltext_mock = mocker.patch.object(deep_dive, "topic_deep_dive_fetch_original_fulltext", return_value=[])
    call_structured_mock = mocker.patch.object(
        deep_dive, "call_structured", return_value=make_analysis_result_mock(mocker)
    )
    write_activity_mock = mocker.patch.object(deep_dive, "write_activity", return_value=1)

    payload = {
        "topic_slug": "agents",
        "window_start": date(2026, 6, 1),
        "window_end": date(2026, 6, 30),
        "entry_count": 6,
        "daily_counts": [],
        "clusters": [
            {"heading": "线索A", "doc_ids": ["o0", "o1", "o2"]},
            {"heading": "线索B", "doc_ids": ["o3", "o4", "o5"]},
        ],
    }
    result = deep_dive.generate_topic_deep_dive_activity(payload)

    assert call_structured_mock.call_count == 2  # 每个子主题各一次深度分析
    assert fulltext_mock.call_count == 2  # 每个子主题各自独立挑选深挖全文
    assert result["cluster_count"] == 2
    written_records = write_activity_mock.call_args[0][0]
    assert "### 线索A" in written_records[0]["body_md"]
    assert "### 线索B" in written_records[0]["body_md"]

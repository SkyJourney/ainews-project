"""Deep Dive 周报专用 activity（M10 新增，详见 .claude/memory/decisions.md）。

背景：对过去一周内容做二次聚合，识别"跨日延续/热门 topic 趋势线" + 一段 LLM 生成的周
叙事导语，产出独立的 `documents.doc_type='deep_dive'` 文档。完全独立的 Temporal Schedule
（`worker/worker.py::ensure_deep_dive_schedule`，每周一 09:00 Asia/Shanghai），跟主流水线
和 arxiv 全文回补一样互不阻塞，且比 arxiv 回补更彻底地"只读输入"——不改写任何既有
Topic/Daily/Digest/Original/Zettel 文档，只新增一条 deep_dive 记录。

聚合规则贯彻"跨文章判断只能发生在 aggregate 阶段"这条项目铁律的自然延伸：哪个 topic
算"热门"是机械统计规则（total_count/active_days 双门槛），直接复用每篇文章 aggregate
阶段已经判定的 topic_slug，不重新调用聚类 LLM 做二次判断。唯一一次 LLM 调用只做"给定
素材写一段周叙事导语"，不参与"哪个 topic 算热门"这个判断。
"""

from __future__ import annotations

from datetime import date, timedelta

from temporalio import activity

from worker.aggregate import PLACEHOLDER_TOPIC, TOPIC_EMOJI, TOPIC_LABEL, topic_heading
from worker.db import (
    deep_dive_list_digest_documents_in_window,
    deep_dive_list_original_documents_in_window,
)
from worker.enrich import content_hash
from worker.llm_client import call_structured
from worker.schemas import DeepDiveIntro
from worker.write import write_activity

WINDOW_DAYS = 7
MIN_TREND_TOTAL_COUNT = 3
MIN_TREND_ACTIVE_DAYS = 2
MAX_TRENDING_TOPICS = 8
REPRESENTATIVES_PER_TOPIC = 3

_INTRO_MODEL = "deepseek-v4-flash"

_NO_TREND_NOTE = "本周内容集中度不足，未形成显著趋势。"
_NO_MATERIAL_INTRO = "本周暂无可用于生成周报导语的素材。"


# ---------------------------------------------------------------------------
# 纯函数：趋势统计（可单测，不依赖 DB/LLM）
# ---------------------------------------------------------------------------

def _compute_topic_trends(rows: list[dict]) -> list[dict]:
    """按 topic_slug 分组统计 total_count（窗口内文章数）/active_days（distinct 日期数），
    机械筛选（total_count >= MIN_TREND_TOTAL_COUNT 且 active_days >= MIN_TREND_ACTIVE_DAYS）
    后按 total_count 降序截断到前 MAX_TRENDING_TOPICS 个。`uncategorized` 溢出桶不是真实
    主题聚类结果，不参与"热门话题"评选，跳过。
    """
    grouped: dict[str, dict] = {}
    for row in rows:
        slug = row.get("topic_slug")
        if not slug or slug == PLACEHOLDER_TOPIC:
            continue
        bucket = grouped.setdefault(slug, {"slug": slug, "dates": set(), "docs": []})
        bucket["dates"].add(row["doc_date"])
        bucket["docs"].append(row)

    trends = []
    for bucket in grouped.values():
        total_count = len(bucket["docs"])
        active_days = len(bucket["dates"])
        if total_count < MIN_TREND_TOTAL_COUNT or active_days < MIN_TREND_ACTIVE_DAYS:
            continue
        trends.append(
            {"slug": bucket["slug"], "total_count": total_count, "active_days": active_days, "docs": bucket["docs"]}
        )
    trends.sort(key=lambda t: t["total_count"], reverse=True)
    return trends[:MAX_TRENDING_TOPICS]


def _select_representative_docs(docs: list[dict], limit: int = REPRESENTATIVES_PER_TOPIC) -> list[dict]:
    """按 doc_date 降序取前 N 篇，纯机械排序，不做"代表性"判断。"""
    return sorted(docs, key=lambda d: d["doc_date"], reverse=True)[:limit]


def _compute_daily_counts(rows: list[dict], window_start: date, window_end: date) -> list[dict]:
    """按日期统计窗口内每天的原文数量，机械填满窗口内每一天（含 0 篇的日子），保证柱状图
    x 轴天数固定等于 WINDOW_DAYS，不会因为某天没数据就少一根柱子。日期用 ISO 字符串
    （不是 date 对象）——这份结构要跨 activity 边界传递，之前在 window_start/window_end
    上踩过"未指定字段类型的 dict 里 date 会被 Temporal 序列化退化成字符串"这个坑（见
    .claude/memory/decisions.md），这里直接产出字符串，不留歧义，下游也不需要真的做
    日期运算，字符串够用。
    """
    counts: dict[date, int] = {}
    for row in rows:
        counts[row["doc_date"]] = counts.get(row["doc_date"], 0) + 1
    days = []
    current = window_start
    while current <= window_end:
        days.append({"date": current.isoformat(), "count": counts.get(current, 0)})
        current += timedelta(days=1)
    return days


# ---------------------------------------------------------------------------
# 纯函数：导语生成 + 文档组装（可单测，LLM 调用点隔离在 _generate_intro 里）
# ---------------------------------------------------------------------------

def _generate_intro(trending: list[dict], digests: list[dict]) -> str:
    """素材皆空时机械兜底不调 LLM（理论上不会发生——触发门槛是 digest 历史 ≥7 天已满足，
    这里只是防御性兜底）；否则拼 prompt 用给定素材生成一段导语。"""
    if not trending and not digests:
        return _NO_MATERIAL_INTRO

    trend_lines = [
        f"- {t['slug']}：本周 {t['total_count']} 条，{t['active_days']} 天活跃，代表文章："
        + "；".join(f"{r['title']}（{r['gist']}）" for r in t["representatives"])
        for t in trending
    ] or ["（本周没有满足门槛的热门话题）"]

    digest_lines = [f"### {d['doc_date'].isoformat()}\n{d['body_md']}" for d in digests]

    user_content = (
        "本周热门话题统计：\n" + "\n".join(trend_lines) + "\n\n"
        "本周逐日 Digest 原文：\n" + "\n\n".join(digest_lines)
    )
    result = call_structured(
        model=_INTRO_MODEL,
        system_prompt=(
            "你是资讯周报编辑。根据给定的本周热门话题统计和逐日 Digest 原文，写一段150-300字"
            "的中文导语，概括本周AI领域整体动态与延续性趋势。只能引用素材中出现的事实，不能编造，"
            "不需要逐条罗列，重点讲清楚趋势和关联。用 Markdown **加粗** 标出 2-4 处最核心的"
            "关键词/短语（如核心趋势、代表性产品/公司名）或不超过15字的核心结论短句，"
            "方便读者快速扫描抓重点，不要整句话加粗。"
        ),
        user_content=user_content,
        response_model=DeepDiveIntro,
    )
    return result.intro


def _build_trend_pie_chart(trending: list[dict]) -> str:
    """机械生成 mermaid 饼图代码块，展示热门 topic 的 total_count 占比——直接从
    compute_deep_dive_trends_activity 已经算好的结构化统计拼接固定语法，不经过 LLM
    （避免图表语法出错、或凭印象编造跟真实统计不一致的数字）。0 个热门 topic 时
    调用方不应该调这个函数（空饼图没有意义）。"""
    lines = ['```mermaid', 'pie showData', '    title 本周热门话题占比']
    for t in trending:
        emoji = TOPIC_EMOJI.get(t["slug"], "📌")
        label = TOPIC_LABEL.get(t["slug"], t["slug"])
        lines.append(f'    "{emoji} {label}" : {t["total_count"]}')
    lines.append('```')
    return "\n".join(lines)


def _build_daily_volume_bar_chart(daily_counts: list[dict]) -> str:
    """机械生成 mermaid 柱状图代码块，展示窗口内每日原文产出量——直接从
    _compute_daily_counts 已经算好的逐日统计拼接固定语法，不经过 LLM。空窗口
    （daily_counts 为空，理论上不会发生）调用方不应该调这个函数。"""
    labels = [d["date"][5:] for d in daily_counts]  # "2026-07-02" -> "07-02"，柱状图标签不需要年份
    counts = [d["count"] for d in daily_counts]
    max_count = max(counts)
    y_max = max_count + max(1, max_count // 5)  # 顶部留一点余量，最高的柱子不会贴着图表边框
    x_axis_labels = ", ".join(f'"{label}"' for label in labels)
    bar_values = ", ".join(str(c) for c in counts)
    lines = [
        '```mermaid',
        'xychart-beta',
        '    title "本周每日产出量"',
        f'    x-axis [{x_axis_labels}]',
        f'    y-axis "原文数" 0 --> {y_max}',
        f'    bar [{bar_values}]',
        '```',
    ]
    return "\n".join(lines)


def _build_trend_quadrant_chart(trending: list[dict]) -> str:
    """机械生成 mermaid 象限图，把每个热门 topic 按 (延续天数, 相对热度) 定位——这是
    最直接对应"热门 topic 趋势线"这个设计目标的图：右上"持续热点"（活跃天数多且总量
    大）、左上"集中爆发"（活跃天数少但总量大，通常是单个大事件带动）、右下"细水长流"
    （活跃天数多但单量不大）、左下"边缘话题"（本周热门话题里相对不起眼的）。x/y 坐标
    机械归一化到 (0,1] 区间（x = active_days/WINDOW_DAYS，y = total_count/本批次最大
    total_count），不经过 LLM。0/1 个热门 topic 时调用方不应该调这个函数（象限图至少
    需要几个点才有对比意义，且 1 个点时 y 归一化后必为 1.0，退化成没有信息量的单点图）。
    """
    max_total = max(t["total_count"] for t in trending)
    lines = [
        '```mermaid',
        'quadrantChart',
        '    title 话题热度与延续性',
        '    x-axis 单日集中 --> 多日延续',
        '    y-axis 相对冷门 --> 相对热门',
        '    quadrant-1 持续热点',
        '    quadrant-2 集中爆发',
        '    quadrant-3 边缘话题',
        '    quadrant-4 细水长流',
    ]
    for t in trending:
        x = round(t["active_days"] / WINDOW_DAYS, 2)
        y = round(t["total_count"] / max_total, 2)
        label = TOPIC_LABEL.get(t["slug"], t["slug"])
        lines.append(f'    "{label}" : [{_format_quadrant_coord(x)}, {_format_quadrant_coord(y)}]')
    lines.append('```')
    return "\n".join(lines)


def _format_quadrant_coord(value: float) -> str:
    """mermaid quadrantChart 的词法分析器有个真实 bug（用 mermaid 11.16.0 实测确认，
    不是猜测）：无法解析形如 "1.0" 这种小数点后只有一个尾随零的浮点数字面量——
    `[0.5, 1.0]` 直接报 Lexical error，但 `[0.5, 1]`（裸整数）或 `[0.5, 0.99]`
    （非整数小数）都能正常解析。本项目坐标固定归一化到 (0,1] 区间，批次里 total_count
    最高的那个 topic（或 active_days 恰好等于 WINDOW_DAYS 的 topic）必然会精确算出
    1.0，这个 bug 100% 会被真实数据触发，不是理论边缘情况。整数值格式化成不带小数点的
    整数字面量规避，其余保留两位小数。"""
    if value == int(value):
        return str(int(value))
    return str(value)


def _render_trend_section(topic: dict) -> str:
    lines = [
        topic_heading(topic["slug"]),
        "",
        f"**本周 {topic['total_count']} 条 / {topic['active_days']} 天活跃**",
        "",
    ]
    lines.extend(
        f"- [[{r['doc_id']}]] {r['title']}（来源：{r['source_name']}）：{r['gist']}"
        for r in topic["representatives"]
    )
    return "\n".join(lines)


def _build_deep_dive_record(
    window_start: date,
    window_end: date,
    trending: list[dict],
    entry_count: int,
    digests: list[dict],
    intro: str,
    daily_counts: list[dict],
) -> dict:
    title = f"{window_start.isoformat()} ~ {window_end.isoformat()} AI 深度周报"
    doc_id = f"deep-dive-{window_end.isoformat()}"

    sections = (
        "\n\n".join(_render_trend_section(t) for t in trending)
        if trending
        else f"## 本周趋势\n\n{_NO_TREND_NOTE}"
    )
    # 三张图分别回答三个不同问题："这周原文什么节奏"（柱状图，只要窗口内有原文就画）、
    # "热门话题占比"（饼图）、"每个热门话题是持续型还是爆发型"（象限图，至少 2 个点
    # 才有对比意义，1 个点归一化后 y 必为 1.0，是没有信息量的退化情况）。
    charts: list[str] = []
    if daily_counts and sum(d["count"] for d in daily_counts) > 0:
        charts.append(_build_daily_volume_bar_chart(daily_counts))
    if trending:
        charts.append(_build_trend_pie_chart(trending))
    if len(trending) >= 2:
        charts.append(_build_trend_quadrant_chart(trending))
    charts_section = ("\n\n".join(charts) + "\n\n") if charts else ""
    stats_section = "## 本周数据统计\n\n" + "\n".join(
        [
            f"- 🗓️ 窗口范围：{window_start.isoformat()} ~ {window_end.isoformat()}",
            f"- 📰 原文总数：{entry_count}",
            f"- 📈 热门话题数：{len(trending)}",
            f"- 📅 覆盖 Digest 天数：{len(digests)}",
        ]
    )
    # 不在 body_md 里重复拼标题：title 已经是独立字段，前端详情页单独渲染
    body_md = intro + "\n\n" + charts_section + sections + "\n\n" + stats_section + "\n"

    # 只链接正文里实际出现的引用（热门 topic slug + 各自代表文章），不制造正文没提到的
    # 悬空/无意义边；source_digest_ids 只做 frontmatter 可追溯字段，不进 links 表——
    # Digest 本身"明确不用 wikilink"，给它建反链边不会被任何页面展示，是死边。
    link_targets: list[str] = []
    for t in trending:
        link_targets.append(t["slug"])
        link_targets.extend(r["doc_id"] for r in t["representatives"])

    return {
        "doc_id": doc_id,
        "doc_type": "deep_dive",
        "title": title,
        "doc_date": window_end,
        "frontmatter": {
            "title": title,
            "doc_type": "deep_dive",
            "window_start": window_start.isoformat(),
            "window_end": window_end.isoformat(),
            "trending_topics": [
                {"slug": t["slug"], "total_count": t["total_count"], "active_days": t["active_days"]}
                for t in trending
            ],
            "entry_count": entry_count,
            "source_digest_ids": [d["doc_id"] for d in digests],
        },
        "body_md": body_md,
        "content_hash": content_hash(body_md),
        "tags": [],
        "link_targets": link_targets,
    }


# ---------------------------------------------------------------------------
# activity 入口
# ---------------------------------------------------------------------------

@activity.defn
def compute_deep_dive_trends_activity(window_end: date) -> dict:
    """纯查询 + 内存聚合，不含 LLM 调用。返回值只含代表文章的标题级字段（最多
    MAX_TRENDING_TOPICS × REPRESENTATIVES_PER_TOPIC = 24 条），不含正文，远低于
    Temporal gRPC 4MB 消息上限（04-roadmap.md §2.5 说明；aggregate_activity 曾因返回值
    含全文正文真实撞过这个上限，见 .claude/memory/decisions.md）。
    """
    window_start = window_end - timedelta(days=WINDOW_DAYS - 1)
    rows = deep_dive_list_original_documents_in_window(window_start, window_end)
    trends = _compute_topic_trends(rows)
    trending = [
        {
            "slug": t["slug"],
            "total_count": t["total_count"],
            "active_days": t["active_days"],
            "representatives": [
                {"doc_id": d["doc_id"], "title": d["title"], "source_name": d["source_name"], "gist": d["gist"]}
                for d in _select_representative_docs(t["docs"])
            ],
        }
        for t in trends
    ]
    daily_counts = _compute_daily_counts(rows, window_start, window_end)
    activity.logger.info(
        f"compute_deep_dive_trends_activity: 窗口 {window_start}~{window_end}，"
        f"原文 {len(rows)} 篇，热门话题 {len(trending)} 个"
    )
    return {
        "window_start": window_start,
        "window_end": window_end,
        "entry_count": len(rows),
        "trending": trending,
        "daily_counts": daily_counts,
    }


@activity.defn
def generate_deep_dive_activity(payload: dict) -> dict:
    """查 digest 素材 → 生成导语 → 组装记录 → write_activity 落库（直接内部调用，不单独
    注册为 Temporal activity，参照 aggregate_activity/refresh_original_document_activity
    的既有模式）。"""
    # 真实 Temporal 执行验证时发现：compute_deep_dive_trends_activity 的返回类型标注是
    # 未指定字段类型的 `-> dict`，pydantic_data_converter 在没有具体类型信息可依据时，
    # 解码这类跨 activity 边界传递的 dict 会把 date 值退化成 ISO 字符串（date.isoformat()
    # 直接调用会报 AttributeError），而单测里直接函数调用不经过 Temporal 序列化，看不出
    # 这个问题。这里显式做双态兼容，不改 compute_deep_dive_trends_activity 的返回类型
    # （保持它在单测里返回真实 date 对象、语义自文档化）。
    window_start = payload["window_start"]
    window_end = payload["window_end"]
    if isinstance(window_start, str):
        window_start = date.fromisoformat(window_start)
    if isinstance(window_end, str):
        window_end = date.fromisoformat(window_end)
    trending = payload["trending"]
    entry_count = payload["entry_count"]
    daily_counts = payload["daily_counts"]

    digests = deep_dive_list_digest_documents_in_window(window_start, window_end)
    intro = _generate_intro(trending, digests)
    record = _build_deep_dive_record(window_start, window_end, trending, entry_count, digests, intro, daily_counts)
    written = write_activity([record])
    activity.logger.info(
        f"generate_deep_dive_activity: {record['doc_id']} 写入完成，"
        f"热门话题 {len(trending)} 个，覆盖 {entry_count} 篇原文，{len(digests)} 篇 digest"
    )
    return {"written": written, "doc_id": record["doc_id"], "trending_topic_count": len(trending)}

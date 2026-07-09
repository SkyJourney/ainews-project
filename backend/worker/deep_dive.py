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

import re
from datetime import date, timedelta

from temporalio import activity

from worker.aggregate import PLACEHOLDER_TOPIC, TOPIC_EMOJI, TOPIC_LABEL, topic_heading
from worker.db import (
    deep_dive_list_digest_documents_in_window,
    deep_dive_list_original_documents_in_window,
    topic_deep_dive_fetch_original_fulltext,
    topic_deep_dive_list_original_documents_in_window,
    topic_deep_dive_list_zettel_documents_in_window,
)
from worker.enrich import content_hash
from worker.llm_client import call_structured
from worker.schemas import DeepDiveIntro, TopicClusterResult, TopicDeepDiveParams, TopicNarrativeAnalysis
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
# 专题月报（M11）专用常量：固定 1 个 topic 桶 × 自然月窗口的纵向深挖，正交于上面的
# 周报（全部 topic 桶 × 7 天窗口横向扫描）。详见 docs/milestones/M11-topic-deep-dive.md。
# ---------------------------------------------------------------------------

# 按月度窗口（约 4 倍于周报 7 天）粗略放大周报门槛（3/2），不是精确按比例换算——
# 用户与设计讨论时确认的起点值，避免内容量不够的桶被迫出一篇单薄月报。
MONTHLY_MIN_TOTAL_COUNT = 8
MONTHLY_MIN_ACTIVE_DAYS = 4
# 通用素材截断上限：超过机械截断取最新 N 条，不做二次 LLM 筛选（避免叙事阶段偷偷做
# "哪些该保留"的跨条目判断）。历史上只用于 zettel 骨架截断，2026-07-09 深度改版二起
# 也复用于"上一期同 topic 原文"截断——函数名/常量名沿用旧名字，但已是通用的
# "按 doc_date 降序截断"工具，不是 zettel 专属。
MONTHLY_ZETTEL_LIMIT = 30

_NO_MONTHLY_MATERIAL_INTRO = "本月该专题暂无可用于生成月报导语的素材。"
_NO_CLUSTER_NOTE = "本月该专题未识别出明确的子主题线索。"
_WIKILINK_ID_RE = re.compile(r"\[\[([^\]|#]+)")

# ---------------------------------------------------------------------------
# 专题月报子主题聚类常量（2026-07-09 深度改版二，见 .claude/memory/decisions.md）：
# 用户反馈首版月报"只是几个维度的汇总，不是真正的专题报告"——根因是深挖池被 zettel
# 过滤门控（很多 topic 大部分原文都没有对应 zettel，等于大量有价值内容从未进入报告
# 视野）。改为先对该 topic 本月**全部**原文聚类出真实子主题线索，再逐个子主题独立
# 深挖，报告篇幅由素材丰富度自然决定，不再是固定的"一段总览+四段维度"。
# ---------------------------------------------------------------------------

# 每个子主题深挖全文篇数上限：不再像旧版本那样整个 topic 只挑 10 篇，而是每个子主题
# 各自独立挑选，总深挖篇数随子主题数自然放大（最多 7 个子主题 × 8 篇 = 56 篇）。
CLUSTER_FULLTEXT_LIMIT = 8
# 喂给聚类 LLM 调用的原文 title+gist 数量上限：防御性上限，gist 是一句话摘要体积小，
# 正常月份的 topic 原文数（几十篇量级）远低于这个数字，不会真的触发截断。
CLUSTER_SKELETON_LIMIT = 200

# ---------------------------------------------------------------------------
# 深度叙事引擎共享常量（2026-07-09 深度改版，见 .claude/memory/decisions.md）：
# 周报/月报此前"机械 bullet 列表"/"导语+小节"两套结构都偏罗列复述，改成周报+月报
# 共用同一套"五维度深度分析"引擎（_generate_topic_analysis），只是窗口长度、素材规模、
# 喂给 LLM 的深挖全文篇数不同。
# ---------------------------------------------------------------------------

# 周报每个热门 topic 深挖全文篇数上限：周报窗口只有 7 天、代表文章数天然更少，单独定
# 一个比月报（每子主题 CLUSTER_FULLTEXT_LIMIT=8）更小的数字，不是共用一套。
WEEKLY_TOPIC_FULLTEXT_LIMIT = 5
_MERMAID_LABEL_UNSAFE_RE = re.compile(r'["|\[\]\n`]')

# ---------------------------------------------------------------------------
# mermaid 生成安全网（2026-07-09 追加，见 .claude/memory/decisions.md）：项目现有
# mermaid 图表全部是"LLM 出结构化字段 → Python 机械拼语法"，从未让 LLM 直接产出
# mermaid 代码文本——用户确认这个架构方向应该延续，但要求补强两层防御：① 转义规则
# 之前没覆盖到的已知坑（# 实体转义前缀、end 保留字）；② 加一道结构性 lint，未通过
# 时丢弃这张图而不是让格式错误的代码块混进正文破坏渲染。后端容器没有 Node/Chromium，
# 引入 mermaid-cli 做运行时语法校验成本不成比例（这是低频周/月任务，不是高吞吐路径），
# 所以 lint 是纯 Python 字符串结构检查，不是真实的 mermaid 语法解析器。
# ---------------------------------------------------------------------------
_MERMAID_DIAGRAM_TYPES = ("flowchart", "pie", "xychart-beta", "quadrantChart")
_MERMAID_BARE_FLOAT_RE = re.compile(r"\b\d+\.0\b")

# 深度内容总结的篇幅目标（2026-07-09 追加：用户反馈子主题分析仍偏"压缩摘要"，要求
# "精读原文"后写出篇幅适度的深度报告正文，不是几句话）：周报保持原有简短基调（单份
# 报告要覆盖最多 8 个热门 topic，每个都写长会导致周报臃肿到难以阅读）；月报因为是
# "固定 1 个 topic 纵向深挖"定位，明显放长——两者共用同一个 _generate_topic_analysis
# 函数，只是措辞参数不同。
#
# 2026-07-09 同日再追加：篇幅不再是固定字符串，改按素材篇数（不是深挖全文篇数，是该
# 方向/话题涉及的全部原文数）动态分档——原文越多说明这个方向本身内容越丰富，理应写得
# 越充分，不能不管素材多寡都套用同一个固定区间。档位边界（5/15篇）没有精确的数学
# 依据，是"明显更多/明显更少"这个直觉的粗粒度体现，后续可以根据实际产出效果调整。
WEEKLY_SUMMARY_LENGTH_TIERS: list[tuple[int, str]] = [
    (0, "150-300字"),
    (5, "300-500字"),
    (15, "500-800字"),
]
MONTHLY_SUMMARY_LENGTH_TIERS: list[tuple[int, str]] = [
    (0, "400-700字"),
    (5, "600-1200字"),
    (15, "1000-1800字"),
]


def _dynamic_summary_length_hint(article_count: int, tiers: list[tuple[int, str]]) -> str:
    """根据素材篇数动态选择深度总结的篇幅目标，取满足"素材篇数 >= 篇数下限"的最高
    档位。tiers 必须按篇数下限升序排列（WEEKLY_SUMMARY_LENGTH_TIERS/MONTHLY_SUMMARY_
    LENGTH_TIERS 已经是升序，调用方不需要再排序）。"""
    hint = tiers[0][1]
    for threshold, length_hint in tiers:
        if article_count >= threshold:
            hint = length_hint
    return hint


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
            "的中文导语，概括本周AI领域整体动态与延续性趋势——正文后面每个热门话题都会有独立的"
            "深度分析小节（延续性/交叉验证/分歧/新兴信号），这里只需要跨话题的全局总览，"
            "不需要重复小节里的细节。只能引用素材中出现的事实，不能编造，不需要逐条罗列，"
            "重点讲清楚趋势和关联。用 Markdown **加粗** 标出 2-4 处最核心的"
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
    code = "\n".join(lines)
    return code if _lint_mermaid_block(code) else ""


def _build_daily_volume_bar_chart(daily_counts: list[dict], title: str = "本周每日产出量") -> str:
    """机械生成 mermaid 柱状图代码块，展示窗口内每日原文产出量——直接从
    _compute_daily_counts 已经算好的逐日统计拼接固定语法，不经过 LLM。空窗口
    （daily_counts 为空，理论上不会发生）调用方不应该调这个函数。`title` 可覆盖默认
    文案——专题月报（M11）复用同一个函数，标题需要带上专题名而不是"本周"。"""
    labels = [d["date"][5:] for d in daily_counts]  # "2026-07-02" -> "07-02"，柱状图标签不需要年份
    counts = [d["count"] for d in daily_counts]
    max_count = max(counts)
    y_max = max_count + max(1, max_count // 5)  # 顶部留一点余量，最高的柱子不会贴着图表边框
    x_axis_labels = ", ".join(f'"{label}"' for label in labels)
    bar_values = ", ".join(str(c) for c in counts)
    lines = [
        '```mermaid',
        'xychart-beta',
        f'    title "{title}"',
        f'    x-axis [{x_axis_labels}]',
        f'    y-axis "原文数" 0 --> {y_max}',
        f'    bar [{bar_values}]',
        '```',
    ]
    code = "\n".join(lines)
    return code if _lint_mermaid_block(code) else ""


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
    code = "\n".join(lines)
    return code if _lint_mermaid_block(code) else ""


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


def _previous_weekly_window(window_start: date) -> tuple[date, date]:
    """上一个对应周报窗口：紧邻当前窗口之前的 7 天，供延续性分析对比素材使用。"""
    prev_end = window_start - timedelta(days=1)
    prev_start = prev_end - timedelta(days=WINDOW_DAYS - 1)
    return prev_start, prev_end


def _previous_monthly_window(window_start: date) -> tuple[date, date]:
    """上一个自然月窗口：当前月的上一个完整自然月，供延续性分析对比素材使用。"""
    prev_end = window_start - timedelta(days=1)
    prev_start = prev_end.replace(day=1)
    return prev_start, prev_end


def _sanitize_mermaid_label(text: str, max_length: int = 24) -> str:
    """LLM 生成的关系说明文本要拼进 mermaid flowchart 的边标签语法（`|"label"|`），
    双引号/竖线/方括号/换行会破坏语法结构——项目已经在 quadrantChart 坐标格式化上踩过
    一次真实的 mermaid 解析器 bug（见 _format_quadrant_coord），这里对自由文本做防御性
    清洗，不假设 LLM 输出一定是"安全"的短语。2026-07-09 追加两条：mermaid 用 `#数字;`
    表示 HTML 实体转义，裸露的 `#`（比如"发布了 #3 个版本"这类文本）会被解析器当成
    转义序列前缀吞掉后面的字符，替换成全角 ＃ 规避（视觉近似，不影响可读性，比转成
    HTML 实体更简单可靠）；`end`（大小写不敏感整词）是 flowchart 保留字，独立成一个
    标签时会破坏语法，命中时追加一个空格打破整词匹配。"""
    cleaned = _MERMAID_LABEL_UNSAFE_RE.sub("", text).strip()
    cleaned = cleaned.replace("#", "＃")
    if cleaned.lower() == "end":
        cleaned = cleaned + " "
    return cleaned[:max_length] if len(cleaned) > max_length else cleaned


def _lint_mermaid_block(code: str) -> bool:
    """对生成出来的 mermaid 代码块做结构性校验（纯字符串检查，不是真实的 mermaid
    语法解析器——原因见上方"mermaid 生成安全网"注释）。校验项：① ```mermaid/```
    围栏完整；② 声明的图类型是项目已知支持的四种之一；③ 方括号/圆括号/双引号都成对
    出现；④ quadrantChart 专属的 regression 断言——不含形如 "3.0" 的浮点数字面量
    （见 _format_quadrant_coord 的真实解析器 bug 说明），只在 quadrantChart 上检查，
    不对其他图类型生效，避免误伤 flowchart 标签里合法出现的"GPT-4.0"这类文本。
    校验不通过时，调用方应该丢弃这张图而不是把格式错误的代码块写进正文——图表是
    正文的锦上添花，少一张图不影响文档可读，但一段破损的 mermaid 代码块会让整个
    详情页的 markdown 渲染出问题。
    """
    lines = code.strip().split("\n")
    if len(lines) < 3 or lines[0] != "```mermaid" or lines[-1] != "```":
        return False
    diagram_type = lines[1].strip()
    if not diagram_type.startswith(_MERMAID_DIAGRAM_TYPES):
        return False
    body = "\n".join(lines[1:-1])
    if body.count("[") != body.count("]"):
        return False
    if body.count("(") != body.count(")"):
        return False
    if body.count('"') % 2 != 0:
        return False
    if diagram_type.startswith("quadrantChart") and _MERMAID_BARE_FLOAT_RE.search(body):
        return False
    return True


def _build_relationship_chart(relationships: list[dict]) -> str:
    """机械生成 mermaid flowchart 代码块，可视化交叉验证/分歧关系网络。relationships
    在 _generate_topic_analysis 里已经做过候选素材校验（LLM 编造的、不在候选素材里的边
    已被过滤掉）且附带了 from_title/to_title（文章真实标题，不是 doc_id），这里只负责
    渲染，不重新校验。空列表时不画图（没有关系可视化就没意义）。corroborates 用绿色
    实线箭头，conflicts 用红色虚线箭头区分视觉语义。节点 id 用位置索引起别名（doc_id
    本身含连字符，直接当 mermaid 节点 id 会被词法分析器当成箭头语法的一部分），节点
    标签里放文章标题（不是 doc_id 字符串）——标题比 doc_id 更有信息量，读者一眼就能
    看懂这个节点是哪篇文章，不需要去猜 doc_id 对应什么内容。节点/边标签都经过
    _sanitize_mermaid_label 清洗，但文章标题是不受控的自由文本（不像 topic slug
    这类内部字典值），返回前额外过一遍 _lint_mermaid_block 兜底，未通过时返回空
    字符串——调用方（_render_analysis_dimensions）已经有 `if relationship_chart:`
    判断，空字符串会被当成"没有可视化内容"正常跳过，不需要调用方额外处理。"""
    if not relationships:
        return ""
    node_ids: dict[str, str] = {}
    lines = ["```mermaid", "flowchart LR"]
    for edge in relationships:
        for doc_id, title in ((edge["from_id"], edge["from_title"]), (edge["to_id"], edge["to_title"])):
            if doc_id not in node_ids:
                alias = f"n{len(node_ids)}"
                node_ids[doc_id] = alias
                label = _sanitize_mermaid_label(title, max_length=30)
                lines.append(f'    {alias}["{label}"]')
    for edge in relationships:
        src, dst = node_ids[edge["from_id"]], node_ids[edge["to_id"]]
        label = _sanitize_mermaid_label(edge["label"])
        if edge["relation"] == "corroborates":
            lines.append(f'    {src} -->|"✅ {label}"| {dst}')
        else:
            lines.append(f'    {src} -.->|"⚡ {label}"| {dst}')
    lines.append("```")
    code = "\n".join(lines)
    return code if _lint_mermaid_block(code) else ""


def _render_analysis_dimensions(analysis: dict) -> str:
    """渲染深度分析正文（深度内容总结+延续性+交叉验证+分歧+新兴信号+关系图），不含标题/
    参考文章——月报（2026-07-09 深度改版二）按子主题多次调用组装多章节报告，也被
    _render_topic_analysis_section（周报单 topic 用）复用，避免两处重复渲染逻辑。"""
    lines = [
        analysis["deep_summary"],
        "",
        f"**延续性**：{analysis['continuity']}",
        "",
        f"**交叉验证**：{analysis['cross_validation']}",
        "",
        f"**分歧**：{analysis['tensions']}",
        "",
        f"**新兴信号**：{analysis['emerging']}",
    ]
    relationship_chart = _build_relationship_chart(analysis["relationships"])
    if relationship_chart:
        lines.extend(["", relationship_chart])
    return "\n".join(lines)


def _render_topic_analysis_section(
    topic_slug: str, analysis: dict, representatives: list[dict] | None = None
) -> str:
    """渲染单个 topic 的深度分析小节（周报专用：一个热门 topic 一段五维度分析）：
    topic_heading + 五维度分析正文 + 可选的"参考文章"链接列表（保留快速跳转入口）。
    月报（2026-07-09 深度改版二起）不再调用这个函数——月报现在是"一个 topic 拆成多个
    子主题小节"的结构，直接用 _render_analysis_dimensions 按子主题组装，见
    _build_topic_deep_dive_record。
    """
    lines = [topic_heading(topic_slug), "", _render_analysis_dimensions(analysis)]
    if representatives:
        lines.append("")
        lines.append("**参考文章**")
        lines.extend(
            f"- [[{r['doc_id']}]] {r['title']}（来源：{r['source_name']}）：{r['gist']}"
            for r in representatives
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

    # 每个热门 topic 一个深度分析小节（延续性/交叉验证/分歧/新兴信号 + 关系图），取代
    # 早期版本"机械 bullet 列表"——那个版本完全没有 LLM 参与，是"只是罗列文章"观感的
    # 直接原因（见 .claude/memory/decisions.md 深度改版记录）。t["analysis"] 由
    # generate_deep_dive_activity 在调用本函数前逐个 topic 调 _generate_topic_analysis
    # 生成并写回 trending 条目。
    sections = (
        "\n\n".join(
            _render_topic_analysis_section(t["slug"], t["analysis"], t["representatives"]) for t in trending
        )
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
    # 每个 _build_*_chart 返回前都自带 _lint_mermaid_block 兜底，未通过校验时返回空
    # 字符串（正常情况下不会发生，这里只是防止某张图万一没通过校验时在正文里留下
    # 一段多余的空行）——过滤掉空字符串，不是过滤掉某个"图表位置"。
    charts = [c for c in charts if c]
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

    # link_targets：topic slug（deterministic，topic_heading() 渲染进小节开头）+ 代表
    # 文章（deterministic，"参考文章"bullet 列表）+ 分析正文里实际引用到的素材 wikilink
    # （zettels ∪ fulltext_ids 候选集合内、且真的被 LLM 生成的五段分析文本引用到的那部分，
    # 用 _extract_cited_doc_ids 同monthly一致的方式过滤，不制造悬空/无意义边）。
    link_targets: list[str] = []
    for t in trending:
        link_targets.append(t["slug"])
        link_targets.extend(r["doc_id"] for r in t["representatives"])
        zettel_ids = [z["doc_id"] for z in t["zettels"]]
        valid_material_ids = zettel_ids + [oid for oid in t["fulltext_ids"] if oid not in zettel_ids]
        analysis_text = "\n".join(
            [t["analysis"]["deep_summary"], t["analysis"]["continuity"], t["analysis"]["cross_validation"], t["analysis"]["tensions"], t["analysis"]["emerging"]]
        )
        link_targets.extend(_extract_cited_doc_ids(analysis_text, valid_material_ids))

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
# 纯函数：专题月报（M11 新增，可单测，不依赖 DB/LLM）
# ---------------------------------------------------------------------------

def _compute_monthly_topic_candidates(rows: list[dict]) -> list[dict]:
    """按 topic_slug 分组统计上月 total_count（原文数）/active_days（distinct 日期数），
    双门槛（MONTHLY_MIN_TOTAL_COUNT/MONTHLY_MIN_ACTIVE_DAYS）机械筛选达标 topic。不像
    周报那样再截断 top-N——预设 topic 桶数量本身有限（10 个），fan-out 规模天然有界，
    不需要二次截断；`uncategorized` 溢出桶不是真实聚类结果，不参与评选。返回值按
    total_count 降序排列，只是为了展示友好，不影响后续 fan-out 顺序。
    """
    grouped: dict[str, dict] = {}
    for row in rows:
        slug = row.get("topic_slug")
        if not slug or slug == PLACEHOLDER_TOPIC:
            continue
        bucket = grouped.setdefault(slug, {"slug": slug, "dates": set(), "count": 0})
        bucket["dates"].add(row["doc_date"])
        bucket["count"] += 1

    candidates = []
    for bucket in grouped.values():
        total_count = bucket["count"]
        active_days = len(bucket["dates"])
        if total_count < MONTHLY_MIN_TOTAL_COUNT or active_days < MONTHLY_MIN_ACTIVE_DAYS:
            continue
        candidates.append({"slug": bucket["slug"], "total_count": total_count, "active_days": active_days})
    candidates.sort(key=lambda c: c["total_count"], reverse=True)
    return candidates


def _select_monthly_zettel_material(zettels: list[dict], limit: int = MONTHLY_ZETTEL_LIMIT) -> list[dict]:
    """本月该 topic 全部 zettel 若超过上限，机械截断取最新 N 条（按 doc_date 降序），
    不做二次 LLM 筛选——避免叙事阶段偷偷做"哪些该保留"的跨条目判断。"""
    return sorted(zettels, key=lambda z: z["doc_date"], reverse=True)[:limit]


def _select_fulltext_original_ids(zettels: list[dict], limit: int) -> list[str]:
    """从叙事骨架 zettel 素材（已经过 _select_monthly_zettel_material 截断）反查
    original_id，按 zettel 的 doc_date 降序取前 N 个（机械排序，不是"代表性"判断），
    供"深挖细节"取全文使用。zettel 的 doc_date 与其对应 original 的 doc_date 同源
    （均取自文章 published_at，见 aggregate.py::_build_zettel_record），可以直接复用
    排序，不需要再单独查一次 original 表确认。周报专用（月报 2026-07-09 深度改版二起
    改走 _cluster_topic_articles + _select_cluster_fulltext_ids，不再经过 zettel 反查）。
    """
    ordered = sorted(zettels, key=lambda z: z["doc_date"], reverse=True)
    seen: set[str] = set()
    original_ids: list[str] = []
    for z in ordered:
        original_id = z.get("original_id")
        if not original_id or original_id in seen:
            continue
        seen.add(original_id)
        original_ids.append(original_id)
        if len(original_ids) >= limit:
            break
    return original_ids


def _empty_topic_analysis(no_material_note: str) -> dict:
    """素材皆空时的机械兜底结构（不调 LLM）——理论上很少发生（周报/月报都有前置门槛
    保证一定量素材），只是防御性兜底，跟 _generate_intro 的空素材兜底同一个性质。"""
    return {
        "deep_summary": no_material_note,
        "continuity": "",
        "cross_validation": "",
        "tensions": "",
        "emerging": "",
        "relationships": [],
    }


def _generate_topic_analysis(
    topic_slug: str,
    window_label: str,
    materials: list[dict],
    fulltexts: list[dict],
    previous_materials: list[dict],
    summary_length_hint: str,
) -> dict:
    """周报（M10）+ 专题月报（M11）共用的深度分析引擎（2026-07-09 深度改版，同日又追加
    "深度内容总结"字段与动态篇幅，见 .claude/memory/decisions.md）：素材皆空时机械兜底
    不调 LLM，否则拼 prompt 用给定素材（部分原文全文为主 + 未展开全文的条目列表补充
    覆盖面 + 上一期同话题素材摘要）生成"深度内容总结 + 四个分析维度"——不是简单复述
    文章列表，深度总结要求基于精读全文写成，不能只依赖标题摘要。`window_label` 只影响
    prompt 措辞（"本周"/"本月"），`summary_length_hint` 是调用方算好的篇幅目标字符串
    （按素材篇数动态分档，见 `_dynamic_summary_length_hint`/`WEEKLY_SUMMARY_LENGTH_
    TIERS`/`MONTHLY_SUMMARY_LENGTH_TIERS`，本函数不关心分档逻辑，只负责把结果塞进
    prompt），窗口长度、素材规模均由调用方决定。`materials`/`previous_materials` 是
    通用的 `{doc_id, title, gist}` 形状列表——周报调用方传入真实 zettel 行，月报
    2026-07-09 深度改版二起改传入某个子主题下的原文行（不再门控于 zettel，见
    _cluster_topic_articles），两者对这个函数是透明的，字段形状一致即可复用。返回 dict
    （不是 pydantic 对象）方便下游直接用于组装 body_md/frontmatter；relationships 会
    先过滤成只保留候选素材（materials ∪ fulltexts）里真实存在的 doc_id 组合，防止 LLM
    编造的关系边被渲染进图表。
    """
    label = TOPIC_LABEL.get(topic_slug, topic_slug)
    if not materials and not fulltexts:
        return _empty_topic_analysis(f"{window_label}该专题暂无可用于生成分析的素材。")

    # 全文排在素材前面且措辞强调"精读依据"——用户反馈此前"全文只是补充细节参考"的
    # 措辞把全文降级成了装饰，深度总结实际上主要靠标题摘要拼凑；这里反过来，未展开
    # 全文的条目列表改成"整体覆盖面参考"，不再是主要写作依据。
    fulltext_blocks = [f"### [[{f['doc_id']}]] {f['title']}\n{f['body_md']}" for f in fulltexts]
    material_lines = [f"- [[{m['doc_id']}]] {m['title']}：{m['gist']}" for m in materials]
    previous_lines = (
        [f"- [[{m['doc_id']}]] {m['title']}：{m['gist']}" for m in previous_materials]
        if previous_materials
        else ["（上一期没有该话题的素材，或本话题是本期新出现的）"]
    )

    user_content = (
        f"{window_label}「{label}」相关的代表文章全文（深度总结应主要基于精读这些全文撰写）：\n\n"
        + "\n\n".join(fulltext_blocks) + "\n\n"
        f"{window_label}「{label}」相关的完整文章列表（含未展开全文的条目，仅用于把握整体"
        "覆盖面，不是深度总结的主要写作依据）：\n" + "\n".join(material_lines) + "\n\n"
        f"上一期「{label}」相关素材（供延续性对比参考）：\n" + "\n".join(previous_lines)
    )
    result = call_structured(
        model=_INTRO_MODEL,
        system_prompt=(
            f"你是资讯深度分析编辑。根据给定的{window_label}「{label}」相关代表文章全文（主要"
            "依据，要求精读，不能只扫一眼标题摘要）、完整文章列表（补充覆盖面）、以及上一期"
            "同话题素材，写出真正有深度的报告——不是逐条复述文章，而是要综合判断：① 深度"
            f"内容总结（{summary_length_hint}）：基于精读全文，讲清楚这段时期该话题具体发生"
            "了什么、涉及哪些关键产品/公司/数据/事件、为什么重要、彼此之间有什么关联，行文"
            "要像真正的报道/分析文章，有具体细节和脉络，不是空泛概括也不是逐条罗列文章标题；"
            "排版可以根据内容特点灵活组织——如果有多个并列的具体案例/产品/数据点，可以用"
            "小标题或分点呈现会更清晰，不必所有内容都挤在一个大段落里，但整体仍要保持叙事"
            "连贯，不是简单罗列条目；② 延续性：对比上一期素材，哪些方向在持续演进、有什么变化；③ 交叉验证：不同"
            "原文之间相互印证同一信号的地方，具体是哪些文章呼应了哪些内容；④ 分歧：不同原文"
            "观点/结论上的矛盾，具体是谁和谁的分歧；⑤ 新兴信号：真正新出现、此前未见的方向。"
            "只能引用素材中出现的事实，不能编造，没有真实对应内容的维度要如实说明'未见明显"
            "XX'，不能为了凑够维度硬编。重要概念/文章请用给定素材里标注的 [[doc_id]] 格式"
            "引用；用 Markdown **加粗** 标出深度内容总结里最核心的关键词/短语或核心结论短句"
            "（大致每 250-300 字标 1 处，篇幅越长处数按比例增加，不要堆砌）。另外用"
            "relationships 字段标注 0-6 条素材间的关系（相互印证或矛盾），from_id/to_id"
            "必须是给定素材里出现过的 doc_id，没有真实关系时留空，不能为了凑数量编造。"
        ),
        user_content=user_content,
        response_model=TopicNarrativeAnalysis,
    )

    # id_to_title 同时承担两个作用：① 候选素材存在性校验（LLM 编造的、不在候选素材里的
    # id 会被排除）② 给关系图节点提供真实标题（不是 doc_id 字符串，读者能一眼看懂）。
    id_to_title = {m["doc_id"]: m["title"] for m in materials}
    id_to_title.update({f["doc_id"]: f["title"] for f in fulltexts})
    relationships = [
        {
            "from_id": e.from_id,
            "from_title": id_to_title[e.from_id],
            "to_id": e.to_id,
            "to_title": id_to_title[e.to_id],
            "relation": e.relation,
            "label": e.label,
        }
        for e in result.relationships
        if e.from_id in id_to_title and e.to_id in id_to_title and e.from_id != e.to_id
    ]
    return {
        "deep_summary": result.deep_summary,
        "continuity": result.continuity,
        "cross_validation": result.cross_validation,
        "tensions": result.tensions,
        "emerging": result.emerging,
        "relationships": relationships,
    }


def _cluster_topic_articles(topic_slug: str, articles: list[dict]) -> list[dict]:
    """Stage 1（子主题聚类，2026-07-09 深度改版二，见 .claude/memory/decisions.md）：
    月报此前用 zettel 命中与否决定"哪些原文进深挖池"，但 zettel 只是"是否值得单独建
    原子笔记"这个独立判断，不代表文章本身有没有价值，不能拿来当深度报告的准入门槛
    （用户反馈：某些 topic 90% 的原文从未真正进入报告视野）。这里改为基于该 topic
    本月**全部**原文的 title+gist（不经过 zettel 过滤）识别 3-7 条真实存在的子主题
    线索，每条线索关联具体 doc_id。素材皆空/超过 CLUSTER_SKELETON_LIMIT 时机械截断，
    不做二次 LLM 筛选。机械校验每条线索的 doc_ids 必须真实存在于候选文章里，过滤掉
    编造的 id；doc_ids 校验后为空的线索整体丢弃。0 条有效线索时（LLM 判断失误或素材
    过于同质）调用方应该退化成"整个 topic 当一条线索"，不在这个函数里处理兜底
    （兜底逻辑属于组装层，不属于聚类这个纯粹的"分类判断"职责）。
    """
    if not articles:
        return []
    candidates = _select_monthly_zettel_material(articles, limit=CLUSTER_SKELETON_LIMIT)
    label = TOPIC_LABEL.get(topic_slug, topic_slug)
    article_lines = [f"- [[{a['doc_id']}]] {a['title']}：{a.get('gist') or '（无摘要）'}" for a in candidates]
    user_content = f"本月「{label}」全部原文列表（共{len(candidates)}篇）：\n" + "\n".join(article_lines)
    result = call_structured(
        model=_INTRO_MODEL,
        system_prompt=(
            f"你是资讯专题报告编辑。根据给定的本月「{label}」全部原文标题与摘要，识别出"
            "3-7条真实存在的子主题线索（同一大方向下不同的具体动态分支），每条线索关联"
            "属于它的原文 doc_id。只能引用给定素材中真实出现的 doc_id，不能编造；划分要"
            "基于素材实际呈现的情况，不要为了凑够数量硬拆，也不要为了省事把明显不同的"
            "方向硬合并成一条。允许有文章不属于任何清晰的子主题，不强制覆盖全部文章。"
        ),
        user_content=user_content,
        response_model=TopicClusterResult,
    )
    valid_ids = {a["doc_id"] for a in candidates}
    clusters = []
    for c in result.clusters:
        doc_ids = [d for d in c.doc_ids if d in valid_ids]
        if doc_ids:
            clusters.append({"heading": c.heading, "doc_ids": doc_ids})
    return clusters


def _select_cluster_fulltext_ids(doc_ids: list[str], articles_by_id: dict[str, dict], limit: int) -> list[str]:
    """从一个子主题线索的 doc_ids 里按 doc_date 降序取前 N 个（机械排序，不是"代表性"
    判断），供该子主题"深挖细节"取全文使用。每个子主题独立挑选，不是整个 topic 共用
    一个固定上限——子主题数越多，总深挖篇数自然越多。"""
    ordered = sorted(doc_ids, key=lambda d: articles_by_id[d]["doc_date"], reverse=True)
    return ordered[:limit]


def _extract_cited_doc_ids(text_content: str, valid_ids: list[str]) -> list[str]:
    """扫描正文实际出现的 `[[doc_id]]` wikilink，只保留候选素材里真实存在的 id——LLM
    生成的小节正文可能只引用给定素材的一部分，不是每条素材都要建反链边；同时防御性
    排除任何不在候选素材里的 id（`sync_document_links` 的 `to_id` 有外键约束，候选
    素材以外的 id 保证不存在于 documents 表，写入会失败）。保持 valid_ids 的原始
    顺序，不是 cited 集合的迭代顺序。"""
    cited = {m.group(1) for m in _WIKILINK_ID_RE.finditer(text_content)}
    return [doc_id for doc_id in valid_ids if doc_id in cited]


def _build_topic_deep_dive_record(
    topic_slug: str,
    window_start: date,
    window_end: date,
    entry_count: int,
    daily_counts: list[dict],
    cluster_sections: list[dict],
    fulltext_ids: list[str],
) -> dict:
    """组装专题月报记录（2026-07-09 深度改版二：多子主题章节结构，取代早期"整个 topic
    一段五维度分析"）。`cluster_sections` 是 `[{heading, doc_ids, analysis}, ...]`——
    `doc_ids` 是该子主题下全部原文 doc_id（供 link_targets 校验用），`analysis` 是该
    子主题的五维度分析 dict（`_generate_topic_analysis` 的返回值）。报告篇幅由子主题
    数量和素材丰富度自然决定，不是固定长度。
    """
    label = TOPIC_LABEL.get(topic_slug, topic_slug)
    month_label = f"{window_start.year}年{window_start.month:02d}月"
    title = f"{label}专题月报 · {month_label}"
    doc_id = f"deep-dive-{topic_slug}-{window_end.isoformat()}"

    # 机械拼一句子主题目录（不调 LLM——每个子主题自己的 overview 已经是 LLM 生成的深度
    # 分析，这里只是导航性质的一句话，纯字符串拼接不需要语义判断），帮读者快速定位。
    if cluster_sections:
        toc = "、".join(f"「{cs['heading']}」" for cs in cluster_sections)
        toc_line = f"本月「{label}」识别出 {len(cluster_sections)} 条主要线索：{toc}。"
    else:
        toc_line = _NO_CLUSTER_NOTE

    sections_md = "\n\n".join(
        f"### {cs['heading']}\n\n{_render_analysis_dimensions(cs['analysis'])}" for cs in cluster_sections
    )

    charts_section = ""
    if daily_counts and sum(d["count"] for d in daily_counts) > 0:
        bar_chart = _build_daily_volume_bar_chart(daily_counts, title=f"{label} · 本月每日产出量")
        if bar_chart:
            charts_section = bar_chart + "\n\n"

    stats_section = "## 本月数据统计\n\n" + "\n".join(
        [
            f"- 🗓️ 窗口范围：{window_start.isoformat()} ~ {window_end.isoformat()}",
            f"- 📰 原文总数：{entry_count}",
            f"- 🧵 子主题数：{len(cluster_sections)}",
            f"- 🔍 深挖原文篇数：{len(fulltext_ids)}",
        ]
    )
    # 不在 body_md 里重复拼标题：title 已经是独立字段，前端详情页单独渲染
    body_md = (
        topic_heading(topic_slug) + "\n\n" + toc_line + "\n\n"
        + sections_md + ("\n\n" if sections_md else "")
        + charts_section + stats_section + "\n"
    )

    # link_targets：topic_slug 是 topic_heading() 渲染进 body_md 开头的确定性引用，直接
    # 加入；每个子主题的五维度分析正文是 LLM 自由决定引用哪些素材的文本，只从该子主题
    # 自己的候选素材（cs["doc_ids"]，本函数调用前已经真实查过的存在文档）里筛选正文
    # 实际出现的 wikilink id——按子主题各自校验，不是拿全 topic 的素材池笼统校验，这样
    # 才能保证"某条边确实来自这个子主题引用的素材"，不会张冠李戴放行别的子主题的 id。
    link_targets = [topic_slug]
    for cs in cluster_sections:
        analysis = cs["analysis"]
        analysis_text = "\n".join(
            [analysis["deep_summary"], analysis["continuity"], analysis["cross_validation"], analysis["tensions"], analysis["emerging"]]
        )
        link_targets.extend(_extract_cited_doc_ids(analysis_text, cs["doc_ids"]))

    return {
        "doc_id": doc_id,
        "doc_type": "deep_dive",
        "title": title,
        "doc_date": window_end,
        "frontmatter": {
            "title": title,
            "doc_type": "deep_dive",
            "topic_slug": topic_slug,
            "window_start": window_start.isoformat(),
            "window_end": window_end.isoformat(),
            "entry_count": entry_count,
            "cluster_count": len(cluster_sections),
            "cluster_headings": [cs["heading"] for cs in cluster_sections],
            "deep_dive_original_ids": list(fulltext_ids),
        },
        "body_md": body_md,
        "content_hash": content_hash(body_md),
        "tags": [],
        "link_targets": link_targets,
    }


def _coerce_date(value: date | str) -> date:
    """跨 activity 边界传递的未指定字段类型的 dict，date 字段会被 Temporal 的
    pydantic_data_converter 退化成 ISO 字符串（M10 已踩过的坑，见
    .claude/memory/decisions.md）。双态兼容，不假设上游一定是哪种类型。"""
    return date.fromisoformat(value) if isinstance(value, str) else value


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
    """查 digest 素材 → 生成整体导语 → 逐个热门 topic 查 zettel/上周同期zettel/深挖全文
    并生成深度分析（2026-07-09 深度改版，取代早期"机械 bullet 列表"）→ 组装记录 →
    write_activity 落库（直接内部调用，不单独注册为 Temporal activity，参照
    aggregate_activity/refresh_original_document_activity 的既有模式）。"""
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

    # 每个热门 topic 独立查素材 + 生成深度分析——含全文的素材不跨这次 activity 边界
    # 传递（沿用 aggregate_activity 的 gRPC 4MB 教训），全部在本 activity 内完成。
    prev_window_start, prev_window_end = _previous_weekly_window(window_start)
    for t in trending:
        zettels_all = topic_deep_dive_list_zettel_documents_in_window(t["slug"], window_start, window_end)
        zettels = _select_monthly_zettel_material(zettels_all)
        fulltext_ids = _select_fulltext_original_ids(zettels, limit=WEEKLY_TOPIC_FULLTEXT_LIMIT)
        fulltexts = topic_deep_dive_fetch_original_fulltext(fulltext_ids)
        previous_zettels = _select_monthly_zettel_material(
            topic_deep_dive_list_zettel_documents_in_window(t["slug"], prev_window_start, prev_window_end)
        )
        t["zettels"] = zettels
        t["fulltext_ids"] = fulltext_ids
        length_hint = _dynamic_summary_length_hint(len(zettels), WEEKLY_SUMMARY_LENGTH_TIERS)
        t["analysis"] = _generate_topic_analysis(
            t["slug"], "本周", zettels, fulltexts, previous_zettels, summary_length_hint=length_hint
        )

    record = _build_deep_dive_record(window_start, window_end, trending, entry_count, digests, intro, daily_counts)
    written = write_activity([record])
    activity.logger.info(
        f"generate_deep_dive_activity: {record['doc_id']} 写入完成，"
        f"热门话题 {len(trending)} 个，覆盖 {entry_count} 篇原文，{len(digests)} 篇 digest"
    )
    return {"written": written, "doc_id": record["doc_id"], "trending_topic_count": len(trending)}


# ---------------------------------------------------------------------------
# activity 入口：专题月报（M11 新增）
# ---------------------------------------------------------------------------

@activity.defn
def compute_topic_deep_dive_candidates_activity(window_start: date, window_end: date) -> list[dict]:
    """纯查询 + 内存聚合，机械筛出上月达标的 topic 桶——只返回 slug/count 级别的小结构
    （不含正文），供 `TopicDeepDiveMonthlyWorkflow` 决定要给哪些 topic 做 child workflow
    fan-out。复用 `deep_dive_list_original_documents_in_window`（周报同一个查询函数，
    参数化窗口长度即可覆盖月度窗口，不需要新写一个只是窗口更长的查询）。
    """
    rows = deep_dive_list_original_documents_in_window(window_start, window_end)
    candidates = _compute_monthly_topic_candidates(rows)
    activity.logger.info(
        f"compute_topic_deep_dive_candidates_activity: 窗口 {window_start}~{window_end}，"
        f"原文 {len(rows)} 篇，达标 topic {len(candidates)} 个"
    )
    return candidates


@activity.defn
def compute_topic_deep_dive_stats_activity(params: TopicDeepDiveParams) -> dict:
    """单个达标 topic 的统计数字（entry_count/逐日分布）+ 子主题聚类结果，不含正文——
    含全文的深挖素材放到 generate_topic_deep_dive_activity 内部重新查询，不经过这次
    activity 边界，规避 aggregate_activity 曾经真实撞过的 gRPC 4MB 教训。聚类只需要
    title+gist（体积小），跟统计数字共用同一次查询结果，不用另开一次 activity
    （2026-07-09 深度改版二新增聚类步骤，见 .claude/memory/decisions.md）。0 条有效
    聚类线索时退化成"整个 topic 当一条线索"，不能因为聚类失败就让报告开天窗。
    """
    rows = topic_deep_dive_list_original_documents_in_window(
        params.topic_slug, params.window_start, params.window_end
    )
    daily_counts = _compute_daily_counts(rows, params.window_start, params.window_end)
    clusters = _cluster_topic_articles(params.topic_slug, rows)
    if not clusters and rows:
        clusters = [
            {
                "heading": TOPIC_LABEL.get(params.topic_slug, params.topic_slug),
                "doc_ids": [r["doc_id"] for r in rows],
            }
        ]
    activity.logger.info(
        f"compute_topic_deep_dive_stats_activity: {params.topic_slug} 窗口 "
        f"{params.window_start}~{params.window_end}，原文 {len(rows)} 篇，子主题 {len(clusters)} 条"
    )
    return {
        "topic_slug": params.topic_slug,
        "window_start": params.window_start,
        "window_end": params.window_end,
        "entry_count": len(rows),
        "daily_counts": daily_counts,
        "clusters": clusters,
    }


@activity.defn
def generate_topic_deep_dive_activity(payload: dict) -> dict:
    """逐个子主题查全文深挖素材（CLUSTER_FULLTEXT_LIMIT 篇/子主题，不是整个 topic 共用
    一个固定上限）+ 查上个月同 topic 原文（延续性对比素材）→ 逐子主题生成深度分析 →
    组装多章节记录 → write_activity 落库，全部在同一个 activity 内完成——避免含全文
    素材跨 activity 边界传递，重演 aggregate_activity 已经踩过的 gRPC 4MB 教训
    （2026-07-09 深度改版二：从"单个 topic 一段五维度分析"改为"逐子主题多段深度分析"，
    见 .claude/memory/decisions.md）。
    """
    topic_slug = payload["topic_slug"]
    window_start = _coerce_date(payload["window_start"])
    window_end = _coerce_date(payload["window_end"])
    entry_count = payload["entry_count"]
    daily_counts = payload["daily_counts"]
    clusters = payload["clusters"]

    # 重新查一次该 topic 本月全部原文（含 gist），供子主题深挖时取 title/gist 素材——
    # 不跨 activity 边界传全文，stats activity 只传了 clusters 的 heading/doc_ids 结构。
    articles = topic_deep_dive_list_original_documents_in_window(topic_slug, window_start, window_end)
    articles_by_id = {a["doc_id"]: a for a in articles}

    prev_window_start, prev_window_end = _previous_monthly_window(window_start)
    previous_materials = _select_monthly_zettel_material(
        topic_deep_dive_list_original_documents_in_window(topic_slug, prev_window_start, prev_window_end)
    )

    cluster_sections = []
    all_fulltext_ids: list[str] = []
    for cluster in clusters:
        doc_ids = [d for d in cluster["doc_ids"] if d in articles_by_id]
        if not doc_ids:
            continue
        cluster_materials = [articles_by_id[d] for d in doc_ids]
        fulltext_ids = _select_cluster_fulltext_ids(doc_ids, articles_by_id, CLUSTER_FULLTEXT_LIMIT)
        fulltexts = topic_deep_dive_fetch_original_fulltext(fulltext_ids)
        length_hint = _dynamic_summary_length_hint(len(cluster_materials), MONTHLY_SUMMARY_LENGTH_TIERS)
        analysis = _generate_topic_analysis(
            topic_slug, "本月", cluster_materials, fulltexts, previous_materials,
            summary_length_hint=length_hint,
        )
        cluster_sections.append({"heading": cluster["heading"], "doc_ids": doc_ids, "analysis": analysis})
        all_fulltext_ids.extend(fulltext_ids)

    record = _build_topic_deep_dive_record(
        topic_slug, window_start, window_end, entry_count, daily_counts, cluster_sections, all_fulltext_ids
    )
    written = write_activity([record])
    activity.logger.info(
        f"generate_topic_deep_dive_activity: {record['doc_id']} 写入完成，"
        f"子主题 {len(cluster_sections)} 条，深挖原文合计 {len(all_fulltext_ids)} 篇"
    )
    return {
        "written": written,
        "doc_id": record["doc_id"],
        "topic_slug": topic_slug,
        "cluster_count": len(cluster_sections),
    }

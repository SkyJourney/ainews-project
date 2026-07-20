"""M1 管道用到的数据结构：统一 entry schema（04 §2.2）与 LLM 结构化输出 schema。

结构化输出 schema 类只承载单个字段/单一场景，不要把多个不相关字段塞进同一个 schema
（保持每次 Instructor 调用的 response_model 单一、prompt 意图清晰）。
"""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, Field


class Entry(BaseModel):
    """fetch_activity 的统一输出（04 §2.2），未来 api/webfetch/script 分支复用同一结构。

    source_name 从 M2 起补上（filter_activity 的跨源同论文去重需要按源过滤条目，
    M3 起 filter_activity 会同时处理多个源合并后的条目，仅凭 batch 级别的单一
    source_name 已经不够用）。
    """

    title: str
    url: str
    source_name: str
    published: date | None = Field(default=None, description="发布日期，无法解析时为 None")
    raw_summary: str = ""
    low_confidence: bool = False
    extra: dict = Field(default_factory=dict)


class SourceConfig(BaseModel):
    """sources.yaml 里一条信息源记录（04 §2.1）。"""

    name: str
    tier: int
    perspective: str
    fetch_method: str
    reliability: str
    last_verified: date
    url: str | None = None
    bias: str | None = None


class PipelineParams(BaseModel):
    """AInewsPipelineWorkflow 的入参：batch_id 可选，留空时 workflow 内部用
    workflow.info().start_time 兜底生成（M7 起由 Temporal Schedule 触发，不再依赖
    Celery Beat 在 workflow 外部生成再传入）；手动触发 ad-hoc 批次时仍可显式指定。

    M3 起不再带 source_name——一次 pipeline 运行会对 sources.yaml 里全部活跃源做
    fetch fan-out（03 doc 的既定架构：fetch_activity × N，N=活跃源数），不是针对单一源。
    """

    batch_id: str | None = None


class ListingEntry(BaseModel):
    """webfetch 抓取方式的列表页单条抽取结果（04 §2.2）。"""

    title: str = Field(description="文章标题")
    url: str = Field(description="文章链接，可以是相对路径，后续会自动补全为绝对路径")
    published_raw: str = Field(default="", description="原始发布日期文本，找不到就给空字符串，不要编造")


class PageListing(BaseModel):
    """webfetch 列表页整体抽取结果：一个 tool schema 里含多条目，与其余单字段 schema 不同。"""

    entries: list[ListingEntry] = Field(description="列表页里全部真实文章条目，忽略导航/页脚/广告噪声")


class EnrichArticleParams(BaseModel):
    """EnrichArticleWorkflow 的入参：单篇文章 + 所属批次信息。

    source_name 从 entry.source_name 取，不再单独作为一个字段——一旦 M3 起
    一个 batch 同时处理多个源，两处字段值会不一致，只保留 Entry 上那份权威值。
    """

    entry: Entry
    batch_id: str


class TopicDeepDiveParams(BaseModel):
    """TopicDeepDiveWorkflow（M11 专题月报 child workflow）入参：单个达标 topic + 自然月
    窗口。显式用 pydantic 模型（不是裸 dict）声明 window_start/window_end 的 date 类型——
    M10 周报曾经因为跨 activity 边界传递未指定字段类型的 dict，导致 date 字段被
    pydantic_data_converter 退化成 ISO 字符串（见 .claude/memory/decisions.md），这里
    从一开始就用类型化模型规避同类问题，不需要下游再做 isinstance 兼容判断。
    """

    topic_slug: str
    window_start: date
    window_end: date


class DeepDiveParams(BaseModel):
    """DeepDiveWorkflow（M10 周报）入参：window_end 可选，留空时 workflow 内部用
    workflow.info().start_time 兜底生成（每周一 09:00 触发时覆盖到上周日为窗口终点，
    仿 PipelineParams.batch_id 的既定模式）；手动回补历史某一周的报告时可显式指定，
    例如用户要求重新生成上周报告时，不需要等到下周一 Schedule 自然触发才能补跑。
    """

    window_end: date | None = None


class PreflightResult(BaseModel):
    """preflight_activity 的输出：源健康检查结果（04 §2.1）。"""

    source_name: str
    reliability: str
    stale: bool = Field(description="last_verified 是否已超过 30 天")


class TitleTranslation(BaseModel):
    """标题翻译 tool schema：仅在 enrich 阶段判定非中文时调用一次。"""

    translated_title: str = Field(description="翻译成中文后的标题，保留专有名词原文+括号中文解释")


class ChunkTranslation(BaseModel):
    """正文分块翻译 tool schema：正文按段落切块后逐块调用，按序拼接。"""

    translated_text: str = Field(description="这一块正文翻译成中文的结果，逐段对应，不合并不总结")


class ArticleGist(BaseModel):
    """一段话摘要 tool schema。"""

    gist: str = Field(
        description="用一段中文话概括这篇文章讲了什么，供聚合/展示阶段使用；"
        "用 Markdown **加粗** 标出 2-4 处关键词/短语或不超过15字的核心结论短句，"
        "方便快速扫描抓重点，不整句加粗、不生造内容"
    )


class TranslationCompletenessReview(BaseModel):
    """翻译完整性复审 tool schema：机械 CJK 占比校验未通过时，独立判断这是真的翻译
    缺失，还是专有名词/品牌名/数据表格密度天然偏高导致的误判（04 §2.4 深度诊断新增，
    只在机械校验已经判定"不通过"之后触发，不替代机械校验，只是复核减少误杀）。"""

    is_complete: bool = Field(
        description="对照原文逐段核对：译文是否完整传达了原文全部信息（允许专有名词/品牌名/代码/数据表格保留英文或数字原样）"
    )
    reason: str = Field(description="判断依据，一句话说明具体理由")


class ClusterEntryAssignment(BaseModel):
    """聚类判断里单篇文章的分桶结果（04 §2.5）。不含 is_new 字段——是否新建 topic
    的最终判据是实际存储状态（查 documents 表），不能由模型自己的判断决定，
    这里只提供"建议的 topic_slug"，交给代码核验。
    """

    url: str = Field(description="对应文章的原文 URL，必须和输入的某一条完全一致，不能编造或遗漏")
    topic_slug: str = Field(description="建议分配到的 topic slug：优先从给定的预设桶里选，"
                             "确有必要且同类文章足够多时才建议一个新 slug（kebab-case）")
    zettel_worthy: bool = Field(
        description="是否值得升级为独立的原子笔记：概念/方法首次出现、重大事件锚点（半年后回看仍重要）、"
        "或含可复用洞察，三选一命中才算 true"
    )
    rationale: str = Field(description="一句话说明分桶依据和 zettel_worthy 判断理由")


class ClusterAssignment(BaseModel):
    """topic 聚类 tool schema：唯一允许跨文章判断的地方（04 §2.5），一次调用处理整批文章。"""

    assignments: list[ClusterEntryAssignment] = Field(
        description="本批次每一篇输入文章的分桶结果，数量必须与输入文章数一致，不能遗漏或新增条目"
    )


class DailyHighlights(BaseModel):
    """Daily TL;DR 的关键事件筛选 tool schema：只做"选哪几条"的跨文章比较判断，
    不重新生成摘要文本——TL;DR 展示文案直接复用 enrich 阶段已经产出的 gist。
    """

    highlight_urls: list[str] = Field(
        description="从输入文章里选出 3-5 条最值得放进今日 TL;DR 的关键事件对应 URL，"
        "必须是输入里出现过的 URL，不能编造"
    )


class TagAssignmentEntry(BaseModel):
    url: str = Field(description="对应文章的原文 URL，必须和输入的某一条完全一致")
    tags: list[str] = Field(
        description="2-5 个 kebab-case 全小写标签，覆盖技术领域/产品公司/事件类型/来源质量四个维度，"
        "不发明新分类轴，不打宽泛无信息量标签（如 'ai'/'news' 这类）"
    )


class TagAssignment(BaseModel):
    """Tags 四轴打标 tool schema（04 §2.5），批量处理整批文章。"""

    assignments: list[TagAssignmentEntry] = Field(
        description="本批次每一篇输入文章的打标结果，数量必须与输入文章数一致"
    )


class ArticleMetadata(BaseModel):
    """富元数据抽取 tool schema（04 §2.4）：只判断"这篇文章本身是什么"，不做跨文章判断。"""

    entities: list[str] = Field(description="文章中出现的关键实体列表（公司名/模型名/产品名/人名等专有名词）")
    content_type: str = Field(
        description="内容类型分类，选最贴切的一个：research_paper / product_announcement / "
        "opinion / tutorial / policy / industry_news / other"
    )
    novelty_keywords: list[str] = Field(
        description="辅助新颖度判断的关键词或短语（如'首次提出'、'突破性'、'渐进式改进'），"
        "只描述这篇文章本身呈现的信号，不要判断是否与其他文章重复"
    )


class DeepDiveIntro(BaseModel):
    """Deep Dive 周报导语 tool schema（M10）：唯一一次 LLM 调用，只生成叙事文本——"哪个
    topic 算热门"是机械规则算好后传入的既定事实，这里不做二次判断，也不能编造给定素材
    之外的事实。"""

    intro: str = Field(
        description="一段150-300字的中文导语，基于给定的本周热门话题统计与文章素材，"
        "概括本周AI领域整体动态与延续性趋势，只能引用素材中出现的事实，不能编造；"
        "用 Markdown **加粗** 标出 2-4 处最核心的关键词/短语或核心结论短句，方便快速扫描"
    )


class TopicCluster(BaseModel):
    """专题月报（M11）子主题聚类的一条线索（2026-07-09 深度改版二：从"zettel 门控素材"
    改为"全部原文聚类"）。"""

    heading: str = Field(description="子主题标题，8-16字，概括这条线索在讲什么，不要带 emoji（渲染时统一加）")
    doc_ids: list[str] = Field(
        description="属于这条子主题线索的原文 doc_id 列表，必须是给定素材里真实存在的 doc_id，不能编造"
    )


class TopicClusterResult(BaseModel):
    """子主题聚类 tool schema：一次结构化调用，基于该 topic 本月**全部**原文的标题+摘要
    （不经过 zettel 过滤——zettel 只是"是否值得单独建原子笔记"的独立判断，不代表文章
    本身有没有价值，不能拿来当深度报告的准入门槛）识别出真实存在的子主题线索。LLM 只做
    "这些文章能分成几条线索"的划分判断，不做"哪条线索该不该收录"这类门槛判断（本月是否
    出报告是机械双门槛已经前置决定的）。"""

    clusters: list[TopicCluster] = Field(
        description="3-7 条真实存在的子主题线索，按素材实际呈现的情况划分，不要为了凑够"
        "数量硬拆，也不要为了省事把明显不同的方向硬合并成一条；允许有文章不属于任何"
        "清晰的子主题，不强制覆盖全部文章"
    )


class TopicRelationshipEdge(BaseModel):
    """素材间的一条关系边（2026-07-09 新增，配合 TopicNarrativeAnalysis 的交叉验证/分歧
    维度做可视化）：`from_id`/`to_id` 必须是给定素材里真实存在的 doc_id，代码侧会校验、
    丢弃任何不在候选素材里的边（防止编造关系），不是靠 LLM 自觉。"""

    from_id: str = Field(description="给定素材里的 doc_id（zettel 或深挖原文）")
    to_id: str = Field(description="给定素材里的 doc_id（zettel 或深挖原文），不能与 from_id 相同")
    relation: Literal["corroborates", "conflicts"] = Field(
        description="两者关系：corroborates=相互印证同一信号，conflicts=观点/结论存在矛盾"
    )
    label: str = Field(description="8-16字关系说明，例如'均指向出口管制影响发布节奏'")


class TopicNarrativeAnalysis(BaseModel):
    """话题深度叙事分析 tool schema（周报 M10 + 专题月报 M11 共享，2026-07-09 深度改版，
    2026-07-09 同日又追加"深度内容总结"字段）：一次结构化调用生成"深度内容总结 + 四个
    分析维度"，取代早期"导语+小节"这种偏罗列/复述的结构。LLM 只整合分析，不判断"哪个
    话题算热门/该不该出报告"（机械规则已前置决定），只能引用给定素材（materials/原文
    深挖片段/上一期同话题素材）中的事实——四个分析维度里，任何一个维度在给定素材里
    没有真实对应内容时，必须如实说明"未见明显XX"，不能为了凑够维度编造。"""

    deep_summary: str = Field(
        description="篇幅适度的深度内容总结：基于精读给定的原文全文（不是只看标题摘要）"
        "综合写成的报告正文，讲清楚这段时期该话题具体发生了什么、涉及哪些关键产品/公司/"
        "数据/事件、为什么重要、彼此之间有什么关联，行文要像真正的报道/分析文章——有具体"
        "细节、有脉络，不是空泛概括也不是逐条罗列文章标题；具体篇幅以 system prompt 里的"
        "指引为准；只能引用素材中出现的事实，不能编造；重要概念/文章用给定素材里标注的"
        "[[doc_id]] 格式引用；用 Markdown **加粗** 标出全文最核心的关键词/短语或核心结论短句"
    )
    continuity: str = Field(
        description="延续性分析（100-200字）：结合给定的'上一期同话题素材'，指出哪些方向"
        "在延续演进、跟上期相比发生了什么变化；如果没有提供上一期素材，或本期与上期确实"
        "没有明显延续关系，如实说明'未见明显延续对比'，不能编造"
    )
    cross_validation: str = Field(
        description="交叉验证（100-200字）：不同原文之间相互印证、指向同一信号或结论的"
        "地方，需要说明具体是哪些文章呼应了哪些内容；如果给定素材之间没有明显的相互印证，"
        "如实说明'素材间未见明显交叉验证'，不能编造"
    )
    tensions: str = Field(
        description="分歧/矛盾（100-200字）：不同原文之间观点、结论或立场上的矛盾/分歧，"
        "需要说明具体是谁和谁的分歧；如果没有真实存在的分歧，如实说明'素材间未见明显分歧'，"
        "不能为了凑内容编造矛盾"
    )
    emerging: str = Field(
        description="新兴信号（100-200字）：本期素材中真正新出现、此前未见的方向/概念/"
        "事件；如果本期内容都是延续性质、没有真正新东西，如实说明'本期未见明显新兴信号'"
    )
    relationships: list[TopicRelationshipEdge] = Field(
        default_factory=list,
        description="0-6 条素材间的关系边，用于可视化交叉验证/分歧网络；只能引用给定素材"
        "里真实存在的 doc_id，没有真实存在的关系时返回空列表，不能为了凑数量编造",
    )

"""M1 管道用到的数据结构：统一 entry schema（04 §2.2）与 LLM 结构化输出 schema。

结构化输出 schema 类只承载单个字段/单一场景，不要把多个不相关字段塞进同一个 schema
（保持每次 Instructor 调用的 response_model 单一、prompt 意图清晰）。
"""

from __future__ import annotations

from datetime import date

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
    """AInewsPipelineWorkflow 的入参：batch_id 由 Celery Beat 触发时生成
    （依赖真实时钟，workflow 内部不允许自己生成，必须外部传入）。

    M3 起不再带 source_name——一次 pipeline 运行会对 sources.yaml 里全部活跃源做
    fetch fan-out（03 doc 的既定架构：fetch_activity × N，N=活跃源数），不是针对单一源。
    """

    batch_id: str


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

    gist: str = Field(description="用一段中文话概括这篇文章讲了什么，供聚合/展示阶段使用")


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

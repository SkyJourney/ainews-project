"""M1 管道用到的数据结构：统一 entry schema（04 §2.2）与 LLM 结构化输出 schema。

结构化输出 schema 类只承载单个字段/单一场景，不要把多个不相关字段塞进同一个 schema
（保持每次 Instructor 调用的 response_model 单一、prompt 意图清晰）。
"""

from __future__ import annotations

from datetime import date

from pydantic import BaseModel, Field


class Entry(BaseModel):
    """fetch_activity 的统一输出（04 §2.2），未来 api/webfetch/script 分支复用同一结构。"""

    title: str
    url: str
    published: date | None = Field(default=None, description="发布日期，无法解析时为 None")
    raw_summary: str = ""
    low_confidence: bool = False
    extra: dict = Field(default_factory=dict)


class SourceConfig(BaseModel):
    """sources.yaml 里一条信息源记录（04 §2.1）。M1 只用到 rss 分支必需的字段。"""

    name: str
    tier: int
    perspective: str
    fetch_method: str
    reliability: str
    last_verified: date
    url: str | None = None


class PipelineParams(BaseModel):
    """AInewsPipelineWorkflow 的入参：source_name + batch_id 由 Celery Beat 触发时生成
    （batch_id 依赖真实时钟，workflow 内部不允许自己生成，必须外部传入）。
    """

    source_name: str
    batch_id: str


class EnrichArticleParams(BaseModel):
    """EnrichArticleWorkflow 的入参：单篇文章 + 所属批次信息。"""

    entry: Entry
    source_name: str
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
    """一段话摘要 tool schema（M1 元数据抽取最简版，实体/内容类型/新颖度信号留 M4）。"""

    gist: str = Field(description="用一段中文话概括这篇文章讲了什么，供聚合/展示阶段使用")

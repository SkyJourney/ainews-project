"""enrich_activity 相关实现，见 04 §2.4。

边界约束：只判断"这篇文章本身是什么"，不做跨文章判断——那是 aggregate_activity 的事。
原文抓取实现状态①（direct，httpx+trafilatura）+ 状态②（Jina Reader 兜底，参考旧项目
fetch-with-assets.py / news-originalizer.md 已验证的设计：M1 实测 openai.com 的文章页
挂 Cloudflare 反爬挑战，direct 通道必然失败，state②是让 M1 选定的单一源能跑通的必要条件，
不是可选加强）。状态③ Fallback A/B（WebFetch 等价物 + 占位正文）留给 M4——那一层在旧系统
里依赖 Claude Code 内置的 WebFetch 工具，本项目要重新设计成经 LiteLLM 的抽取场景，是真正的
新工作，和"迁移已验证规则"性质不同。翻译完整性机械校验也留 M4，这里只做长文本分块。
"""

from __future__ import annotations

import hashlib

import httpx
import trafilatura
from temporalio import activity

from worker.db import upsert_enriched_article
from worker.llm_client import call_structured
from worker.schemas import ArticleGist, ChunkTranslation, TitleTranslation

USER_AGENT = "ainews-service/0.1 (+https://github.com/SkyJourney/ainews-project)"
TRANSLATE_MODEL = "deepseek-v4-flash"
GIST_MODEL = "deepseek-v4-flash"
MAX_CHUNK_CHARS = 2000

# Jina Reader 兜底触发条件，照搬旧项目 fetch-with-assets.py 已验证的清单
# （JINA_TRIGGER_ERRORS = http_400/403/429/503/timeout）；其余状态码（如 404）视为
# 真实错误，不掩盖，直接抛出交给 Temporal 重试/失败。
_JINA_READER_BASE = "https://r.jina.ai/"
_JINA_TRIGGER_STATUS = {400, 403, 429, 503}
_JINA_TIMEOUT_SECONDS = 45.0  # Jina 内部跑 headless render，比直连更宽松

# 04 §2.4 语言判断：标题含中文字符 / 正文前 N 字符 CJK 占比 >30%，视为已是中文
_CJK_RANGES = ((0x4E00, 0x9FFF), (0x3400, 0x4DBF), (0x3040, 0x30FF), (0xAC00, 0xD7A3))
_LANGUAGE_SAMPLE_CHARS = 500
_CJK_RATIO_THRESHOLD = 0.3


def _is_cjk_char(ch: str) -> bool:
    code = ord(ch)
    return any(lo <= code <= hi for lo, hi in _CJK_RANGES)


def _cjk_ratio(text: str) -> float:
    if not text:
        return 0.0
    return sum(1 for ch in text if _is_cjk_char(ch)) / len(text)


def needs_translation(title: str, body_md: str) -> bool:
    """纯函数，workflow 里直接调用来决定是否要跑 translate_activity（04 §2.4 语言判断）。"""
    already_chinese = _cjk_ratio(title) > 0 or _cjk_ratio(body_md[:_LANGUAGE_SAMPLE_CHARS]) > _CJK_RATIO_THRESHOLD
    return not already_chinese


def _chunk_paragraphs(body_md: str, max_chars: int = MAX_CHUNK_CHARS) -> list[str]:
    """按段落贪心分块，避免长文本单次撑爆上下文（用户在 M1 明确要求的分段翻译）。"""
    paragraphs = body_md.split("\n\n")
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0

    for para in paragraphs:
        para_len = len(para) + 2
        if current and current_len + para_len > max_chars:
            chunks.append("\n\n".join(current))
            current = []
            current_len = 0
        current.append(para)
        current_len += para_len

    if current:
        chunks.append("\n\n".join(current))
    return chunks


def _fetch_direct(url: str) -> str | None:
    """状态①：direct 通道。命中 Jina 触发条件时返回 None 交给调用方走兜底；
    其余异常（如真实的 404）原样抛出，不掩盖真实错误。
    """
    try:
        response = httpx.get(url, headers={"User-Agent": USER_AGENT}, timeout=30.0, follow_redirects=True)
    except httpx.TimeoutException:
        return None
    if response.status_code in _JINA_TRIGGER_STATUS:
        return None
    response.raise_for_status()
    return trafilatura.extract(response.text, url=url, output_format="markdown", with_metadata=False)


def _fetch_via_jina(url: str) -> str | None:
    """状态②：Jina Reader 兜底，直接返回已抽取好的 markdown 正文，跳过 trafilatura 转换。"""
    response = httpx.get(_JINA_READER_BASE + url, timeout=_JINA_TIMEOUT_SECONDS, follow_redirects=True)
    response.raise_for_status()
    marker = "Markdown Content:"
    idx = response.text.find(marker)
    if idx == -1:
        return None
    return response.text[idx + len(marker):].strip() or None


@activity.defn
def fetch_original_activity(url: str) -> dict:
    """抓原文转 markdown：状态①失败（403/429/503/超时/抽取为空）自动走状态② Jina 兜底。
    两者都失败则抛异常交给 Temporal 重试；状态③ Fallback A/B 留 M4。
    """
    body_md = _fetch_direct(url)
    if body_md:
        return {"body_md": body_md, "fetch_channel": "direct"}

    body_md = _fetch_via_jina(url)
    if body_md:
        return {"body_md": body_md, "fetch_channel": "jina"}

    raise RuntimeError(f"direct 与 jina 两个抓取通道均未能拿到正文：{url}")


@activity.defn
def translate_activity(title: str, body_md: str) -> dict:
    """标题单独翻译一次；正文按段落分块逐块翻译再按序拼接（04 §2.4 翻译逻辑，M1 简化版）。"""
    title_result = call_structured(
        model=TRANSLATE_MODEL,
        system_prompt="你是专业的技术文章翻译助手，将标题翻译成中文，专有名词保留原文并在括号内给出中文解释。",
        user_content=title,
        response_model=TitleTranslation,
    )

    translated_chunks = []
    for chunk in _chunk_paragraphs(body_md):
        chunk_result = call_structured(
            model=TRANSLATE_MODEL,
            system_prompt=(
                "你是专业的技术文章翻译助手。把下面这段 Markdown 正文翻译成中文："
                "逐段对应，不合并、不总结、不评论；保留标题层级/代码块/公式/引用块等 Markdown 结构；"
                "专有名词保留原文并在首次出现时用括号给出中文解释；保留数字精确度。"
            ),
            user_content=chunk,
            response_model=ChunkTranslation,
        )
        translated_chunks.append(chunk_result.translated_text)

    return {
        "translated_title": title_result.translated_title,
        "translated_body_md": "\n\n".join(translated_chunks),
    }


@activity.defn
def gist_activity(title: str, body_md: str) -> str:
    """一段话摘要，基于保证是中文的标题+正文生成（M1 元数据抽取最简版）。"""
    result = call_structured(
        model=GIST_MODEL,
        system_prompt="你是新闻摘要助手，用一段中文话（80-150字）概括这篇文章讲了什么，不要逐段复述细节。",
        user_content=f"{title}\n\n{body_md}",
        response_model=ArticleGist,
    )
    return result.gist


@activity.defn
def upsert_article_activity(payload: dict) -> None:
    upsert_enriched_article(**payload)


def content_hash(body_md: str) -> str:
    return hashlib.sha256(body_md.encode("utf-8")).hexdigest()

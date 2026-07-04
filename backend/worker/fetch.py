"""preflight_activity + fetch_activity 完整版（04 §2.1/§2.2，M3 全部四种 fetch_method）。

四种抓取方式：
- rss：httpx + feedparser 直接解析
- api（arxiv-api / huggingface-daily-papers）：各自专用解析 + 限流/日期回退
- webfetch（anthropic-news / meta-ai-blog / the-batch / state-of-ai）：httpx 抓列表页
  HTML，经 LiteLLM 用 PageListing schema 抽取条目——03 doc 定的技术路线：这类源没有
  结构化 API，唯一需要 LLM 参与 Fetch 阶段的地方
- script（jiqizhixin / a16z-news-content）：参考旧项目已验证脚本移植的源特化解析

铁律：任何分支都不做时间窗口过滤，全量返回，交给 filter_activity 统一处理。
"""

from __future__ import annotations

import html as html_lib
import re
import time
from datetime import date, timedelta
from urllib.parse import urljoin

import feedparser
import httpx
from dateutil import parser as dateutil_parser
from defusedxml import ElementTree
from temporalio import activity

from worker.db import (
    get_source_reliability,
    init_source_health,
    record_fetch_failure,
    record_fetch_success,
)
from worker.llm_client import call_structured
from worker.schemas import Entry, PageListing, PreflightResult
from worker.source_registry import get_source, load_sources

STALE_AFTER_DAYS = 30
USER_AGENT = "ainews-service/0.1 (+https://github.com/SkyJourney/ainews-project)"
LISTING_EXTRACTION_MODEL = "deepseek-v4-flash"
MAX_LISTING_HTML_CHARS = 40000


@activity.defn
def preflight_activity(source_name: str) -> PreflightResult:
    """源健康检查（04 §2.1）：last_verified 超 30 天告警；reliability 以 source_health
    表的运行时状态为准（首次见到某个源时用 sources.yaml 的静态值播种）。
    """
    source = get_source(source_name)
    stale = (date.today() - source.last_verified) > timedelta(days=STALE_AFTER_DAYS)
    if stale:
        activity.logger.warning(
            f"信息源 {source_name} 的 last_verified={source.last_verified} 已超过 "
            f"{STALE_AFTER_DAYS} 天，需要人工核实"
        )

    reliability = get_source_reliability(source_name)
    if reliability is None:
        init_source_health(source_name, source.reliability)
        reliability = source.reliability

    return PreflightResult(source_name=source_name, reliability=reliability, stale=stale)


@activity.defn
def list_active_sources_activity() -> list[str]:
    """返回 sources.yaml 里 reliability != 'dead' 的全部源名，供 workflow 做 fetch fan-out。"""
    sources = load_sources()
    return [name for name, cfg in sources.items() if cfg.reliability != "dead"]


@activity.defn
def record_source_health_activity(source_name: str, success: bool, reason: str | None) -> None:
    """workflow 在 fetch_activity 的 Temporal 重试全部耗尽/成功后调用一次，更新健康状态机。"""
    if success:
        record_fetch_success(source_name)
    else:
        record_fetch_failure(source_name, reason or "unknown error")


@activity.defn
def fetch_activity(source_name: str) -> list[Entry]:
    source = get_source(source_name)

    if source.fetch_method == "rss":
        entries = _fetch_rss(source.url, source_name=source.name)
    elif source.fetch_method == "api":
        handler = _API_HANDLERS.get(source.name)
        if handler is None:
            raise NotImplementedError(f"api 方式尚未实现 {source.name} 的处理逻辑")
        entries = handler(source.url, source_name=source.name)
    elif source.fetch_method == "webfetch":
        entries = _fetch_webfetch(source.url, source_name=source.name)
    elif source.fetch_method == "script":
        handler = _SCRIPT_HANDLERS.get(source.name)
        if handler is None:
            raise NotImplementedError(f"script 方式尚未实现 {source.name} 的处理逻辑")
        entries = handler(source.url, source_name=source.name)
    else:
        raise NotImplementedError(f"未知的 fetch_method: {source.fetch_method!r}")

    # 源健康状态机（04 §2.1）：degraded 的源仍然抓，但过滤阶段要降权——用 low_confidence
    # 承载这个"降权"信号，复用已有的模糊地带兜底机制，不额外发明一套新字段。
    reliability = get_source_reliability(source.name) or source.reliability
    if reliability == "degraded":
        for entry in entries:
            entry.low_confidence = True

    return entries


# ---------------------------------------------------------------------------
# rss
# ---------------------------------------------------------------------------

def _fetch_rss(url: str, *, source_name: str) -> list[Entry]:
    response = httpx.get(url, headers={"User-Agent": USER_AGENT}, timeout=30.0, follow_redirects=True)
    response.raise_for_status()
    parsed = feedparser.parse(response.content)
    return [_rss_entry_to_entry(raw_entry, source_name=source_name) for raw_entry in parsed.entries]


def _rss_entry_to_entry(raw_entry, *, source_name: str) -> Entry:
    url = raw_entry.get("link", "")
    title = raw_entry.get("title", "")
    raw_summary = raw_entry.get("summary", "") or raw_entry.get("description", "")

    published = None
    published_parsed = raw_entry.get("published_parsed")
    if published_parsed:
        published = date(*published_parsed[:3])

    # low_confidence 判定条件见 04 §2.2：标题/URL 缺失、摘要缺失、日期无法解析、日期异常，任一命中即标记
    low_confidence = not url or not title or not raw_summary or published is None
    if published is not None and published > date.today() + timedelta(days=1):
        low_confidence = True

    return Entry(
        title=title,
        url=url,
        source_name=source_name,
        published=published,
        raw_summary=raw_summary,
        low_confidence=low_confidence,
        extra={"guid": raw_entry.get("id", "")},
    )


# ---------------------------------------------------------------------------
# api：arxiv-api（限流 3 秒/次）
# ---------------------------------------------------------------------------

ARXIV_RATE_LIMIT_SECONDS = 3
ARXIV_CATEGORIES = ["cs.AI", "cs.LG"]
ARXIV_MAX_RESULTS_PER_CATEGORY = 50
_ATOM_NS = {"atom": "http://www.w3.org/2005/Atom"}


def _fetch_arxiv(url: str, *, source_name: str) -> list[Entry]:
    entries: list[Entry] = []
    for i, category in enumerate(ARXIV_CATEGORIES):
        if i > 0:
            time.sleep(ARXIV_RATE_LIMIT_SECONDS)  # arXiv API 限流礼仪（04 §2.2）
        response = httpx.get(
            url,
            params={
                "search_query": f"cat:{category}",
                "sortBy": "submittedDate",
                "sortOrder": "descending",
                "max_results": ARXIV_MAX_RESULTS_PER_CATEGORY,
            },
            headers={"User-Agent": USER_AGENT},
            timeout=30.0,
        )
        response.raise_for_status()
        entries.extend(_parse_arxiv_atom(response.text, source_name=source_name))
    return entries


def _parse_arxiv_atom(xml_text: str, *, source_name: str) -> list[Entry]:
    root = ElementTree.fromstring(xml_text)
    entries = []
    for entry_el in root.findall("atom:entry", _ATOM_NS):
        title = " ".join((entry_el.findtext("atom:title", default="", namespaces=_ATOM_NS) or "").split())
        summary = " ".join((entry_el.findtext("atom:summary", default="", namespaces=_ATOM_NS) or "").split())
        url = (entry_el.findtext("atom:id", default="", namespaces=_ATOM_NS) or "").strip()

        published = None
        published_raw = entry_el.findtext("atom:published", default="", namespaces=_ATOM_NS)
        if published_raw:
            try:
                published = date.fromisoformat(published_raw[:10])
            except ValueError:
                pass

        low_confidence = not url or not title or not summary or published is None
        entries.append(
            Entry(
                title=title,
                url=url,
                source_name=source_name,
                published=published,
                raw_summary=summary,
                low_confidence=low_confidence,
                extra={},
            )
        )
    return entries


# ---------------------------------------------------------------------------
# api：huggingface-daily-papers（日期回退：今天无数据试昨天）
# ---------------------------------------------------------------------------

def _fetch_huggingface_daily_papers(url: str, *, source_name: str) -> list[Entry]:
    today = date.today()
    for query_date in (today, today - timedelta(days=1)):
        response = httpx.get(
            url, params={"date": query_date.isoformat()}, headers={"User-Agent": USER_AGENT}, timeout=30.0
        )
        response.raise_for_status()
        payload = response.json()
        if payload:
            return [_hf_paper_to_entry(item, source_name=source_name) for item in payload]
    return []  # 今天昨天都没有，返回空不算错误（04 §2.2）


def _hf_paper_to_entry(item: dict, *, source_name: str) -> Entry:
    paper = item.get("paper", {})
    arxiv_id = paper.get("id", "")
    url = f"https://arxiv.org/abs/{arxiv_id}" if arxiv_id else ""
    title = paper.get("title", "")
    summary = paper.get("summary", "")

    published = None
    published_raw = paper.get("publishedAt")
    if published_raw:
        try:
            published = date.fromisoformat(published_raw[:10])
        except ValueError:
            pass

    low_confidence = not url or not title or not summary or published is None
    return Entry(
        title=title,
        url=url,
        source_name=source_name,
        published=published,
        raw_summary=summary,
        low_confidence=low_confidence,
        extra={"upvotes": item.get("paper", {}).get("upvotes", 0)},
    )


_API_HANDLERS = {
    "arxiv-api": _fetch_arxiv,
    "huggingface-daily-papers": _fetch_huggingface_daily_papers,
}


# ---------------------------------------------------------------------------
# webfetch：列表页 HTML → 经 LiteLLM 抽取条目（03 doc：无结构化 API 的场景）
# ---------------------------------------------------------------------------

_NOISE_TAG_RE = re.compile(r"<(script|style|noscript|svg)\b[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL)

_RELATIVE_DATE_PATTERNS = [
    (re.compile(r"(\d+)\s*天前"), lambda m: timedelta(days=int(m.group(1)))),
    (re.compile(r"(\d+)\s*days?\s*ago", re.IGNORECASE), lambda m: timedelta(days=int(m.group(1)))),
    (re.compile(r"(\d+)\s*小时前"), lambda m: timedelta(hours=int(m.group(1)))),
    (re.compile(r"(\d+)\s*hours?\s*ago", re.IGNORECASE), lambda m: timedelta(hours=int(m.group(1)))),
    (re.compile(r"昨天|yesterday", re.IGNORECASE), lambda m: timedelta(days=1)),
    (re.compile(r"今天|today", re.IGNORECASE), lambda m: timedelta(days=0)),
]
_DATE_SANITY_MAX_DAYS = 3650  # 与当前时间差过大 → 视为不可信（04 §2.2 低置信度触发条件之一）


def _parse_flexible_date(raw: str, fetched_at: date) -> date | None:
    """相对日期优先于绝对日期换算；绝对日期做合理性校验（04 §2.2 webfetch 日期优先级规则）。"""
    if not raw:
        return None
    raw = raw.strip()

    for pattern, delta_fn in _RELATIVE_DATE_PATTERNS:
        m = pattern.search(raw)
        if m:
            return fetched_at - delta_fn(m)

    try:
        parsed = dateutil_parser.parse(raw, fuzzy=True).date()
    except (ValueError, OverflowError, TypeError):
        return None

    if abs((fetched_at - parsed).days) > _DATE_SANITY_MAX_DAYS:
        return None
    return parsed


def _fetch_webfetch(url: str, *, source_name: str) -> list[Entry]:
    response = httpx.get(url, headers={"User-Agent": USER_AGENT}, timeout=30.0, follow_redirects=True)
    response.raise_for_status()
    cleaned_html = _NOISE_TAG_RE.sub("", response.text)[:MAX_LISTING_HTML_CHARS]

    listing = call_structured(
        model=LISTING_EXTRACTION_MODEL,
        system_prompt=(
            "你是网页内容抽取助手。从这段列表页 HTML 中提取真实的文章/新闻条目列表，"
            "忽略导航栏、页脚、广告、订阅表单等噪声；每条给出标题、链接（可以是相对路径）、"
            "以及原始发布日期文本（找不到就给空字符串，不要编造）。"
        ),
        user_content=cleaned_html,
        response_model=PageListing,
    )

    fetched_at = date.today()
    entries = []
    for item in listing.entries:
        absolute_url = urljoin(url, item.url)  # 相对路径补全为绝对路径（04 §2.2）
        published = _parse_flexible_date(item.published_raw, fetched_at)
        low_confidence = not absolute_url or not item.title or published is None
        entries.append(
            Entry(
                title=item.title,
                url=absolute_url,
                source_name=source_name,
                published=published,
                raw_summary="",
                low_confidence=low_confidence,
                extra={"published_raw": item.published_raw},
            )
        )
    return entries


# ---------------------------------------------------------------------------
# script：jiqizhixin（anyfeeder 微信镜像，从 content:encoded 提取 mp 直链）
# ---------------------------------------------------------------------------

_MP_LINK_RE = re.compile(r'mp\.weixin\.qq\.com/s\?[^"\'<\s]+')
_JIQIZHIXIN_SUMMARY_CHARS = 200


def _fetch_jiqizhixin(url: str, *, source_name: str) -> list[Entry]:
    response = httpx.get(url, headers={"User-Agent": USER_AGENT}, timeout=30.0, follow_redirects=True)
    response.raise_for_status()
    parsed = feedparser.parse(response.content)

    entries = []
    for raw_entry in parsed.entries:
        title = raw_entry.get("title", "")
        published = None
        published_parsed = raw_entry.get("published_parsed")
        if published_parsed:
            published = date(*published_parsed[:3])

        content_encoded = ""
        content_list = raw_entry.get("content")
        if content_list:
            content_encoded = content_list[0].get("value", "")

        # RSS 的 <link> 是搜狗中间页，抓不到正文；从 content:encoded 提取 mp 真实直链
        # （2026-07-03 旧系统踩过的坑，见探查旧项目 jiqizhixin-fetch.py 时的记录）
        mp_match = _MP_LINK_RE.search(content_encoded)
        if mp_match:
            url_result = "https://" + mp_match.group(0).split("#")[0]
        else:
            url_result = raw_entry.get("link", "")  # 兜底：可能是搜狗中间页，标记低置信度

        raw_summary = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", content_encoded)).strip()[:_JIQIZHIXIN_SUMMARY_CHARS]

        low_confidence = not url_result or not title or "mp.weixin.qq.com" not in url_result or published is None
        entries.append(
            Entry(
                title=title,
                url=url_result,
                source_name=source_name,
                published=published,
                raw_summary=raw_summary,
                low_confidence=low_confidence,
                extra={"guid": raw_entry.get("id", "")},
            )
        )

    # 诊断统计透传（04 §2.2 旧系统教训）：mp 直链提取的成功/降级比例是唯一的事后诊断
    # 依据——2026-07-03 旧系统曾丢弃这个统计，导致一次 jiqizhixin 全降级异常无法确诊是
    # 脚本问题还是源数据时序问题。这里用 activity.logger 而不是塞进 Entry 字段，因为这是
    # 一次抓取整体的诊断信息，不是某一条 entry 的属性；Temporal Web UI 原生覆盖了旧系统
    # 靠 99-Log/*.md 承担的这层可观测性。
    with_mp_link = sum(1 for e in entries if "mp.weixin.qq.com" in e.url)
    activity.logger.info(
        f"jiqizhixin script_stats: total={len(entries)}, with_mp_link={with_mp_link}, "
        f"fallback_link={len(entries) - with_mp_link}"
    )
    return entries


# ---------------------------------------------------------------------------
# script：a16z-news-content（列表页 + 详情页补 datePublished，0.5s 礼仪间隔）
# ---------------------------------------------------------------------------

A16Z_LIST_MAX_ENTRIES = 20
A16Z_DETAIL_RATE_LIMIT_SECONDS = 0.5
_A16Z_BLOCK_RE = re.compile(r'<div [^>]*data-feed-item[^>]*>')
_A16Z_URL_RE = re.compile(r'<a href="(https?://a16z\.com/[^"]+)"')
_A16Z_DATE_PUBLISHED_RE = re.compile(r'"datePublished":\s*"([^"]+)"')
_A16Z_SKIP_PATH_SEGMENTS = ("/author/", "/tag/", "/category/", "/page/")


def _fetch_a16z(url: str, *, source_name: str) -> list[Entry]:
    response = httpx.get(url, headers={"User-Agent": USER_AGENT}, timeout=30.0, follow_redirects=True)
    response.raise_for_status()
    entries_meta = _parse_a16z_list(response.text, limit=A16Z_LIST_MAX_ENTRIES)

    entries = []
    for i, meta in enumerate(entries_meta):
        if i > 0:
            time.sleep(A16Z_DETAIL_RATE_LIMIT_SECONDS)  # 详情页串行礼仪间隔
        published = _fetch_a16z_detail_published(meta["url"])
        low_confidence = not meta["url"] or not meta["title"] or published is None
        entries.append(
            Entry(
                title=meta["title"],
                url=meta["url"],
                source_name=source_name,
                published=published,
                raw_summary="",
                low_confidence=low_confidence,
                extra={"category": meta.get("category")},
            )
        )

    # 诊断统计透传（04 §2.2 旧系统教训，同 jiqizhixin）：详情页 datePublished 抓取的
    # 成功/失败比例是事后诊断依据，不能静默丢弃。
    detail_failed = sum(1 for e in entries if e.published is None)
    activity.logger.info(
        f"a16z-news-content script_stats: list_parsed={len(entries_meta)}, "
        f"detail_fetched={len(entries) - detail_failed}, detail_failed={detail_failed}"
    )
    return entries


def _parse_a16z_list(html: str, *, limit: int) -> list[dict]:
    """列表页缺 entry 自身日期（04 §2.1 备注），只能拿 title/url/category，日期靠详情页补。"""
    entries: list[dict] = []
    seen_urls: set[str] = set()
    blocks = _A16Z_BLOCK_RE.split(html)[1:]

    for raw_block in blocks:
        block = raw_block[:3000]
        url_match = _A16Z_URL_RE.search(block)
        if not url_match:
            continue
        url = url_match.group(1).rstrip("/")
        if any(segment in url for segment in _A16Z_SKIP_PATH_SEGMENTS) or url in seen_urls:
            continue
        seen_urls.add(url)

        title_match = re.search(r'<a href="' + re.escape(url) + r'/?"[^>]*>\s*(.+?)\s*</a>', block, re.DOTALL)
        title = html_lib.unescape(re.sub(r"\s+", " ", title_match.group(1).strip())) if title_match else ""

        entries.append({"title": title, "url": url})
        if len(entries) >= limit:
            break

    return entries


def _fetch_a16z_detail_published(url: str) -> date | None:
    try:
        response = httpx.get(url, headers={"User-Agent": USER_AGENT}, timeout=20.0, follow_redirects=True)
        response.raise_for_status()
    except httpx.HTTPError:
        return None

    match = _A16Z_DATE_PUBLISHED_RE.search(response.text)
    if not match:
        return None
    try:
        return dateutil_parser.parse(match.group(1)).date()
    except (ValueError, OverflowError, TypeError):
        return None


_SCRIPT_HANDLERS = {
    "jiqizhixin": _fetch_jiqizhixin,
    "a16z-news-content": _fetch_a16z,
}

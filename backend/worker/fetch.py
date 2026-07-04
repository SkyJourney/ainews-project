"""preflight_activity + fetch_activity（M1 只实现 rss 分支，见 04 §2.1/§2.2）。"""

from __future__ import annotations

from datetime import date, timedelta

import feedparser
import httpx
from temporalio import activity

from worker.schemas import Entry, PreflightResult
from worker.source_registry import get_source

STALE_AFTER_DAYS = 30
USER_AGENT = "ainews-service/0.1 (+https://github.com/SkyJourney/ainews-project)"


@activity.defn
def preflight_activity(source_name: str) -> PreflightResult:
    """源健康检查：M1 最简版只检查 last_verified 是否超过 30 天（04 §2.1）。"""
    source = get_source(source_name)
    stale = (date.today() - source.last_verified) > timedelta(days=STALE_AFTER_DAYS)
    if stale:
        activity.logger.warning(
            f"信息源 {source_name} 的 last_verified={source.last_verified} 已超过 "
            f"{STALE_AFTER_DAYS} 天，需要人工核实"
        )
    return PreflightResult(source_name=source_name, reliability=source.reliability, stale=stale)


@activity.defn
def fetch_activity(source_name: str) -> list[Entry]:
    """铁律：不做任何时间窗口过滤，全量返回，交给 filter_activity 统一处理（04 §2.2）。"""
    source = get_source(source_name)
    if source.fetch_method != "rss":
        raise NotImplementedError(
            f"M1 只实现了 rss 抓取方式，{source_name} 的 fetch_method 是 {source.fetch_method!r}"
        )

    response = httpx.get(source.url, headers={"User-Agent": USER_AGENT}, timeout=30.0, follow_redirects=True)
    response.raise_for_status()
    parsed = feedparser.parse(response.content)

    return [_to_entry(raw_entry) for raw_entry in parsed.entries]


def _to_entry(raw_entry) -> Entry:
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
        published=published,
        raw_summary=raw_summary,
        low_confidence=low_confidence,
        extra={"guid": raw_entry.get("id", "")},
    )

"""filter_activity（M1 最简版）：同批次去重①②+ 14 天时效过滤（04 §2.3）。

跨源同论文去重、信噪比过滤、跨日 Jaccard 去重留 M2；执行顺序按 04 §2.3：
同批次去重 → 时效过滤（先于信噪过滤）。
"""

from __future__ import annotations

from datetime import date
from urllib.parse import urlunparse, urlparse

from temporalio import activity

from worker.schemas import Entry

STALENESS_DAYS = 14


def _normalize_host_path(url: str) -> str:
    """URL host+path 归一化：忽略大小写与 query/锚点（04 §2.3 同批次去重②）。"""
    parsed = urlparse(url)
    return urlunparse((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path.rstrip("/"), "", "", ""))


@activity.defn
def filter_activity(entries: list[Entry]) -> list[Entry]:
    kept: list[Entry] = []
    seen_exact: set[str] = set()
    seen_host_path: set[str] = set()
    dropped_dedup = 0
    dropped_stale = 0

    for entry in entries:
        exact_key = entry.url.strip()
        host_path_key = _normalize_host_path(entry.url)

        if exact_key in seen_exact or host_path_key in seen_host_path:
            dropped_dedup += 1
            continue

        if entry.published is not None and (date.today() - entry.published).days > STALENESS_DAYS:
            dropped_stale += 1
            continue

        seen_exact.add(exact_key)
        seen_host_path.add(host_path_key)
        kept.append(entry)

    activity.logger.info(
        f"filter_activity: 输入 {len(entries)} 条，同批次去重丢弃 {dropped_dedup} 条，"
        f"时效丢弃 {dropped_stale} 条（>{STALENESS_DAYS}天），保留 {len(kept)} 条"
    )
    return kept

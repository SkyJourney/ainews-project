"""filter_activity 完整版（04 §2.3）：同批次去重（4级）→ 跨源同论文去重 → 时效过滤 →
信噪比过滤 → 跨日去重（30 天窗口，Postgres `url_index` 表）。

执行顺序严格按 04 §2.3；这一整个阶段是"批量、纯规则、必须在 fan-out 前完成"——
去重本质是跨条目比较，不能拆到独立子线程里做。
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from urllib.parse import urlparse, urlunparse

from temporalio import activity

from worker.db import filter_cleanup_url_index, filter_lookup_url_index, filter_upsert_url_index
from worker.schemas import Entry

STALENESS_DAYS = 14
TITLE_SIMILARITY_THRESHOLD = 0.85
SUMMARY_OVERLAP_THRESHOLD = 0.9
SUMMARY_PREFIX_CHARS = 100
SUMMARY_EXCERPT_CHARS = 200
CROSS_DAY_INDEX_TTL_DAYS = 30
CROSS_DAY_RECENT_WINDOW_DAYS = 7
RE_COVERAGE_OVERLAP_THRESHOLD = 0.6

# 论文类源（跨源同论文去重的适用范围，04 §2.3）；M3 起才会真的同时出现在一个 batch 里
PAPER_SOURCES = {"arxiv-api", "huggingface-daily-papers"}

_STOPWORDS_EN = {
    "the", "a", "an", "of", "to", "in", "on", "for", "and", "or", "is", "are",
    "was", "were", "with", "by", "at", "from", "this", "that", "it", "as", "be",
    "have", "has", "will", "we", "our", "its",
}
_STOPWORDS_ZH = {
    "的", "了", "和", "是", "在", "有", "与", "及", "等", "对", "为", "也",
    "就", "都", "而", "并", "将", "被", "这", "那", "个", "上", "中",
}


# ---------------------------------------------------------------------------
# URL 归一化：两种口径用途不同，不要混用
# ---------------------------------------------------------------------------

def _normalize_host_path(url: str) -> str:
    """同批次去重②用：保留 scheme，忽略大小写与 query/锚点。"""
    parsed = urlparse(url)
    return urlunparse((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path.rstrip("/"), "", "", ""))


def normalize_url_for_index(url: str) -> str:
    """跨日去重索引用：小写、去 scheme、去 www、去 query/锚点、去尾斜杠（04 §2.3）。"""
    parsed = urlparse(url.lower())
    netloc = parsed.netloc[4:] if parsed.netloc.startswith("www.") else parsed.netloc
    return f"{netloc}{parsed.path.rstrip('/')}"


# ---------------------------------------------------------------------------
# Jaccard 重叠（中文按字切分去停用词/英文按词切分去停用词，04 §2.3 定义）
# 同时用于同批次的标题相似度、摘要重叠，与跨日去重的 re_coverage 判定
# ---------------------------------------------------------------------------

def _tokenize(text: str) -> set[str]:
    if not text:
        return set()
    if re.search(r"[一-鿿]", text):
        return {ch for ch in re.findall(r"[一-鿿]", text) if ch not in _STOPWORDS_ZH}
    return {w for w in re.findall(r"[a-zA-Z0-9]+", text.lower()) if w not in _STOPWORDS_EN}


def _jaccard_overlap(a: str, b: str) -> float:
    """overlap = |A∩B| / |A∪B|；任一方为空则 overlap=0（不豁免，04 §2.3）。"""
    if not a or not b:
        return 0.0
    tokens_a, tokens_b = _tokenize(a), _tokenize(b)
    if not tokens_a or not tokens_b:
        return 0.0
    return len(tokens_a & tokens_b) / len(tokens_a | tokens_b)


# ---------------------------------------------------------------------------
# ① 同批次去重（4 级优先，命中即判重复，保留信息最完整一条）
# ---------------------------------------------------------------------------

def _completeness_score(entry: Entry) -> tuple:
    return (entry.published is not None, len(entry.raw_summary), not entry.low_confidence)


def _is_same_batch_duplicate(a: Entry, b: Entry) -> bool:
    if a.url.strip() == b.url.strip():
        return True
    if _normalize_host_path(a.url) == _normalize_host_path(b.url):
        return True
    if _jaccard_overlap(a.title, b.title) >= TITLE_SIMILARITY_THRESHOLD:
        return True
    a_prefix, b_prefix = a.raw_summary[:SUMMARY_PREFIX_CHARS], b.raw_summary[:SUMMARY_PREFIX_CHARS]
    return _jaccard_overlap(a_prefix, b_prefix) >= SUMMARY_OVERLAP_THRESHOLD


def _dedup_same_batch(entries: list[Entry]) -> tuple[list[Entry], int]:
    kept: list[Entry] = []
    dropped = 0
    for entry in entries:
        match_idx = next(
            (i for i, existing in enumerate(kept) if _is_same_batch_duplicate(entry, existing)), None
        )
        if match_idx is None:
            kept.append(entry)
        else:
            if _completeness_score(entry) > _completeness_score(kept[match_idx]):
                kept[match_idx] = entry
            dropped += 1
    return kept, dropped


# ---------------------------------------------------------------------------
# ② 跨源同论文去重（范围限论文类源，标题归一化后精确相等即合并）
# ---------------------------------------------------------------------------

def _paper_title_key(title: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", "", title.lower())).strip()


def _dedup_cross_source_papers(entries: list[Entry]) -> tuple[list[Entry], int]:
    kept: list[Entry] = []
    seen_titles: dict[str, int] = {}
    dropped = 0

    for entry in entries:
        if entry.source_name not in PAPER_SOURCES:
            kept.append(entry)
            continue

        title_key = _paper_title_key(entry.title)
        if title_key not in seen_titles:
            seen_titles[title_key] = len(kept)
            kept.append(entry)
            continue

        # 命中同一篇论文：保留规范链接源（arxiv.org）版本。同一个源自己内部撞车
        # （如 arxiv-api 按 cs.AI/cs.LG 分类分别查询，同一篇论文跨类目被查两次）
        # 不算"跨源"，直接丢弃。
        canonical_idx = seen_titles[title_key]
        canonical = kept[canonical_idx]
        if (
            entry.source_name != canonical.source_name
            and "arxiv.org" in entry.url
            and "arxiv.org" not in canonical.url
        ):
            kept[canonical_idx] = entry
        dropped += 1

    return kept, dropped


# ---------------------------------------------------------------------------
# ③ 时效过滤（单一阈值，不分来源等级）
# ---------------------------------------------------------------------------

def _filter_staleness(entries: list[Entry]) -> tuple[list[Entry], int]:
    kept: list[Entry] = []
    dropped = 0
    for entry in entries:
        if entry.published is not None and (date.today() - entry.published).days > STALENESS_DAYS:
            dropped += 1
            continue
        kept.append(entry)
    return kept, dropped


# ---------------------------------------------------------------------------
# ④ 信噪比过滤（丢弃类别 + 保留信号覆盖所有丢弃规则）
# ---------------------------------------------------------------------------

_DROP_PATTERNS = [
    re.compile(p, re.IGNORECASE)
    for p in [
        r"完成.{0,6}轮融资", r"获.{0,10}投资", r"funding round", r"raises\s*\$", r"series [a-z] funding",
        r"招聘", r"内推", r"we'?re hiring", r"join our team",
        r"报名开启", r"直播预告", r"线下沙龙", r"\bwebinar\b", r"\bmeetup\b", r"register now",
        r"限时优惠", r"\bsponsored\b",
        r"编译自", r"compiled from", r"翻译自",
    ]
]
_KEEP_DOMAINS = {"arxiv.org", "github.com", "huggingface.co"}
_KEEP_PATTERNS = [
    re.compile(p, re.IGNORECASE)
    for p in [
        r"benchmark", r"\bsota\b", r"state-of-the-art", r"开源", r"open.?source",
        r"\bpaper\b", r"论文", r"policy", r"regulation", r"政策", r"监管",
        r"\bsafety\b", r"\balignment\b", r"安全对齐", r"对齐",
    ]
]


def _has_keep_signal(entry: Entry) -> bool:
    domain = urlparse(entry.url).netloc.lower()
    if any(domain == d or domain.endswith(f".{d}") for d in _KEEP_DOMAINS):
        return True
    haystack = f"{entry.title} {entry.raw_summary}"
    return any(p.search(haystack) for p in _KEEP_PATTERNS)


def _filter_noise(entries: list[Entry]) -> tuple[list[Entry], int]:
    kept: list[Entry] = []
    dropped = 0
    for entry in entries:
        haystack = f"{entry.title} {entry.raw_summary}"
        hit_drop = any(p.search(haystack) for p in _DROP_PATTERNS)
        if hit_drop and not _has_keep_signal(entry):
            dropped += 1
            continue
        kept.append(entry)
    return kept, dropped


# ---------------------------------------------------------------------------
# ⑤ 跨日去重（30 天滚动窗口索引；仅本函数可全权限读写 url_index，见 db.py 注释）
# ---------------------------------------------------------------------------

def _dedup_cross_day(entries: list[Entry], batch_id: str) -> tuple[list[Entry], int]:
    filter_cleanup_url_index(date.today() - timedelta(days=CROSS_DAY_INDEX_TTL_DAYS))

    kept: list[Entry] = []
    dropped = 0
    for entry in entries:
        normalized = normalize_url_for_index(entry.url)
        existing = filter_lookup_url_index(normalized)

        if existing is None:
            filter_upsert_url_index(
                normalized_url=normalized,
                first_seen_date=date.today(),
                first_seen_run=batch_id,
                title=entry.title,
                source_name=entry.source_name,
                raw_summary_excerpt=entry.raw_summary[:SUMMARY_EXCERPT_CHARS],
                batch_id_to_append=batch_id,
            )
            kept.append(entry)
            continue

        age_days = (date.today() - existing["first_seen_date"]).days
        if age_days > CROSS_DAY_RECENT_WINDOW_DAYS:
            # 已淡出，正常保留（不覆盖 first_seen_date，只追加本次 batch）
            filter_upsert_url_index(
                normalized_url=normalized,
                first_seen_date=existing["first_seen_date"],
                first_seen_run=existing["first_seen_run"],
                title=existing["title"],
                source_name=existing["source_name"],
                raw_summary_excerpt=existing["raw_summary_excerpt"],
                batch_id_to_append=batch_id,
            )
            kept.append(entry)
            continue

        # 任一方摘要为空则不豁免（04 §2.3）——空摘要不能算"新角度"，必须走默认丢弃，
        # 不能让 _jaccard_overlap 对空字符串返回的 0.0 被误判成"低重叠→新角度"。
        either_summary_empty = not entry.raw_summary or not existing["raw_summary_excerpt"]
        overlap = 0.0 if either_summary_empty else _jaccard_overlap(entry.raw_summary, existing["raw_summary_excerpt"])
        if not either_summary_empty and overlap <= RE_COVERAGE_OVERLAP_THRESHOLD:
            entry.extra["re_coverage"] = True
            filter_upsert_url_index(
                normalized_url=normalized,
                first_seen_date=existing["first_seen_date"],
                first_seen_run=existing["first_seen_run"],
                title=existing["title"],
                source_name=existing["source_name"],
                raw_summary_excerpt=existing["raw_summary_excerpt"],
                batch_id_to_append=batch_id,
            )
            kept.append(entry)
        else:
            dropped += 1

    return kept, dropped


@activity.defn
def filter_activity(entries: list[Entry], batch_id: str) -> list[Entry]:
    total = len(entries)

    after_same_batch, dropped_same_batch = _dedup_same_batch(entries)
    after_dedup, dropped_papers = _dedup_cross_source_papers(after_same_batch)

    after_staleness, dropped_stale = _filter_staleness(after_dedup)
    after_filter, dropped_noise = _filter_noise(after_staleness)

    kept, dropped_cross_day = _dedup_cross_day(after_filter, batch_id)

    # 统计自检（04 §2.3）：单调递减关系 + 丢弃计数之和应与总数对上
    if not (len(after_dedup) >= len(after_filter) >= len(kept)):
        raise RuntimeError(
            f"filter_activity 统计自检失败（单调递减关系不成立）：after_dedup={len(after_dedup)}, "
            f"after_filter={len(after_filter)}, kept={len(kept)}"
        )
    dropped_total = dropped_same_batch + dropped_papers + dropped_stale + dropped_noise + dropped_cross_day
    if dropped_total + len(kept) != total:
        raise RuntimeError(
            f"filter_activity 统计自检失败（丢弃计数之和对不上总数）："
            f"dropped_total={dropped_total} + kept={len(kept)} != total={total}"
        )

    activity.logger.info(
        f"filter_activity: 输入{total}条 → 同批次去重-{dropped_same_batch} → 跨源论文去重-{dropped_papers} "
        f"→ 时效过滤-{dropped_stale} → 信噪过滤-{dropped_noise} → 跨日去重-{dropped_cross_day} → 保留{len(kept)}条"
    )
    return kept

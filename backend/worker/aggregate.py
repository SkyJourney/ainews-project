"""aggregate_activity 完整版（04 §2.5）：唯一允许跨文章判断的地方。

职责边界（反复强调）：per-article 的 enrich_activity 只判断"这篇文章本身是什么"；
本模块只做"这一整批文章该怎么分桶/是否升级为原子笔记/怎么写进 Daily-Topic-Digest"
这类需要同时看到同批次全部文章才能下的判断——M5 前置调研（见 .claude/memory/decisions.md）
确认：事件级去重/合并早在 filter 阶段完成，到 aggregate 阶段时 entries 已经去重完毕，这里
唯一的跨文章判断就是"这批 entries 该怎么分桶"，不是再做一次事件合并。

文档类型边界（M5 新增，此前版本用 zettel 顶替了 original 的角色）：
- **original**：原文归档层，每篇 enriched 文章都建，doc id 按 URL 稳定 hash 生成（保证同一
  URL 跨批次重新处理时天然 upsert 到同一条记录），保证下游双链引用不断链。
- **zettel**：原子笔记，只有 zettel_worthy 的文章才创建/复用，走三级复用判断，复用时不改写
  已有内容。
- **topic**：按事件类型分桶，首次创建写完整 frontmatter，之后只追加不整体重写。
- **daily**：一天一篇，TL;DR + 昨日回顾 + 按主题分组（五种情形渲染）+ 数据统计。
- **digest**：独立消费本批次结构化数据（不反查 Daily/Topic 渲染结果），五项自检硬约束。
"""

from __future__ import annotations

import hashlib
import re
from datetime import date, datetime, timedelta

from temporalio import activity

from worker.db import (
    aggregate_get_daily_by_date,
    aggregate_get_document,
    aggregate_list_topic_slugs,
    aggregate_lookup_url_index_entry,
    aggregate_search_zettel_by_slug,
    document_id_exists,
    fetch_enriched_articles,
)
from worker.filter import normalize_url_for_index
from worker.llm_client import call_structured
from worker.schemas import ClusterAssignment, DailyHighlights, TagAssignment
from worker.source_registry import load_sources

MAX_SLUG_WORDS = 5
MAX_ID_COLLISION_RETRIES = 60

# 预设主题桶（04 §2.5，按事件类型分类，不按来源/公司分类）；uncategorized 是溢出桶，
# 复用同一个 slug 而不是新造名字。
TOPIC_BUCKETS = (
    "model-releases", "safety-alignment", "opensource-tools", "research-papers",
    "policy-regulation", "industry-moves", "funding-investment", "infra-hardware",
    "applications", "agents",
)
PLACEHOLDER_TOPIC = "uncategorized"

# Daily 正文里"按主题"小标题的 emoji + 中文名固定映射表（04 §2.5）。旧系统这块是每次
# LLM 现生成的，实测同一个 slug 在不同天出现过三种不同 emoji（如 infra-hardware 出现过
# ⚡/🖥️/🔧），不是刻意设计、只是从未固定过——这里改成固定表，不随批次自估飘移。
# 覆盖 TOPIC_BUCKETS + PLACEHOLDER_TOPIC 全部预设桶；未命中的 slug（历史遗留桶）用
# _humanize_slug 兜底，不报错。
TOPIC_EMOJI = {
    "model-releases": "🚀",
    "safety-alignment": "🛡️",
    "opensource-tools": "🛠️",
    "research-papers": "📄",
    "policy-regulation": "⚖️",
    "industry-moves": "🏢",
    "funding-investment": "💰",
    "infra-hardware": "🖥️",
    "applications": "🎯",
    "agents": "🤖",
    "uncategorized": "📌",
}
TOPIC_LABEL = {
    "model-releases": "模型发布",
    "safety-alignment": "安全对齐",
    "opensource-tools": "开源工具",
    "research-papers": "研究论文",
    "policy-regulation": "政策监管",
    "industry-moves": "产业动态",
    "funding-investment": "融资投资",
    "infra-hardware": "基础设施",
    "applications": "应用案例",
    "agents": "Agent",
    "uncategorized": "其他",
}


def _topic_heading(slug: str) -> str:
    emoji = TOPIC_EMOJI.get(slug, "📌")
    label = TOPIC_LABEL.get(slug, _humanize_slug(slug))
    return f"## {emoji} {label} [[{slug}]]"

# 分桶粒度规则（04 §2.5）：新 topic 本批次条数 < 3 视为"新领域涌现"证据不足，并入 uncategorized；
# 单个 topic 本批次 > 8 条只记录建议拆分子类，不自动拆分（自动拆分需要模型二次判断合理的子类
# 命名，一次性做到位风险较高，先记录留给后续人工/下一批次决定）。
MIN_NEW_TOPIC_BATCH_COUNT = 3
SPLIT_SUGGESTION_THRESHOLD = 8

# 04 §2.5 产出量软性指标：单次运行建议新建 3-10 张原子笔记，超过说明前置判断不够严格
# （只是诊断日志，不做任何自动裁剪——机械砍掉多出来的笔记没有依据判断该保留哪些）。
ZETTEL_YIELD_SOFT_MIN = 3
ZETTEL_YIELD_SOFT_MAX = 10

_CLUSTER_MODEL = "deepseek-v4-flash"
_TAG_MODEL = "deepseek-v4-flash"
_DAILY_HIGHLIGHT_MODEL = "deepseek-v4-flash"
# 聚类/打标调用的输出条目数随批次规模线性增长，真实批次实测 136 篇一次性调用触发
# instructor.IncompleteOutputException（超过 max_tokens 被截断）——单纯调高 max_tokens
# 只是把问题推迟到更大的批次，改成分块调用（沿用 enrich.py 翻译阶段"按段落分块"的既有
# 模式）才是不随批次规模增长而失效的稳定方案。
_CLUSTER_BATCH_SIZE = 20
_BATCH_CALL_MAX_TOKENS = 8000

_DIGEST_BLURB_MAX_CHARS = 120


def _slugify(title: str, max_words: int = MAX_SLUG_WORDS) -> str:
    """默认 slug 方案：取标题里的字母数字词（连字符/标点天然分词点），转小写 kebab-case。"""
    words = re.findall(r"[a-zA-Z0-9]+", title)[:max_words]
    return "-".join(w.lower() for w in words) if words else "untitled"


def _humanize_slug(slug: str) -> str:
    return " ".join(w.capitalize() for w in slug.split("-"))


def _content_hash(body_md: str) -> str:
    return hashlib.sha256(body_md.encode("utf-8")).hexdigest()


def _generate_doc_id(slug: str, used_in_batch: set[str]) -> str:
    """12 位分钟时间戳 + slug；同 HHMM 冲突顺延到下一分钟（04 §2.6，Zettel 专用 ID 方案）。"""
    candidate_time = datetime.now()
    for _ in range(MAX_ID_COLLISION_RETRIES):
        candidate_id = f"{candidate_time.strftime('%Y%m%d%H%M')}-{slug}"
        if candidate_id not in used_in_batch and not document_id_exists(candidate_id):
            used_in_batch.add(candidate_id)
            return candidate_id
        candidate_time += timedelta(minutes=1)
    raise RuntimeError(f"zettel ID 冲突顺延超过 {MAX_ID_COLLISION_RETRIES} 次：slug={slug}")


def _original_doc_id(url: str) -> str:
    """Original 归档层 doc id：按 URL 稳定 hash 生成（不是时间戳）——Original 与 URL 是
    稳定 1:1 关系，同一 URL 跨批次重新处理（如跨日去重"已淡出"后重新保留）时应该 upsert
    到同一条记录，而不是每次都生成新记录制造重复归档；时间戳方案只适合"选择性创建、
    一次生成不再变"的 Zettel。
    """
    return f"original-{hashlib.sha256(url.encode('utf-8')).hexdigest()[:12]}"


_FETCH_CHANNEL_NOTICES = {
    "jina": "主抓取通道失败（反爬拦截/超时等），使用 Jina Reader 兜底获取原文",
    "playwright": "direct 与 Jina 均失败，使用无头浏览器渲染兜底获取原文",
    "placeholder": "全部抓取通道失败（direct/Jina/Playwright），仅占位记录，正文缺失",
}


def _build_fallback_notice(fetch_channel: str, translation_fallback_notice: str | None) -> str | None:
    """04 §2.6 fallback_notice 三态：null=正常，字符串=降级原因；抓取降级与翻译降级是
    两个独立信号，都存在时合并成一条说明。
    """
    notices = [n for n in (_FETCH_CHANNEL_NOTICES.get(fetch_channel), translation_fallback_notice) if n]
    return "；".join(notices) if notices else None


# ---------------------------------------------------------------------------
# Stage A：Topic 聚类 + is_new 机械核验 + 分桶粒度规则
# ---------------------------------------------------------------------------

def _chunk_list(items: list, size: int) -> list[list]:
    return [items[i : i + size] for i in range(0, len(items), size)]


def _run_cluster_assignment(articles: list[dict], existing_topics: list[str]) -> dict[str, dict]:
    """跨文章聚类判断，按 `_CLUSTER_BATCH_SIZE` 分块调用（真实批次可能有 100+ 篇文章，
    单次调用输出会被 max_tokens 截断）。分块不影响正确性——`_apply_granularity_rules`
    是在全部分块结果合并后统一计算 is_new/分桶粒度规则，不依赖单次调用内部的一致性。
    """
    by_url: dict[str, dict] = {}
    for chunk in _chunk_list(articles, _CLUSTER_BATCH_SIZE):
        by_url.update(_run_cluster_assignment_chunk(chunk, existing_topics))
    return by_url


def _run_cluster_assignment_chunk(articles: list[dict], existing_topics: list[str]) -> dict[str, dict]:
    """单块聚类判断（一次跨文章 LLM 调用，覆盖这一块里的全部文章）。返回 {url: {topic_slug,
    zettel_worthy, rationale}}——不含 is_new，是否新建 topic 的最终判据是实际存储状态，
    不采信模型自己的判断（04 §2.5 `is_new` 强制规则）。
    """
    lines = [
        f"- url: {a['url']}\n  title: {a['translated_title'] or a['fetched_title']}\n  gist: {a['gist']}"
        for a in articles
    ]
    user_content = (
        f"预设主题桶：{', '.join(TOPIC_BUCKETS)}（也可以是这些之外确有必要的新 slug）\n"
        f"当前已存在的 topic slug：{', '.join(existing_topics) if existing_topics else '（无）'}\n\n"
        "待分类文章列表：\n" + "\n".join(lines)
    )
    result = call_structured(
        model=_CLUSTER_MODEL,
        system_prompt=(
            "你是新闻聚类助手。把下面每篇文章分配到一个 topic（按事件类型分类，不按来源/公司分类），"
            "优先复用已存在的 topic slug；只有确实是全新领域、且同类文章数量足够多时才建议新 slug。"
            "同时判断每篇文章是否值得升级为独立的原子笔记：概念/方法首次出现（这个概念/方法在此前"
            "从未被记录过）、重大事件锚点（半年后回看仍然重要，不是普通产品更新）、或含明确可复用的"
            "洞察（能被未来其他笔记引用的关键判断），三选一严格命中才算 true——从严判断，大多数文章"
            "都不应该 zettel_worthy，这一批（约几十篇）里预期只有 0-3 篇够格，其余都应该是 false。"
        ),
        user_content=user_content,
        response_model=ClusterAssignment,
        max_tokens=_BATCH_CALL_MAX_TOKENS,
    )

    valid_urls = {a["url"] for a in articles}
    by_url = {item.url: item for item in result.assignments if item.url in valid_urls}
    missing = valid_urls - by_url.keys()
    if missing:
        activity.logger.warning(
            f"aggregate_activity: 聚类结果遗漏 {len(missing)} 条文章，按 {PLACEHOLDER_TOPIC} 机械兜底"
        )
    return {
        url: {
            "topic_slug": (by_url[url].topic_slug if url in by_url else PLACEHOLDER_TOPIC),
            "zettel_worthy": (by_url[url].zettel_worthy if url in by_url else False),
            "rationale": (by_url[url].rationale if url in by_url else "聚类结果未覆盖，机械兜底"),
        }
        for url in valid_urls
    }


def _apply_granularity_rules(cluster_by_url: dict[str, dict], existing_topics: set[str]) -> dict[str, dict]:
    """04 §2.5 分桶粒度规则的机械后处理（纯函数，不碰 DB/LLM）：
    - is_new 的唯一依据是 existing_topics（调用方传入的实际存储状态快照）
    - 新 topic 本批次条数 < MIN_NEW_TOPIC_BATCH_COUNT 时降级并入 PLACEHOLDER_TOPIC
    - 单个 topic 本批次条数 > SPLIT_SUGGESTION_THRESHOLD 时只记录建议拆分，不自动执行
    """
    proposed_counts: dict[str, int] = {}
    for item in cluster_by_url.values():
        proposed_counts[item["topic_slug"]] = proposed_counts.get(item["topic_slug"], 0) + 1

    decisions: dict[str, dict] = {}
    for url, item in cluster_by_url.items():
        topic_slug = item["topic_slug"]
        is_new = topic_slug not in existing_topics
        if is_new and proposed_counts[topic_slug] < MIN_NEW_TOPIC_BATCH_COUNT:
            activity.logger.info(
                f"aggregate_activity: 新 topic 候选 {topic_slug!r} 本批次仅 "
                f"{proposed_counts[topic_slug]} 条，低于 {MIN_NEW_TOPIC_BATCH_COUNT} 条门槛，"
                f"并入 {PLACEHOLDER_TOPIC}"
            )
            topic_slug = PLACEHOLDER_TOPIC
            is_new = topic_slug not in existing_topics
        decisions[url] = {
            "topic_slug": topic_slug,
            "is_new_topic": is_new,
            "zettel_worthy": item["zettel_worthy"],
            "rationale": item["rationale"],
        }

    final_counts: dict[str, int] = {}
    for d in decisions.values():
        final_counts[d["topic_slug"]] = final_counts.get(d["topic_slug"], 0) + 1
    for slug, count in final_counts.items():
        if count > SPLIT_SUGGESTION_THRESHOLD:
            activity.logger.warning(
                f"aggregate_activity: topic {slug!r} 本批次 {count} 条，超过 "
                f"{SPLIT_SUGGESTION_THRESHOLD} 条，建议后续考虑拆分子类（本次不自动拆分）"
            )
    return decisions


# ---------------------------------------------------------------------------
# Stage B：Zettel 三级复用判断
# ---------------------------------------------------------------------------

def _decide_zettel(index_entry: dict | None, slug: str, used_ids: set[str]) -> dict:
    """Zettel 三级复用判断（04 §2.5）：① url_index.zettel_id 命中 → 复用
    ② 按 slug 后缀搜索现有 Zettel 文档命中 → 复用 ③ 都未命中 → 新建。
    复用时只返回既有 id，不改写该文档的既有内容。

    ①额外校验目标文档确实存在（`document_id_exists`）——`url_index` 与 `documents` 是
    两张独立的表，理论上不该出现"索引记得但文档已不存在"的悬空引用，但一旦真的出现
    （如手工维护/迁移导致两表不同步），直接信任索引值会在 write_activity 阶段触发
    `links` 表外键违反、让整批写入失败；这里退化为按 slug 走②/③，不放大成硬失败。
    ②本身直接查 `documents` 表，不存在这个悬空引用风险，不需要重复校验。
    """
    if index_entry and index_entry.get("zettel_id") and document_id_exists(index_entry["zettel_id"]):
        return {"action": "reuse", "zettel_id": index_entry["zettel_id"]}
    existing = aggregate_search_zettel_by_slug(slug)
    if existing:
        return {"action": "reuse", "zettel_id": existing}
    return {"action": "create", "zettel_id": _generate_doc_id(slug, used_ids)}


# ---------------------------------------------------------------------------
# Original / Zettel 记录组装
# ---------------------------------------------------------------------------

def _build_original_record(article: dict, original_id: str, zettel_id: str | None, decision: dict, tags: list[str]) -> dict:
    title = article["translated_title"] or article["fetched_title"]
    # 不在 body_md 里重复拼 "# {title}"：title 已经是独立字段（documents.title 列 +
    # frontmatter.title），前端详情页会单独渲染一次；重复拼会导致页面标题渲染两遍，
    # 对 original 类型还会跟原文本身自带的标题结构叠成三层 H1（真实批次验证时发现）。
    body_md = article["translated_summary"] or article["original_text"] or ""
    frontmatter = {
        "title": title,
        "doc_type": "original",
        "source_name": article["source_name"],
        "source_url": article["url"],
        "gist": article["gist"],
        "topic_slug": decision["topic_slug"],
        "entities": article.get("entities") or [],
        "content_type": article.get("content_type"),
        "word_count": article.get("word_count"),
        "fallback_notice": _build_fallback_notice(article["fetch_channel"], article.get("translation_fallback_notice")),
        "related_zettel_id": zettel_id,
    }
    return {
        "doc_id": original_id,
        "doc_type": "original",
        "title": title,
        "doc_date": article["published_at"] or date.today(),
        "frontmatter": frontmatter,
        "body_md": body_md,
        "content_hash": _content_hash(body_md),
        "tags": tags,
        "link_targets": [zettel_id] if zettel_id else [],
    }


def _build_zettel_record(article: dict, zettel_id: str, original_id: str, decision: dict, tags: list[str]) -> dict:
    title = article["translated_title"] or article["fetched_title"]
    # 同 _build_original_record：title 已是独立字段，不在 body_md 里重复拼标题
    body_md = f"{article['gist']}\n\n原文归档：[[{original_id}]]"
    frontmatter = {
        "title": title,
        "doc_type": "zettel",
        "topic_slug": decision["topic_slug"],
        "gist": article["gist"],
        "original_id": original_id,
        "rationale": decision["rationale"],
    }
    return {
        "doc_id": zettel_id,
        "doc_type": "zettel",
        "title": title,
        "doc_date": article["published_at"] or date.today(),
        "frontmatter": frontmatter,
        "body_md": body_md,
        "content_hash": _content_hash(body_md),
        "tags": tags,
        "link_targets": [original_id],
    }


# ---------------------------------------------------------------------------
# Stage C：Topic 文档写作（追加铁律）
# ---------------------------------------------------------------------------

def _topic_date_heading(d: date) -> str:
    return f"## {d.isoformat()}"


def _render_topic_entry_line(article: dict, zettel_id: str | None, original_id: str) -> str:
    title = article["translated_title"] or article["fetched_title"]
    link_id = zettel_id or original_id
    return f"- [[{link_id}]] {title}：{article['gist']}"


def _insert_topic_block(body_md: str, doc_date: date, entry_lines: list[str]) -> str:
    """Topic 追加铁律的文本层落地（04 §2.5/§2.6）：当天区块已存在则区块内追加，否则插入
    到最新区块之前（日期区块强制倒序，最新在前）——只做基于标题行匹配的文本插入，不引入
    完整 Markdown AST 解析，足够确定性且容易单测。
    """
    heading = _topic_date_heading(doc_date)
    lines = body_md.split("\n")
    if heading in lines:
        insert_at = lines.index(heading) + 1
        while insert_at < len(lines) and not lines[insert_at].startswith("## "):
            insert_at += 1
        return "\n".join(lines[:insert_at] + entry_lines + lines[insert_at:])

    for i, line in enumerate(lines):
        if line.startswith("## "):
            block = [heading, ""] + entry_lines + [""]
            return "\n".join(lines[:i] + block + lines[i:])

    # 历史文档里完全没有日期区块（理论上不应该发生——首建分支会直接带上第一个区块），
    # 兜底追加到文末，不让内容丢失。
    return body_md.rstrip("\n") + "\n\n" + heading + "\n\n" + "\n".join(entry_lines) + "\n"


def _build_topic_record(slug: str, entries: list[tuple[dict, str | None, str]], doc_date: date) -> dict:
    entry_lines = [_render_topic_entry_line(a, zid, oid) for a, zid, oid in entries]
    existing = aggregate_get_document(slug)

    if existing is None:
        title = _humanize_slug(slug)
        # 不在 body_md 里重复拼标题：title 已经是独立字段，前端详情页单独渲染
        body_md = f"{_topic_date_heading(doc_date)}\n\n" + "\n".join(entry_lines) + "\n"
        frontmatter = {
            "title": title,
            "doc_type": "topic",
            "topic_slug": slug,
            "created_date": doc_date.isoformat(),
            "last_updated_date": doc_date.isoformat(),
            "article_count": len(entries),
        }
    else:
        body_md = _insert_topic_block(existing["body_md"], doc_date, entry_lines)
        frontmatter = dict(existing["frontmatter"])
        frontmatter["last_updated_date"] = doc_date.isoformat()
        frontmatter["article_count"] = frontmatter.get("article_count", 0) + len(entries)
        title = frontmatter.get("title", _humanize_slug(slug))

    return {
        "doc_id": slug,
        "doc_type": "topic",
        "title": title,
        "doc_date": doc_date,
        "frontmatter": frontmatter,
        "body_md": body_md,
        "content_hash": _content_hash(body_md),
        "tags": [],
        "link_targets": [zid or oid for _, zid, oid in entries],
    }


# ---------------------------------------------------------------------------
# Stage D：Daily 文档写作
# ---------------------------------------------------------------------------

def _select_daily_highlights(articles: list[dict]) -> list[str]:
    """TL;DR 关键事件筛选：只做"选哪几条"的跨文章比较判断，文案直接复用已有的 gist，
    不重新生成摘要文本。批次本身就 <=5 篇时跳过 LLM 调用，全部纳入。
    """
    if len(articles) <= 5:
        return [a["url"] for a in articles]

    lines = [
        f"- url: {a['url']}\n  title: {a['translated_title'] or a['fetched_title']}\n  gist: {a['gist']}"
        for a in articles
    ]
    result = call_structured(
        model=_DAILY_HIGHLIGHT_MODEL,
        system_prompt="你是新闻编辑。从下面这批文章里选出 3-5 条最值得放进今日 TL;DR 的关键事件。",
        user_content="\n".join(lines),
        response_model=DailyHighlights,
    )
    valid_urls = {a["url"] for a in articles}
    return [u for u in result.highlight_urls if u in valid_urls][:5]


def _classify_daily_entry(article: dict, ctx: dict) -> str:
    """五种情形分类（04 §2.5 Daily 写作结构）。"""
    original_missing = article["fetch_channel"] == "placeholder"
    if ctx["is_recap"]:
        return "recap_with_zettel" if ctx["zettel_id"] else "recap_not_upgraded"
    if ctx["zettel_id"] and original_missing:
        return "has_zettel_original_missing"
    if ctx["zettel_id"]:
        return "has_zettel_has_original"
    return "no_zettel_has_original"


_DAILY_ENTRY_MARKERS = {
    "has_zettel_has_original": "",
    "no_zettel_has_original": "",
    "has_zettel_original_missing": "（原文缺失）",
    "recap_with_zettel": "🔄",
    "recap_not_upgraded": "🔄（未升级为原子笔记）",
}


def _render_daily_entry_line(article: dict, ctx: dict) -> str:
    title = article["translated_title"] or article["fetched_title"]
    link_id = ctx["zettel_id"] or ctx["original_id"]
    category = _classify_daily_entry(article, ctx)
    marker = _DAILY_ENTRY_MARKERS[category]
    suffix = f" {marker}" if marker else ""
    return f"- [[{link_id}]] {title}{suffix}：{article['gist']}"


def _compute_daily_stats(articles: list[dict], decisions: dict[str, dict], per_article_ctx: dict[str, dict]) -> dict:
    """机械统计（不靠 LLM 自估），供 Daily frontmatter.stats 使用。"""
    return {
        "articles_processed": len(articles),
        "topics_touched": len({d["topic_slug"] for d in decisions.values()}),
        "new_topics_created": len({d["topic_slug"] for d in decisions.values() if d["is_new_topic"]}),
        "zettel_created": sum(1 for ctx in per_article_ctx.values() if ctx["is_new_zettel"]),
        "zettel_reused": sum(1 for ctx in per_article_ctx.values() if ctx["zettel_id"] and not ctx["is_new_zettel"]),
    }


def _build_daily_record(
    articles: list[dict], decisions: dict[str, dict], per_article_ctx: dict[str, dict], today: date
) -> dict:
    by_url = {a["url"]: a for a in articles}
    highlight_urls = _select_daily_highlights(articles)
    tldr_lines = [f"- {by_url[u]['gist']}" for u in highlight_urls if u in by_url]

    yesterday = aggregate_get_daily_by_date(today - timedelta(days=1))
    recap_section = f"## 昨日回顾\n\n[[{yesterday['id']}]]\n\n" if yesterday else ""

    groups: dict[str, list[str]] = {}
    for article in articles:
        ctx = per_article_ctx[article["url"]]
        groups.setdefault(ctx["topic_slug"], []).append(_render_daily_entry_line(article, ctx))

    topic_sections = "\n\n".join(
        f"{_topic_heading(slug)}\n\n" + "\n".join(groups[slug]) for slug in sorted(groups)
    )

    stats = _compute_daily_stats(articles, decisions, per_article_ctx)
    stats_section = "## 本日数据统计\n\n" + "\n".join(f"- {k}：{v}" for k, v in stats.items())

    # 不在 body_md 里重复拼标题：title 已经是独立字段，前端详情页单独渲染
    body_md = (
        "## TL;DR\n\n" + "\n".join(tldr_lines) + "\n\n"
        + recap_section
        + topic_sections + "\n\n"
        + stats_section + "\n"
    )

    # 主题小标题现在带 [[slug]] wikilink（见 _topic_heading），一并计入 link_targets，
    # 这样 Topic 详情页的反链才能看到"被哪些 Daily 引用过"。
    link_targets = [ctx["zettel_id"] or ctx["original_id"] for ctx in per_article_ctx.values()]
    link_targets.extend(sorted(groups))
    if yesterday:
        link_targets.append(yesterday["id"])

    title = f"{today.isoformat()} AI 日报"
    return {
        "doc_id": today.isoformat(),
        "doc_type": "daily",
        "title": title,
        "doc_date": today,
        "frontmatter": {
            "title": title,
            "doc_type": "daily",
            "stats": stats,
            # M6 前端需要：本日实际涉及的 topic slug 列表（渲染 chips）+ 昨日日报 id
            # （渲染"昨日回顾"链接），都是已经计算过的值，只是之前没有写进 frontmatter。
            "topics": sorted(groups),
            "previous_daily": yesterday["id"] if yesterday else None,
        },
        "body_md": body_md,
        "content_hash": _content_hash(body_md),
        "tags": [],
        "link_targets": link_targets,
    }


# ---------------------------------------------------------------------------
# Stage E：Digest 文档写作（独立消费本批次结构化数据，五项自检硬约束）
# ---------------------------------------------------------------------------

def _truncate_blurb(text: str, max_chars: int = _DIGEST_BLURB_MAX_CHARS) -> str:
    """自检⑤的机械落地：硬性字符上限，不靠 LLM 自估长度。优先在最后一个句末标点处截断，
    避免半句话戛然而止；找不到标点就硬截断并加省略号。
    """
    if len(text) <= max_chars:
        return text
    truncated = text[:max_chars]
    for punct in ("。", "！", "？", "；"):
        idx = truncated.rfind(punct)
        if idx > 0:
            return truncated[: idx + 1]
    return truncated.rstrip() + "…"


def _build_digest_entries(articles: list[dict]) -> list[dict]:
    """Digest 五项自检硬约束落地（04 §2.5）：
    ① 禁止合成条目——直接遍历输入文章，不额外生成/合并条目
    ② source_name 必须与注册表逐字一致——校验失败跳过（记录警告，不整批失败）
    ③ URL 字段必填且直接取自结构化数据——直接用 article['url']，不做任何反查/改写
    ④ 去重自检——同 URL 不出现两次
    ⑤ 每条硬性字符上限——_truncate_blurb 机械截断
    """
    valid_sources = load_sources()
    seen_urls: set[str] = set()
    entries: list[dict] = []
    for article in articles:
        url = article["url"]
        if not url:
            activity.logger.warning("digest 自检失败（URL 缺失），跳过该条目")
            continue
        if url in seen_urls:
            activity.logger.warning(f"digest 去重自检命中，跳过重复 URL：{url}")
            continue
        if article["source_name"] not in valid_sources:
            activity.logger.warning(
                f"digest 自检失败（source_name 不在注册表，可能是拼写/大小写不一致）："
                f"{article['source_name']!r}，跳过该条目"
            )
            continue
        seen_urls.add(url)
        entries.append(
            {
                "title": article["translated_title"] or article["fetched_title"],
                "source_name": article["source_name"],
                "url": url,
                "blurb": _truncate_blurb(article["gist"]),
            }
        )
    return entries


def _build_digest_record(articles: list[dict], today: date) -> dict:
    entries = _build_digest_entries(articles)
    lines = [f"- **{e['title']}**（来源：{e['source_name']}）{e['blurb']} {e['url']}" for e in entries]
    # 不在 body_md 里重复拼标题：title 已经是独立字段，前端详情页单独渲染
    body_md = "\n".join(lines) + "\n"
    title = f"{today.isoformat()} AI Digest"
    return {
        "doc_id": f"digest-{today.isoformat()}",
        "doc_type": "digest",
        "title": title,
        "doc_date": today,
        "frontmatter": {"title": title, "doc_type": "digest", "entry_count": len(entries)},
        "body_md": body_md,
        "content_hash": _content_hash(body_md),
        "tags": [],
        # Digest 明确不用 wikilink（定位"去 wikilink、可独立分享打印"），不写 links 边。
        "link_targets": [],
    }


# ---------------------------------------------------------------------------
# Stage F：Tags 四轴打标
# ---------------------------------------------------------------------------

def _run_tag_assignment(articles: list[dict]) -> dict[str, list[str]]:
    """按 `_CLUSTER_BATCH_SIZE` 分块打标，理由同 `_run_cluster_assignment`——避免大批次
    单次调用输出被 max_tokens 截断。"""
    by_url: dict[str, list[str]] = {}
    for chunk in _chunk_list(articles, _CLUSTER_BATCH_SIZE):
        by_url.update(_run_tag_assignment_chunk(chunk))
    return by_url


def _run_tag_assignment_chunk(articles: list[dict]) -> dict[str, list[str]]:
    lines = [
        f"- url: {a['url']}\n  title: {a['translated_title'] or a['fetched_title']}\n  "
        f"gist: {a['gist']}\n  entities: {a.get('entities') or []}"
        for a in articles
    ]
    result = call_structured(
        model=_TAG_MODEL,
        system_prompt=(
            "你是打标助手。给每篇文章打 2-5 个 kebab-case 全小写标签，覆盖技术领域/产品公司/"
            "事件类型/来源质量四个维度，不要发明新分类轴，不要打宽泛无信息量标签（如 'ai'/'news'）。"
        ),
        user_content="\n".join(lines),
        response_model=TagAssignment,
        max_tokens=_BATCH_CALL_MAX_TOKENS,
    )
    valid_urls = {a["url"] for a in articles}
    by_url = {item.url: item.tags for item in result.assignments if item.url in valid_urls}
    for url in valid_urls - by_url.keys():
        by_url[url] = []
    return by_url


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

@activity.defn
def aggregate_activity(batch_id: str) -> list[dict]:
    articles = fetch_enriched_articles(batch_id)
    if not articles:
        activity.logger.info(f"aggregate_activity: batch_id={batch_id} 没有已完成富化的文章，跳过")
        return []

    today = date.today()
    existing_topics = set(aggregate_list_topic_slugs())
    cluster_by_url = _run_cluster_assignment(articles, sorted(existing_topics))
    decisions = _apply_granularity_rules(cluster_by_url, existing_topics)
    tags_by_url = _run_tag_assignment(articles)

    records: list[dict] = []
    used_zettel_ids: set[str] = set()
    per_article_ctx: dict[str, dict] = {}

    for article in articles:
        url = article["url"]
        decision = decisions[url]
        original_id = _original_doc_id(url)
        tags = tags_by_url.get(url, [])
        normalized = normalize_url_for_index(url)
        index_entry = aggregate_lookup_url_index_entry(normalized)

        zettel_id: str | None = None
        is_new_zettel = False
        if decision["zettel_worthy"]:
            slug = _slugify(article["fetched_title"])
            zettel_decision = _decide_zettel(index_entry, slug, used_zettel_ids)
            zettel_id = zettel_decision["zettel_id"]
            is_new_zettel = zettel_decision["action"] == "create"

        is_recap = bool(index_entry and index_entry["first_seen_date"] != today)
        per_article_ctx[url] = {
            "original_id": original_id,
            "zettel_id": zettel_id,
            "is_new_zettel": is_new_zettel,
            "topic_slug": decision["topic_slug"],
            "is_recap": is_recap,
        }

        records.append(_build_original_record(article, original_id, zettel_id, decision, tags))
        if is_new_zettel:
            records.append(_build_zettel_record(article, zettel_id, original_id, decision, tags))

    topic_groups: dict[str, list[tuple[dict, str | None, str]]] = {}
    for article in articles:
        ctx = per_article_ctx[article["url"]]
        topic_groups.setdefault(ctx["topic_slug"], []).append((article, ctx["zettel_id"], ctx["original_id"]))
    for slug, entries in topic_groups.items():
        records.append(_build_topic_record(slug, entries, today))

    records.append(_build_daily_record(articles, decisions, per_article_ctx, today))
    records.append(_build_digest_record(articles, today))

    new_topic_count = len({d["topic_slug"] for d in decisions.values() if d["is_new_topic"]})
    new_zettel_count = sum(1 for ctx in per_article_ctx.values() if ctx["is_new_zettel"])
    activity.logger.info(
        f"aggregate_activity: batch_id={batch_id} 原文 {len(articles)} 篇，"
        f"新建 topic {new_topic_count} 个，新建 zettel {new_zettel_count} 篇，共组装 {len(records)} 条文档记录"
    )
    if new_zettel_count > ZETTEL_YIELD_SOFT_MAX:
        activity.logger.warning(
            f"aggregate_activity: 本批次新建 {new_zettel_count} 张原子笔记，超过建议上限 "
            f"{ZETTEL_YIELD_SOFT_MAX}，可能是 zettel_worthy 判断偏松，建议关注"
        )
    elif new_zettel_count < ZETTEL_YIELD_SOFT_MIN:
        activity.logger.info(
            f"aggregate_activity: 本批次仅新建 {new_zettel_count} 张原子笔记，低于建议下限 "
            f"{ZETTEL_YIELD_SOFT_MIN}，可能是「低产日」（可接受）"
        )
    return records

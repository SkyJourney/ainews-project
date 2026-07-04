"""aggregate_activity（M1 最简版，见 04 §2.5）。

不做真实 topic 聚类 / is_new 判断——固定归入占位 topic，这是 M5 才要解决的问题
（真实聚类桶、Zettel 入选标准、复用三级判断都还没做）。这里只做：读取本批次
enriched articles → 逐条组装 zettel 记录（id/slug/frontmatter/body_md）。

body_md 用 gist（而不是完整译文）：完整译文归档对应的是"original"文档类型
（04 §2.6，M4/M5 才启用），M1 明确只做 zettel 一种类型，用 gist 更贴近"原子笔记"
的性质，不要提前把 original 归档的活干了。
"""

from __future__ import annotations

import hashlib
import re
from datetime import date, datetime, timedelta

from temporalio import activity

from worker.db import document_id_exists, fetch_enriched_articles

PLACEHOLDER_TOPIC = "uncategorized"
MAX_SLUG_WORDS = 5
MAX_ID_COLLISION_RETRIES = 60


def _slugify(title: str, max_words: int = MAX_SLUG_WORDS) -> str:
    """默认 slug 方案：取标题里的字母数字词（连字符/标点天然分词点），转小写 kebab-case。"""
    words = re.findall(r"[a-zA-Z0-9]+", title)[:max_words]
    return "-".join(w.lower() for w in words) if words else "untitled"


def _generate_doc_id(slug: str, used_in_batch: set[str]) -> str:
    """12 位分钟时间戳 + slug；同 HHMM 冲突顺延到下一分钟（04 §2.6）。"""
    candidate_time = datetime.now()
    for _ in range(MAX_ID_COLLISION_RETRIES):
        candidate_id = f"{candidate_time.strftime('%Y%m%d%H%M')}-{slug}"
        if candidate_id not in used_in_batch and not document_id_exists(candidate_id):
            used_in_batch.add(candidate_id)
            return candidate_id
        candidate_time += timedelta(minutes=1)
    raise RuntimeError(f"zettel ID 冲突顺延超过 {MAX_ID_COLLISION_RETRIES} 次：slug={slug}")


def _fallback_notice(fetch_channel: str) -> str | None:
    """04 §2.6 fallback_notice 三态：null=正常，字符串=降级原因。"""
    if fetch_channel == "jina":
        return "主抓取通道失败（反爬拦截/超时等），使用 Jina Reader 兜底获取原文"
    return None


@activity.defn
def aggregate_activity(batch_id: str) -> list[dict]:
    articles = fetch_enriched_articles(batch_id)
    used_ids: set[str] = set()
    records: list[dict] = []

    for article in articles:
        title = article["translated_title"] or article["fetched_title"]
        slug = _slugify(article["fetched_title"])
        doc_id = _generate_doc_id(slug, used_ids)
        body_md = f"# {title}\n\n{article['gist']}\n\n原文：[{article['source_name']}]({article['url']})"

        records.append(
            {
                "doc_id": doc_id,
                "title": title,
                "doc_date": article["published_at"] or date.today(),
                "frontmatter": {
                    "title": title,
                    "doc_type": "zettel",
                    "source_name": article["source_name"],
                    "source_url": article["url"],
                    "gist": article["gist"],
                    "topic": PLACEHOLDER_TOPIC,
                    "fallback_notice": _fallback_notice(article["fetch_channel"]),
                },
                "body_md": body_md,
                "content_hash": hashlib.sha256(body_md.encode("utf-8")).hexdigest(),
            }
        )

    activity.logger.info(f"aggregate_activity: batch_id={batch_id} 共组装 {len(records)} 条 zettel 记录")
    return records

"""一次性脚本：重抓真实产出（非 M8 迁移）的 arxiv-api 来源 Original，只拿到了摘要页
内容的问题。

背景：`enrich.py::_fetch_direct` 此前只请求 `arxiv.org/abs/<id>`（论文摘要页），这个
页面本身只有几百字摘要 + "查看PDF/全文链接/许可"侧边栏文字，从来不含论文全文——
真实生产数据验证过，全部 83 篇非迁移 arxiv Original 无一例外都只有这部分内容，字数
在 255-979 之间。已经在 `enrich.py` 里补上"优先尝试 arxiv 官方全文 HTML 渲染端点
（/html/<id>），不存在时静默回退到摘要页"的逻辑（不影响其他信息源）；本脚本负责把
这批已经入库的旧数据用新逻辑重新抓取+翻译，回填 documents 表。

处理范围：`doc_type='original' AND frontmatter->>'source_name'='arxiv-api'`，且
`frontmatter->>'migrated_from_legacy_vault'` 不是 'true'（迁移自旧 vault 的 Original
走的是另一套抓取机制，内容已经完整，不在本次范围）。

每篇文章的处理步骤：
  1. 用 arXiv API（id_list 精确查询）核对这篇论文在源头是否仍然存在——这是用户明确
     要求的交叉核对："核对文章标题跟 arxiv-api 源头抓到的文章列表"。我们自己没有存过
     未翻译的英文原标题，无法做字符串级别的强比对，这里的核对方式是：确认 arXiv API
     仍然能查到这个 id、记录它权威的英文标题，供 dry-run 报告里人工核对，且只有查到
     entry 才继续往下处理——查不到（真实世界里 arXiv 几乎不会发生，只是防御性处理）
     就跳过，不覆盖现有内容。
  2. 用新的 fetch_original_activity 重新抓取原文（现在会优先尝试全文 HTML 端点）。
  3. 用 translate_activity 重新翻译正文（标题沿用已有的中文标题，不重新翻译一遍
     覆盖——论文标题不会因为拿到全文而改变，重新翻译只是多花一次可忽略的 LLM 调用，
     结果直接丢弃）；重新生成 gist（现在有全文做依据，摘要式 gist 可能更准确）；
     机械重算 word_count；沿用 `aggregate.py::_build_fallback_notice` 同一套映射
     重算 fallback_notice。
  4. topic_slug/tags/entities/content_type/related_zettel_id/doc_date/title 全部保持
     不变——这些字段的重新计算涉及跨文章判断（聚类/打标），不在本次"补齐原文内容"
     的范围内，改了反而引入不必要的风险。

默认 dry-run 打印每篇的"旧字数 → 新字数 / 抓取通道 / 是否命中全文端点"对照表，
`--execute` 才真正调用 LLM 重新翻译并写库（会真实消耗 token，dry-run 阶段不翻译，
只做抓取层面的探测，避免为了打印报告而浪费翻译调用）。

用法（在 backend/ 目录下）：
    ~/miniconda3/envs/ainews-service/bin/python3 -m scripts.refetch_arxiv_originals
    ~/miniconda3/envs/ainews-service/bin/python3 -m scripts.refetch_arxiv_originals --execute
"""

from __future__ import annotations

import argparse
import re

import httpx
from sqlalchemy import text

from worker import enrich
from worker.aggregate import _build_fallback_notice
from worker.db import get_engine, upsert_document
from worker.enrich import compute_word_count, content_hash, fetch_original_activity, gist_activity, translate_activity
from worker.fetch import _parse_arxiv_atom

ARXIV_API_QUERY_URL = "https://export.arxiv.org/api/query"
_ARXIV_ID_RE = re.compile(r"arxiv\.org/abs/([\w.]+)$", re.IGNORECASE)


def _target_documents() -> list[dict]:
    """只处理还没被本脚本重抓过的文档（`frontmatter.arxiv_fulltext_refetched` 不是
    true）——单篇论文全文翻译按分块数不同耗时差异很大，脚本中途被打断（或分批跑）
    很正常，这个标记让重新执行时天然跳过已完成的部分，不用重新翻译一遍浪费 token。
    """
    with get_engine().begin() as conn:
        rows = conn.execute(
            text(
                """
                SELECT * FROM documents
                WHERE doc_type = 'original'
                  AND frontmatter->>'source_name' = 'arxiv-api'
                  AND COALESCE(frontmatter->>'migrated_from_legacy_vault', 'false') != 'true'
                  AND COALESCE(frontmatter->>'arxiv_fulltext_refetched', 'false') != 'true'
                ORDER BY id
                """
            )
        ).mappings().all()
    return [dict(r) for r in rows]


def _lookup_arxiv_entry(source_url: str) -> dict | None:
    """按 id_list 精确查询 arXiv API，核对这篇文章在源头是否仍然存在，并拿到权威英文
    标题——用同一套 `_parse_arxiv_atom` 解析逻辑（fetch.py 抓取时用的那一份），不新写
    一套解析规则。"""
    m = _ARXIV_ID_RE.search(source_url)
    if not m:
        return None
    response = httpx.get(
        ARXIV_API_QUERY_URL, params={"id_list": m.group(1)}, headers={"User-Agent": enrich.USER_AGENT}, timeout=30.0
    )
    response.raise_for_status()
    entries = _parse_arxiv_atom(response.text, source_name="arxiv-api")
    return entries[0] if entries else None


def plan_refetch(execute: bool) -> list[dict]:
    changes = []
    docs = _target_documents()
    total = len(docs)
    for i, doc in enumerate(docs, start=1):
        doc_id = doc["id"]
        old_fm = doc["frontmatter"]
        source_url = old_fm.get("source_url")
        print(f"[{i}/{total}] {doc_id} 开始处理（{source_url}）", flush=True)

        source_entry = _lookup_arxiv_entry(source_url or "")
        if not source_entry:
            print(f"[{i}/{total}] {doc_id} 跳过：arXiv 源头查无此 id", flush=True)
            changes.append(
                {
                    "doc_id": doc_id, "title": doc["title"], "status": "skipped_not_found_in_source",
                    "source_url": source_url,
                }
            )
            continue

        fetched = fetch_original_activity(source_url)
        new_raw_body = fetched["body_md"]
        fetch_channel = fetched["fetch_channel"]
        old_word_count = old_fm.get("word_count")
        print(
            f"[{i}/{total}] {doc_id} 抓取完成：通道={fetch_channel}  原始字符数={len(new_raw_body)}",
            flush=True,
        )

        entry = {
            "doc_id": doc_id,
            "title": doc["title"],
            "source_title_en": source_entry.title,
            "fetch_channel": fetch_channel,
            "old_word_count": old_word_count,
            "raw_fetched_chars": len(new_raw_body),
        }

        if not execute:
            entry["status"] = "dry_run"
            changes.append(entry)
            continue

        translated = translate_activity(doc["title"], new_raw_body)
        new_body = translated["translated_body_md"]
        new_word_count = compute_word_count(new_body)
        new_gist = gist_activity(doc["title"], new_body)
        new_fallback_notice = _build_fallback_notice(fetch_channel, translated["translation_fallback_notice"])

        new_fm = dict(old_fm)
        new_fm["word_count"] = new_word_count
        new_fm["gist"] = new_gist
        new_fm["fallback_notice"] = new_fallback_notice
        new_fm["arxiv_fulltext_refetched"] = True

        upsert_document(
            doc_id=doc_id,
            doc_type="original",
            title=doc["title"],
            doc_date=doc["doc_date"],
            frontmatter=new_fm,
            body_md=new_body,
            content_hash=content_hash(new_body),
        )

        entry["status"] = "updated"
        entry["new_word_count"] = new_word_count
        print(f"[{i}/{total}] {doc_id} 已写入：字数 {old_word_count} → {new_word_count}", flush=True)
        changes.append(entry)
    return changes


def print_report(changes: list[dict], execute: bool) -> None:
    fulltext_hits = sum(1 for c in changes if c.get("fetch_channel") == "direct" and c.get("raw_fetched_chars", 0) > 0)
    skipped = [c for c in changes if c["status"] == "skipped_not_found_in_source"]
    print(f"=== 共 {len(changes)} 篇待处理，arXiv 源头核对未通过（跳过） {len(skipped)} 篇 ===")
    for c in changes:
        if c["status"] == "skipped_not_found_in_source":
            print(f"  [跳过-源头查无此文] {c['doc_id']}  {c['title']}  ({c['source_url']})")
            continue
        if execute:
            print(
                f"  {c['doc_id']}  通道={c['fetch_channel']}  "
                f"字数 {c['old_word_count']} → {c['new_word_count']}  英文原题核对={c['source_title_en'][:60]!r}"
            )
        else:
            print(
                f"  {c['doc_id']}  通道={c['fetch_channel']}  "
                f"旧字数={c['old_word_count']}  新抓取字符数（未翻译）={c['raw_fetched_chars']}  "
                f"英文原题核对={c['source_title_en'][:60]!r}"
            )
    print(f"通道命中 direct（含全文端点优先命中）的篇数：{fulltext_hits}/{len(changes)}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true", help="真正重新翻译并写入数据库（默认只做抓取层探测，不翻译不写库）")
    args = parser.parse_args()

    changes = plan_refetch(args.execute)
    print_report(changes, args.execute)
    if args.execute:
        updated = sum(1 for c in changes if c["status"] == "updated")
        print(f"已更新 {updated} 篇。")
    else:
        print("dry-run 模式，未翻译、未写入。加 --execute 真正执行。")


if __name__ == "__main__":
    main()

"""一次性脚本：修复 M8 迁移（migrate_legacy_vault.py）写入生产库的错误数据。

背景：M8 迁移脚本存在过 4 个 bug（现已在 migrate_legacy_vault.py 里修复，详见
.claude/memory/known_issues.md 与全量工程审查报告 2026-07-05）——
  1. Original 的 doc_date 判断只认 date 类型，加引号的 published_at 字符串会静默落到
     date.today()（写成脚本执行当天，而非文章真实发布日期）。
  2. Topic 的 article_count 把"待补日期区块数"当"文章篇数"累加，系统性偏低。
  3. Original frontmatter 缺 word_count/gist 两个 key；Daily 的 stats 用了旧键名
     entry_count 而不是权威 schema（aggregate.py）的 articles_processed，前端只认
     后者，导致页面显示"0 字"/"0 条"。
  4. strip_leading_title_block() 的 blockquote 剥离正则漏了 re.MULTILINE，标题下连续
     两行 `> 原文：...` / `> 抓取：...：N 字` 只有第一行被剥离，第二行残留在 Original/
     Daily/Digest 正文最前面。

这些错误已经写进生产 documents 表。本脚本只处理 frontmatter.migrated_from_legacy_vault
= true 的文档（M8 迁移写入的那批），逐条重算受影响字段并整行 upsert 回写；title/doc_type
均保持不变。默认 dry-run 打印"修改前 → 修改后"对照表，`--execute` 才真正写库。

用法（在 backend/ 目录下）：
    ~/miniconda3/envs/ainews-service/bin/python3 -m scripts.repair_m8_legacy_data
    ~/miniconda3/envs/ainews-service/bin/python3 -m scripts.repair_m8_legacy_data --execute
"""

from __future__ import annotations

import argparse

from sqlalchemy import text

from scripts.migrate_legacy_vault import (
    VAULT_ROOT,
    _content_hash,
    _count_topic_entries,
    _parse_legacy_date,
    extract_gist,
    original_doc_id,
    parse_frontmatter,
    strip_leading_title_block,
)
from worker.db import aggregate_get_document, get_engine, upsert_document
from worker.enrich import compute_word_count


def _migrated_docs(doc_type: str) -> list[dict]:
    """查询某 doc_type 下所有 M8 迁移写入的文档（frontmatter.migrated_from_legacy_vault = true）。"""
    with get_engine().begin() as conn:
        rows = conn.execute(
            text(
                "SELECT * FROM documents WHERE doc_type = :doc_type "
                "AND frontmatter->>'migrated_from_legacy_vault' = 'true'"
            ),
            {"doc_type": doc_type},
        ).mappings().all()
    return [dict(r) for r in rows]


def _latest_original_file_per_url(paths: list) -> dict:
    """旧 vault 有 22 个 source_url 对应多个文件（旧系统对同一篇文章隔几天重新抓取/翻译
    一次，"复盘"式重复处理），新系统 Original 按 source_url 唯一归档。原始 M8 迁移对这类
    冲突没有显式判重逻辑——planning 阶段不会跳过重复 doc_id，apply_plan 按文件名排序依次
    upsert，最终隐式以"文件名最靠后（=最近一次重新处理）"的版本落地。这里用同一条规则挑出
    每个 doc_id 的"获胜文件"，之后只对这一份重算，避免同一 doc_id 被两份不同来源内容反复
    判定为"需要修正"而无法收敛（见 .claude/memory/known_issues.md）。
    """
    winner_by_url: dict[str, object] = {}
    for path in sorted(paths):
        fm, _ = parse_frontmatter(path)
        url = fm.get("source_url")
        if not url:
            continue
        winner_by_url[url] = path  # 排序后的后来者覆盖前者，天然得到"文件名最靠后"的那份
    return winner_by_url


def repair_originals(execute: bool) -> list[dict]:
    """doc_date（发布日期误判）+ word_count/gist（缺字段导致前端显示 0）+ body_md
    （strip_leading_title_block 漏 re.MULTILINE 导致的元数据残留行）。"""
    changes = []
    all_paths = list((VAULT_ROOT / "60-Originals").glob("*.md"))
    for url, path in sorted(_latest_original_file_per_url(all_paths).items()):
        fm, raw_body = parse_frontmatter(path)
        doc_id = original_doc_id(url)
        existing = aggregate_get_document(doc_id)
        if not existing or not existing["frontmatter"].get("migrated_from_legacy_vault"):
            continue

        correct_body = strip_leading_title_block(raw_body)
        correct_date = _parse_legacy_date(fm.get("published_at"))
        correct_word_count = compute_word_count(correct_body)
        correct_gist = extract_gist(correct_body)
        correct_hash = _content_hash(correct_body)

        old_fm = existing["frontmatter"]
        diff = {}
        if existing["body_md"] != correct_body:
            diff["body_md"] = ("...（正文残留元数据行）", "...（已剥离）")
        if existing["doc_date"] != correct_date:
            diff["doc_date"] = (existing["doc_date"], correct_date)
        if old_fm.get("word_count") != correct_word_count:
            diff["word_count"] = (old_fm.get("word_count"), correct_word_count)
        if old_fm.get("gist") != correct_gist:
            diff["gist"] = (old_fm.get("gist"), correct_gist)
        if not diff:
            continue

        changes.append({"doc_id": doc_id, "doc_type": "original", "diff": diff})
        if execute:
            new_fm = dict(old_fm)
            new_fm["word_count"] = correct_word_count
            new_fm["gist"] = correct_gist
            upsert_document(
                doc_id=doc_id,
                doc_type="original",
                title=existing["title"],
                doc_date=correct_date,
                frontmatter=new_fm,
                body_md=correct_body,
                content_hash=correct_hash,
            )
    return changes


def repair_topics(execute: bool) -> list[dict]:
    """article_count：按当前 body_md 里实际的条目行数重算，不依赖任何历史基线。"""
    changes = []
    for doc in _migrated_docs("topic"):
        correct_count = _count_topic_entries(doc["body_md"])
        old_count = doc["frontmatter"].get("article_count")
        if old_count == correct_count:
            continue

        changes.append({
            "doc_id": doc["id"],
            "doc_type": "topic",
            "diff": {"article_count": (old_count, correct_count)},
        })
        if execute:
            new_fm = dict(doc["frontmatter"])
            new_fm["article_count"] = correct_count
            upsert_document(
                doc_id=doc["id"],
                doc_type="topic",
                title=doc["title"],
                doc_date=doc["doc_date"],
                frontmatter=new_fm,
                body_md=doc["body_md"],
                content_hash=doc["content_hash"],
            )
    return changes


def repair_dailies(execute: bool) -> list[dict]:
    """stats 键名对齐权威 schema：entry_count → articles_processed，补 topics_touched；
    body_md 同 Original，重新剥离标题下残留的元数据行。"""
    changes = []
    for doc in _migrated_docs("daily"):
        path = VAULT_ROOT / "10-Daily" / f"{doc['id']}.md"
        _, raw_body = parse_frontmatter(path)
        correct_body = strip_leading_title_block(raw_body)

        old_stats = doc["frontmatter"].get("stats") or {}
        needs_stats_fix = "articles_processed" not in old_stats
        needs_body_fix = doc["body_md"] != correct_body
        if not needs_stats_fix and not needs_body_fix:
            continue

        diff = {}
        new_fm = dict(doc["frontmatter"])
        if needs_stats_fix:
            new_stats = {
                "articles_processed": old_stats.get("entry_count"),
                "topics_touched": len(doc["frontmatter"].get("topics") or []),
                "sources_alive": old_stats.get("sources_alive"),
                "sources_dead": old_stats.get("sources_dead"),
            }
            diff["stats"] = (old_stats, new_stats)
            new_fm["stats"] = new_stats
        if needs_body_fix:
            diff["body_md"] = ("...（正文残留元数据行）", "...（已剥离）")

        changes.append({"doc_id": doc["id"], "doc_type": "daily", "diff": diff})
        if execute:
            upsert_document(
                doc_id=doc["id"],
                doc_type="daily",
                title=doc["title"],
                doc_date=doc["doc_date"],
                frontmatter=new_fm,
                body_md=correct_body if needs_body_fix else doc["body_md"],
                content_hash=_content_hash(correct_body) if needs_body_fix else doc["content_hash"],
            )
    return changes


def repair_digests(execute: bool) -> list[dict]:
    """body_md：同 Original/Daily，重新剥离标题下残留的元数据行（Digest 没有 word_count/
    gist/stats 这类字段问题，只有这一项）。"""
    changes = []
    for doc in _migrated_docs("digest"):
        date_str = doc["id"].removeprefix("digest-")
        path = VAULT_ROOT / "30-Digests" / f"{date_str}-digest.md"
        if not path.exists():
            continue
        _, raw_body = parse_frontmatter(path)
        correct_body = strip_leading_title_block(raw_body)
        if doc["body_md"] == correct_body:
            continue

        changes.append({
            "doc_id": doc["id"],
            "doc_type": "digest",
            "diff": {"body_md": ("...（正文残留元数据行）", "...（已剥离）")},
        })
        if execute:
            upsert_document(
                doc_id=doc["id"],
                doc_type="digest",
                title=doc["title"],
                doc_date=doc["doc_date"],
                frontmatter=doc["frontmatter"],
                body_md=correct_body,
                content_hash=_content_hash(correct_body),
            )
    return changes


def print_report(all_changes: dict[str, list[dict]]) -> None:
    for doc_type, changes in all_changes.items():
        print(f"=== {doc_type}：{len(changes)} 条需要修正 ===")
        for c in changes:
            print(f"  {c['doc_id']}")
            for field, (old, new) in c["diff"].items():
                print(f"    {field}: {old!r} → {new!r}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true", help="真正写入数据库（默认只 dry-run 打印对照表）")
    args = parser.parse_args()

    all_changes = {
        "original": repair_originals(args.execute),
        "topic": repair_topics(args.execute),
        "daily": repair_dailies(args.execute),
        "digest": repair_digests(args.execute),
    }
    print_report(all_changes)
    total = sum(len(v) for v in all_changes.values())
    if args.execute:
        print(f"已修正 {total} 条文档。")
    else:
        print(f"dry-run 模式，计划修正 {total} 条文档，未写入。加 --execute 真正执行。")


if __name__ == "__main__":
    main()

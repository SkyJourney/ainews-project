"""一次性脚本：I2 修复后（enrich.py 的 compute_word_count 改为剥离链接/图片引用再
计数），全表存量 Original 的 word_count 口径需要统一重算——M8 迁移那批已经在
repair_m8_legacy_data.py 里用修复后的函数处理过，这里补的是非迁移（M1-M7 正常产出）
的存量 Original，避免表内两种口径混着展示（差异通常是"字数偏高"，不是"显示 0"，
优先级低于 M8 回填，见 .claude/memory/known_issues.md）。

默认 dry-run 打印"文档 id：旧值 → 新值"对照表，`--execute` 才真正写库。

用法（在 backend/ 目录下）：
    ~/miniconda3/envs/ainews-service/bin/python3 -m scripts.recompute_original_word_counts
    ~/miniconda3/envs/ainews-service/bin/python3 -m scripts.recompute_original_word_counts --execute
"""

from __future__ import annotations

import argparse

from sqlalchemy import text

from worker.db import get_engine, upsert_document
from worker.enrich import compute_word_count


def find_outdated_word_counts() -> list[dict]:
    with get_engine().begin() as conn:
        rows = conn.execute(text("SELECT * FROM documents WHERE doc_type = 'original'")).mappings().all()

    changes = []
    for row in rows:
        doc = dict(row)
        correct = compute_word_count(doc["body_md"])
        old = doc["frontmatter"].get("word_count")
        if old != correct:
            changes.append({"doc": doc, "old": old, "new": correct})
    return changes


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true", help="真正写入数据库（默认只 dry-run 打印对照表）")
    args = parser.parse_args()

    changes = find_outdated_word_counts()
    print(f"=== 需要重算 word_count 的 Original：{len(changes)} 条 ===")
    for c in changes:
        print(f"  {c['doc']['id']}: {c['old']!r} → {c['new']!r}")

    if args.execute:
        for c in changes:
            doc = c["doc"]
            new_fm = dict(doc["frontmatter"])
            new_fm["word_count"] = c["new"]
            upsert_document(
                doc_id=doc["id"],
                doc_type="original",
                title=doc["title"],
                doc_date=doc["doc_date"],
                frontmatter=new_fm,
                body_md=doc["body_md"],
                content_hash=doc["content_hash"],
            )
        print(f"已重算 {len(changes)} 条。")
    else:
        print("dry-run 模式，未写入。加 --execute 真正执行。")


if __name__ == "__main__":
    main()

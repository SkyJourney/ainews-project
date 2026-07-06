"""一次性脚本：把旧 AInews vault（/Volumes/Projects/AInews）的历史内容导入 Postgres。

设计详见 docs/milestones/M8-legacy-migration.md，决策记录见 .claude/memory/decisions.md
（"M8：迁移设计定稿"条目）。核心策略：
  - Original/Zettel/Topic/Digest 遇到冲突（同 ID 或按 source_url 反查已存在）以新系统数据
    为准，只导入新系统没有的内容
  - Daily 例外：按日期判重，新系统已有当天记录才跳过，其余日期原样导入，保留旧系统按天
    拆分的粒度（用户明确要求补齐这部分）
  - Original 的新 ID 必须按 source_url 重新计算（`original-{sha256(url)[:12]}`），旧文件名
    是 `YYYY-MM-DD-HHMM-slug` 格式，与新系统的 hash 体系不同——这是唯一结构性不同的一类，
    旧 vault 里有大量 wikilink 直接引用旧 Original 文件名，必须整体重写成新 hash id 才能
    在新系统里正确解析

默认 dry-run（只打印计划，不碰数据库）；`--execute` 才会真正调用
upsert_document/sync_document_tags/sync_document_links 写入。

用法（在 backend/ 目录下）：
    ~/miniconda3/envs/ainews-service/bin/python3 -m scripts.migrate_legacy_vault
    ~/miniconda3/envs/ainews-service/bin/python3 -m scripts.migrate_legacy_vault --execute
"""

from __future__ import annotations

import argparse
import hashlib
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

import yaml

from worker.aggregate import _content_hash, _humanize_slug
from worker.enrich import compute_word_count
from worker.db import (
    aggregate_get_document,
    document_id_exists,
    filter_lookup_url_index,
    get_engine,
    sync_document_links,
    sync_document_tags,
    upsert_document,
)
from worker.filter import normalize_url_for_index
from sqlalchemy import text

VAULT_ROOT = Path("/Volumes/Projects/AInews")

ZETTEL_ID_RE = re.compile(r"^\d{12}-[a-z0-9-]+$")
OLD_ORIGINAL_ID_RE = re.compile(r"^\d{4}-\d{2}-\d{2}-\d{4}-.+$")
DAILY_ID_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
WIKILINK_RE = re.compile(r"\[\[([^\]|#]+)(\|[^\]]*)?\]\]")


# ---------------------------------------------------------------------------
# 通用解析工具
# ---------------------------------------------------------------------------

_KV_LINE_RE = re.compile(r"^(?P<indent>\s*)(?P<key>[A-Za-z_][\w]*):\s*(?P<value>.+)$")


def _quote_value_if_needed(value: str) -> str:
    """旧 vault 有真实案例（曾导致 Astro 构建失败）：标题/摘要类字段值里含未加引号的
    冒号（如 `title: Breaking Failure Cascades: Step-by-step`），会被 YAML 解析成
    非法的嵌套 mapping。补救：值本身看起来不是已加引号/flow 集合/纯字面量时，且含
    `: ` 或以 `:` 结尾，视为需要加引号的裸字符串，单引号包裹（内部单引号按 YAML
    规则转义成两个单引号）。"""
    v = value.strip()
    if not v or v[0] in "'\"[{" or v in ("null", "~", "true", "false"):
        return value
    try:
        float(v)
        return value
    except ValueError:
        pass
    if ": " in v or v.endswith(":"):
        return f"'{v.replace(chr(39), chr(39) * 2)}'"
    return value


def _repair_yaml_text(fm_text: str) -> str:
    repaired = []
    for line in fm_text.split("\n"):
        m = _KV_LINE_RE.match(line)
        if m:
            value = _quote_value_if_needed(m.group("value"))
            repaired.append(f"{m.group('indent')}{m.group('key')}: {value}")
        else:
            repaired.append(line)
    return "\n".join(repaired)


def parse_frontmatter(path: Path) -> tuple[dict, str]:
    """拆分 YAML frontmatter 与正文。旧 vault 引号风格不统一（曾因未加引号的冒号导致
    Astro 构建失败过一次），标准 yaml 库解析失败时用 `_repair_yaml_text` 做一次
    针对性补救再重试，不假设任何引号约定。"""
    raw = path.read_text(encoding="utf-8")
    if not raw.startswith("---\n"):
        return {}, raw
    end = raw.find("\n---\n", 4)
    if end == -1:
        return {}, raw
    fm_text = raw[4:end]
    body = raw[end + 5:]
    try:
        fm = yaml.safe_load(fm_text) or {}
    except yaml.YAMLError:
        fm = yaml.safe_load(_repair_yaml_text(fm_text)) or {}
    return fm, body


def original_doc_id(url: str) -> str:
    """与 worker.aggregate._original_doc_id 保持完全一致的计算方式。"""
    return f"original-{hashlib.sha256(url.encode('utf-8')).hexdigest()[:12]}"


def _parse_legacy_date(value: object) -> date:
    """统一处理旧 vault YAML 解析出的日期值：裸值解析成 date/datetime，加了引号的
    值解析成 str（isinstance(x, date) 会漏判），三种情况都要正确落到 date；解析不出
    才兜底成 date.today()。此前 plan_originals/plan_zettels 各自用了一套不完整的判断，
    加引号的字符串会静默漏判成今天（见 .claude/memory/known_issues.md）。"""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value.strip())
        except ValueError:
            pass
    return date.today()


def strip_leading_title_block(body: str) -> str:
    """去掉旧 vault Daily/Digest/Original 正文开头的 `# {title}`（+ Original/Digest 紧跟的
    来源说明 blockquote，往往是连续两行 `> 原文：...` / `> 抓取：...：N 字`）——新系统 title
    是独立字段，不在 body_md 里重复拼一遍（M6 踩过这个坑）。

    `bq` 的匹配必须带 re.MULTILINE：不带的话 `^` 只认字符串开头一次，`(?:^> ...)+` 循环在
    吃掉第一行 blockquote 后就无法再匹配第二行，导致"抓取时间/字数"这行元数据残留在正文
    最前面（M8 迁移的真实数据里发现过，见 .claude/memory/known_issues.md）。
    """
    body = body.lstrip("\n")
    m = re.match(r"^# .+?\n", body)
    if not m:
        return body
    rest = body[m.end():].lstrip("\n")
    bq = re.match(r"(?:^> .*\n?)+", rest, re.MULTILINE)
    if bq:
        rest = rest[bq.end():]
    return rest.lstrip("\n")


def extract_gist(body: str, max_chars: int = 200) -> str:
    """旧 Zettel 没有独立的机械 gist 字段（新系统 M5 起前端列表页预览依赖
    frontmatter.gist），从"## 概念 / 事件"小节截取一段作为近似替代。"""
    m = re.search(r"## 概念\s*/\s*事件\s*\n+(.+?)(?:\n##|\Z)", body, re.S)
    text_ = (m.group(1).strip() if m else body.strip()).replace("\n", " ")
    if len(text_) <= max_chars:
        return text_
    return text_[:max_chars].rstrip() + "…"


# ---------------------------------------------------------------------------
# 计划态数据结构：dry-run 与 execute 共用同一份"计划"，只是最后一步是否真正写库不同
# ---------------------------------------------------------------------------

@dataclass
class PlannedDoc:
    doc_id: str
    doc_type: str
    title: str
    doc_date: date
    frontmatter: dict
    body_md: str
    tags: list[str]
    raw_link_targets: list[str]  # 未过滤的 wikilink 提取结果，最终解析在全部文档规划完成后统一做


@dataclass
class MigrationPlan:
    docs: list[PlannedDoc] = field(default_factory=list)
    skipped: dict[str, int] = field(default_factory=lambda: {"original": 0, "zettel": 0, "daily": 0, "digest": 0})
    old_to_new_original_id: dict[str, str] = field(default_factory=dict)
    dangling_links_dropped: int = 0
    parse_errors: list[tuple[str, str]] = field(default_factory=list)  # (文件路径, 错误信息)


# ---------------------------------------------------------------------------
# Original：ID 必须按 source_url 重新计算；这也是全库唯一一类需要重写 wikilink 目标文本的类型
# ---------------------------------------------------------------------------

def plan_originals(plan: MigrationPlan) -> None:
    for path in sorted((VAULT_ROOT / "60-Originals").glob("*.md")):
        try:
            fm, body = parse_frontmatter(path)
            url = fm.get("source_url")
            if not url:
                continue  # 缺 source_url 的文件无法计算新 id，跳过（未见过这种样本，防御性处理）
            new_id = original_doc_id(url)
            plan.old_to_new_original_id[path.stem] = new_id
            if document_id_exists(new_id):
                plan.skipped["original"] += 1
                continue

            title = fm.get("title") or path.stem
            body = strip_leading_title_block(body)
            frontmatter = {
                "title": title,
                "doc_type": "original",
                "source_name": fm.get("source_name"),
                "source_url": url,
                # 权威 schema（aggregate.py._build_original_record）没有这个 key——是
                # 有意保留的额外信息（翻译前的原始英文标题），不是要跟权威 schema 严格
                # 对齐；前端/其余代码不读它，纯归档用途，不会因为多这个字段出问题。
                "original_title": fm.get("original_title"),
                "topic_slug": None,  # 旧 vault 的 related_topics 恒为空数组，没有可迁移的真实值
                "gist": extract_gist(body),  # 旧 vault 没有对应字段，机械截断兜底（04 §2.4 硬约束）
                "word_count": compute_word_count(body),
                "fallback_notice": fm.get("fallback_notice"),
                "related_zettel_id": None,  # 由 plan_zettels 回填（如果对应 zettel 也被导入）
                "migrated_from_legacy_vault": True,
            }
            doc_date_val = _parse_legacy_date(fm.get("published_at"))
            plan.docs.append(
                PlannedDoc(
                    doc_id=new_id,
                    doc_type="original",
                    title=title,
                    doc_date=doc_date_val,
                    frontmatter=frontmatter,
                    body_md=body,
                    tags=fm.get("tags") or [],
                    raw_link_targets=[],  # Original 正文极少含 wikilink，且不是本次迁移要保留的关联来源
                )
            )
        except Exception as e:  # noqa: BLE001 - 一次性迁移脚本，单个文件解析失败不阻塞整体
            plan.parse_errors.append((str(path), str(e)))


# ---------------------------------------------------------------------------
# Zettel：ID 格式与新系统一致，按"对应 Original 是否已被新系统 zettel 化"判重
# ---------------------------------------------------------------------------

def _already_zettelized(url: str) -> bool:
    row = filter_lookup_url_index(normalize_url_for_index(url))
    return bool(row and row.get("zettel_id"))


def plan_zettels(plan: MigrationPlan) -> None:
    original_id_by_zettel: dict[str, str] = {}  # zettel_id -> 对应 original 的新 id（用于回填）
    for path in sorted((VAULT_ROOT / "50-Zettel").glob("*.md")):
        try:
            fm, body = parse_frontmatter(path)
            zettel_id = path.stem
            if not ZETTEL_ID_RE.match(zettel_id):
                continue  # 防御性处理，未见过不匹配的样本
            url = fm.get("source_url")
            if document_id_exists(zettel_id) or (url and _already_zettelized(url)):
                plan.skipped["zettel"] += 1
                continue

            title = fm.get("title") or zettel_id
            new_original_id = original_doc_id(url) if url else None
            if new_original_id:
                original_id_by_zettel[zettel_id] = new_original_id
            frontmatter = {
                "title": title,
                "doc_type": "zettel",
                "topic_slug": fm.get("topic"),
                "gist": extract_gist(body),
                "original_id": new_original_id,
                "rationale": "旧系统历史内容迁移（M8）",
                "migrated_from_legacy_vault": True,
            }
            doc_date_val = _parse_legacy_date(fm.get("created"))
            plan.docs.append(
                PlannedDoc(
                    doc_id=zettel_id,
                    doc_type="zettel",
                    title=title,
                    doc_date=doc_date_val,
                    frontmatter=frontmatter,
                    body_md=body,
                    tags=fm.get("tags") or [],
                    raw_link_targets=[m.group(1) for m in WIKILINK_RE.finditer(body)],
                )
            )
        except Exception as e:  # noqa: BLE001
            plan.parse_errors.append((str(path), str(e)))

    # 回填对应 Original 的 related_zettel_id（仅当这篇 Original 也在本次迁移计划里）
    for doc in plan.docs:
        if doc.doc_type != "original":
            continue
        for zid, oid in original_id_by_zettel.items():
            if oid == doc.doc_id:
                doc.frontmatter["related_zettel_id"] = zid


# ---------------------------------------------------------------------------
# Topic：按日期区块合并，不整体覆盖；13 个 slug 里预期 9 个合并、4 个全新创建
# ---------------------------------------------------------------------------

def split_topic_sections(body: str) -> dict[str, str]:
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for line in body.split("\n"):
        m = re.match(r"^## (\d{4}-\d{2}-\d{2})\s*$", line)
        if m:
            current = m.group(1)
            sections[current] = []
        elif current:
            sections[current].append(line)
    return {d: "\n".join(lines).strip("\n") for d, lines in sections.items()}


_TOPIC_ENTRY_LINE_RE = re.compile(r"^- ", re.M)


def _count_topic_entries(body_md: str) -> int:
    """article_count 必须是"实际条目数"，不是"日期区块数"——直接数正文里的条目行，
    不依赖任何历史累加基线，可重复调用（此前 += len(missing) 把待补的日期区块数当成
    篇数累加，见 .claude/memory/known_issues.md）。"""
    return len(_TOPIC_ENTRY_LINE_RE.findall(body_md))


def plan_topics(plan: MigrationPlan) -> None:
    for path in sorted((VAULT_ROOT / "20-Topics").glob("*.md")):
        slug = path.stem
        try:
            fm, body = parse_frontmatter(path)
            sections = split_topic_sections(body)
            existing = aggregate_get_document(slug)

            if existing is None:
                blocks = [f"## {d}\n\n{sections[d]}\n" for d in sorted(sections, reverse=True)]
                body_md = "\n".join(blocks)
                title = _humanize_slug(slug)
                frontmatter = {
                    "title": title,
                    "doc_type": "topic",
                    "topic_slug": slug,
                    "created_date": min(sections) if sections else date.today().isoformat(),
                    "last_updated_date": max(sections) if sections else date.today().isoformat(),
                    "article_count": _count_topic_entries(body_md),
                    "migrated_from_legacy_vault": True,
                }
                doc_date_val = date.fromisoformat(max(sections)) if sections else date.today()
            else:
                existing_dates = set(re.findall(r"^## (\d{4}-\d{2}-\d{2})", existing["body_md"], re.M))
                missing = sorted((d for d in sections if d not in existing_dates), reverse=True)
                if not missing:
                    continue  # 旧 vault 这个 slug 没有新系统缺少的日期，不需要动
                backfill_blocks = [f"## {d}\n\n{sections[d]}\n" for d in missing]
                body_md = existing["body_md"].rstrip("\n") + "\n\n" + "\n".join(backfill_blocks)
                title = existing["frontmatter"].get("title", _humanize_slug(slug))
                frontmatter = dict(existing["frontmatter"])
                frontmatter["article_count"] = _count_topic_entries(body_md)
                frontmatter["migrated_from_legacy_vault"] = True
                doc_date_val = existing["doc_date"]

            plan.docs.append(
                PlannedDoc(
                    doc_id=slug,
                    doc_type="topic",
                    title=title,
                    doc_date=doc_date_val,
                    frontmatter=frontmatter,
                    body_md=body_md,
                    tags=[],
                    raw_link_targets=[m.group(1) for m in WIKILINK_RE.finditer(body)],
                )
            )
        except Exception as e:  # noqa: BLE001
            plan.parse_errors.append((str(path), str(e)))


# ---------------------------------------------------------------------------
# Daily：按日期判重，新系统为准；其余日期原样导入（用户明确要求保留这部分粒度）
# ---------------------------------------------------------------------------

def plan_daily(plan: MigrationPlan) -> None:
    for path in sorted((VAULT_ROOT / "10-Daily").glob("*.md")):
        doc_id = path.stem
        try:
            if not DAILY_ID_RE.match(doc_id) or document_id_exists(doc_id):
                plan.skipped["daily"] += 1
                continue
            fm, body = parse_frontmatter(path)
            body = strip_leading_title_block(body)
            title = f"{doc_id} AI 日报"
            topics = fm.get("topics") or []
            previous_daily = fm.get("previous_daily")
            frontmatter = {
                "title": title,
                "doc_type": "daily",
                "stats": {
                    # 键名对齐权威 schema（aggregate.py _compute_daily_stats），前端只认
                    # articles_processed，旧键名 entry_count 会让页面显示"0 条"。
                    # new_topics_created/zettel_created/zettel_reused 旧系统没有等价数据，
                    # 不虚构（04"简化优于捏造"原则）；sources_alive/sources_dead 是真实
                    # legacy 数据，作为额外信息保留。
                    "articles_processed": fm.get("entry_count"),
                    "topics_touched": len(topics),
                    "sources_alive": fm.get("sources_alive"),
                    "sources_dead": fm.get("sources_dead"),
                },
                "topics": topics,
                "previous_daily": previous_daily.isoformat() if isinstance(previous_daily, date) else previous_daily,
                "migrated_from_legacy_vault": True,
            }
            plan.docs.append(
                PlannedDoc(
                    doc_id=doc_id,
                    doc_type="daily",
                    title=title,
                    doc_date=date.fromisoformat(doc_id),
                    frontmatter=frontmatter,
                    body_md=body,
                    tags=fm.get("tags") or [],
                    raw_link_targets=[m.group(1) for m in WIKILINK_RE.finditer(body)],
                )
            )
        except Exception as e:  # noqa: BLE001
            plan.parse_errors.append((str(path), str(e)))


# ---------------------------------------------------------------------------
# Digest：按日期判重，新系统为准；Digest 本身设计上不含 wikilink
# ---------------------------------------------------------------------------

def plan_digests(plan: MigrationPlan) -> None:
    for path in sorted((VAULT_ROOT / "30-Digests").glob("*.md")):
        old_date = path.stem.removesuffix("-digest")
        doc_id = f"digest-{old_date}"
        try:
            if not DAILY_ID_RE.match(old_date) or document_id_exists(doc_id):
                plan.skipped["digest"] += 1
                continue
            fm, body = parse_frontmatter(path)
            body = strip_leading_title_block(body)
            title = f"{old_date} AI Digest"
            frontmatter = {
                "title": title,
                "doc_type": "digest",
                "entry_count": fm.get("item_count", 0),
                "migrated_from_legacy_vault": True,
            }
            plan.docs.append(
                PlannedDoc(
                    doc_id=doc_id,
                    doc_type="digest",
                    title=title,
                    doc_date=date.fromisoformat(old_date),
                    frontmatter=frontmatter,
                    body_md=body,
                    tags=[],
                    raw_link_targets=[],
                )
            )
        except Exception as e:  # noqa: BLE001
            plan.parse_errors.append((str(path), str(e)))


# ---------------------------------------------------------------------------
# 收尾：wikilink 解析——按"这次迁移完成后哪些 id 真实存在"过滤+重写
# ---------------------------------------------------------------------------

def build_old_to_new_original_id_mapping() -> dict[str, str]:
    """扫一遍 60-Originals/ 计算"旧文件名 → 新 hash id"映射，纯读取 vault 文件、不查
    数据库、不做任何写入。`plan_originals()` 规划迁移时会顺带算出同一份映射（因为它
    要挨个判断每个文件对应的新 id 是否已存在），但那个函数还耦合了 PlannedDoc 组装、
    `document_id_exists` 查询等一堆和"仅仅需要这份映射"无关的工作。

    这份独立版本是给 `repair_m8_legacy_data.py` 用的：那边只需要在重算 Daily/Digest
    正文时重新做 wikilink 改写，不需要（也不应该）重新跑一遍完整的迁移规划。
    """
    mapping: dict[str, str] = {}
    for path in sorted((VAULT_ROOT / "60-Originals").glob("*.md")):
        try:
            fm, _ = parse_frontmatter(path)
        except Exception:  # noqa: BLE001 - 解析失败的文件对映射没有贡献，跳过即可
            continue
        url = fm.get("source_url")
        if url:
            mapping[path.stem] = original_doc_id(url)
    return mapping


def rewrite_original_wikilinks(body_md: str, old_to_new_original_id: dict[str, str]) -> str:
    """把正文里引用旧 Original 文件名的 wikilink 文本改写成新 hash id——只有 Original
    类型需要这一步重写（Zettel/Topic id 格式两边一致，不用改）。

    这是从 `resolve_links()` 抽出来的独立函数：`repair_m8_legacy_data.py` 重算 Daily/
    Digest 正文时如果只做标题/元数据块裁剪、不重新走这一步，会让已经改写正确的
    wikilink 被没改写过的旧文本覆盖回去——这是真实发生过的 bug（3 篇 Daily、133 处
    链接断链，见 .claude/memory/known_issues.md），根源就是两处"重算正文"的逻辑没有
    共用同一个改写步骤。
    """
    if not old_to_new_original_id:
        return body_md

    def _sub(m: re.Match) -> str:
        new_id = old_to_new_original_id.get(m.group(1))
        return f"[[{new_id}]]" if new_id else m.group(0)

    return WIKILINK_RE.sub(_sub, body_md)


def resolve_links(plan: MigrationPlan) -> None:
    """两件事：① Original 类型的旧 id 引用必须重写成新 hash id（文本层面真的要替换，
    否则前端 wikilink 解析找不到对应文档）；② 其余类型 id 格式不变，只需要判断目标在
    迁移完成后是否真实存在，不存在就不计入 link_targets（悬空/伪 wikilink 直接丢弃，
    不生成会违反外键约束的 links 行）。
    """
    with get_engine().begin() as conn:
        existing_ids = {row[0] for row in conn.execute(text("SELECT id FROM documents"))}
    planned_ids = {d.doc_id for d in plan.docs}
    valid_ids = existing_ids | planned_ids

    for doc in plan.docs:
        # Original 引用重写（只有 Daily/Zettel/Topic 的正文可能引用旧 Original 文件名）
        doc.body_md = rewrite_original_wikilinks(doc.body_md, plan.old_to_new_original_id)

        resolved_targets = []
        for raw_target in doc.raw_link_targets:
            target = plan.old_to_new_original_id.get(raw_target, raw_target)
            if target in valid_ids and target != doc.doc_id:
                resolved_targets.append(target)
            else:
                plan.dangling_links_dropped += 1
        doc.link_targets = resolved_targets  # type: ignore[attr-defined]


def build_plan() -> MigrationPlan:
    plan = MigrationPlan()
    plan_originals(plan)
    plan_zettels(plan)
    plan_topics(plan)
    plan_daily(plan)
    plan_digests(plan)
    resolve_links(plan)
    return plan


def apply_plan(plan: MigrationPlan) -> None:
    for doc in plan.docs:
        upsert_document(
            doc_id=doc.doc_id,
            doc_type=doc.doc_type,
            title=doc.title,
            doc_date=doc.doc_date,
            frontmatter=doc.frontmatter,
            body_md=doc.body_md,
            content_hash=_content_hash(doc.body_md),
        )
    for doc in plan.docs:
        sync_document_tags(doc.doc_id, doc.tags)
        sync_document_links(doc.doc_id, getattr(doc, "link_targets", []))


def print_report(plan: MigrationPlan) -> None:
    by_type: dict[str, int] = {}
    for doc in plan.docs:
        by_type[doc.doc_type] = by_type.get(doc.doc_type, 0) + 1
    print("=== 迁移计划 ===")
    for doc_type in ("original", "zettel", "topic", "daily", "digest"):
        print(f"  {doc_type}: 计划导入 {by_type.get(doc_type, 0)} 条，跳过（新系统已有）{plan.skipped.get(doc_type, 0)} 条")
    print(f"  悬空/伪 wikilink 丢弃：{plan.dangling_links_dropped} 处")
    print(f"  Original 旧 id → 新 id 映射条数：{len(plan.old_to_new_original_id)}")
    if plan.parse_errors:
        print(f"  解析失败 {len(plan.parse_errors)} 个文件（未纳入迁移计划，需要人工核对）：")
        for path, err in plan.parse_errors:
            print(f"    - {path}: {err}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true", help="真正写入数据库（默认只 dry-run 打印计划）")
    args = parser.parse_args()

    plan = build_plan()
    print_report(plan)

    if args.execute:
        apply_plan(plan)
        print(f"已写入 {len(plan.docs)} 条文档。")
    else:
        print("dry-run 模式，未写入任何数据。加 --execute 真正执行。")


if __name__ == "__main__":
    main()

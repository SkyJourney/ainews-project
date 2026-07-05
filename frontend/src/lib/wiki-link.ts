// AInews · 自建 mini remark wiki-link 插件
// 把 markdown 里的 [[slug]] 或 [[slug|display]] 转成 mdast link 节点
// 参考格式：`<a class="wikilink" href="/{segment}/{id}/">{display}</a>`
//
// M6 起存在性判断改查 Postgres documents 表（替代旧版内存 vault 缓存），
// doc_type 已经是显式列，不再需要按 slug 命名规律猜测所属目录。

import { visit } from 'unist-util-visit'
import type { Root, Text, Link, PhrasingContent } from 'mdast'
import type { Plugin } from 'unified'
import { fetchWikilinkTargets, type WikilinkTarget } from './db'
import { docHref } from './doc-type'

const WIKI_LINK_RE = /\[\[([^\]]+)\]\]/g
const EXCERPT_MAX_LEN = 90

function parseWikilinkTarget(raw: string): { target: string; display: string } {
  const [rawTarget, rawDisplay] = raw.split('|').map((s) => s.trim())
  // section 链接 [[target#heading]]（v1 不支持，剥掉）
  const target = rawTarget.split('#')[0].trim()
  return { target, display: rawDisplay ?? target }
}

/** 第一遍遍历：收集本文档里出现的全部 wikilink 目标 id，供后面一次性批量查库。 */
function collectWikilinkTargets(tree: Root): string[] {
  const targets = new Set<string>()
  visit(tree, 'text', (node) => {
    const value = (node as Text).value
    if (!value.includes('[[')) return
    WIKI_LINK_RE.lastIndex = 0
    let m: RegExpExecArray | null
    while ((m = WIKI_LINK_RE.exec(value)) !== null) {
      const { target } = parseWikilinkTarget(m[1].trim())
      if (target) targets.add(target)
    }
  })
  return Array.from(targets)
}

function excerptFromGist(gist: string | null): string {
  if (!gist) return ''
  return gist.length > EXCERPT_MAX_LEN ? `${gist.slice(0, EXCERPT_MAX_LEN)}…` : gist
}

/**
 * 把一段 text 拆成 (Text | Link)[] —— 每个 [[wiki]] 变 Link 节点
 * 目标 id 命中 resolved（第一遍批量查库结果）→ 合法链接（带 hover 预览 data 属性）
 * 未命中 → 断链（class="wikilink broken"，href="#"，不可点，title 提示原因）
 */
function splitTextByWikilink(value: string, resolved: Map<string, WikilinkTarget>): PhrasingContent[] | null {
  if (!value.includes('[[')) return null
  const parts: PhrasingContent[] = []
  let lastIndex = 0
  let m: RegExpExecArray | null
  WIKI_LINK_RE.lastIndex = 0
  while ((m = WIKI_LINK_RE.exec(value)) !== null) {
    if (m.index > lastIndex) {
      parts.push({ type: 'text', value: value.slice(lastIndex, m.index) } as Text)
    }
    const { target, display } = parseWikilinkTarget(m[1].trim())
    const matched = resolved.get(target)

    if (matched) {
      parts.push({
        type: 'link',
        url: docHref(matched.doc_type, target),
        title: null,
        children: [{ type: 'text', value: display } as Text],
        data: {
          hProperties: {
            className: ['wikilink'],
            'data-wiki-target': target,
            'data-preview-title': matched.title ?? target,
            'data-preview-excerpt': excerptFromGist(matched.gist),
          },
        },
      } as Link)
    } else {
      parts.push({
        type: 'link',
        url: '#',
        title: `链接目标不存在：${target}`,
        children: [{ type: 'text', value: display } as Text],
        data: {
          hProperties: {
            className: ['wikilink', 'broken'],
            'data-wiki-target': target,
          },
        },
      } as Link)
    }
    lastIndex = m.index + m[0].length
  }
  if (parts.length === 0) return null
  if (lastIndex < value.length) {
    parts.push({ type: 'text', value: value.slice(lastIndex) } as Text)
  }
  return parts
}

/**
 * remark 插件 · 遍历所有 text 节点，把 [[wiki]] 拆成 link。
 * 异步插件：先收集本文档全部 wikilink 目标，一次批量查 Postgres（避免每个 [[wiki]]
 * 各发一次查询），再用查询结果做第二遍替换。
 */
export const remarkWikiLink: Plugin<[], Root> = () => {
  return async (tree) => {
    const targets = collectWikilinkTargets(tree)
    const rows = await fetchWikilinkTargets(targets)
    const resolved = new Map(rows.map((r) => [r.id, r]))

    visit(tree, 'text', (node, index, parent) => {
      if (!parent || index == null) return
      const parentType = (parent as { type: string }).type
      if (parentType === 'code' || parentType === 'inlineCode') return
      const replacement = splitTextByWikilink((node as Text).value, resolved)
      if (!replacement) return
      ;(parent as { children: PhrasingContent[] }).children.splice(index, 1, ...replacement)
      return index + replacement.length // 跳过我们刚插入的节点
    })
  }
}

// AInews · Postgres Live Loader（M6 核心）
// 请求时查 documents 表，替代旧版 vault-loader.ts 的构建时文件系统扫描。
// 官方没有现成的 Postgres Live Loader，按 astro/loaders 的 LiveLoader 接口自建。

import type { LiveLoader } from 'astro/loaders'
import {
  fetchBacklinks,
  fetchDocumentById,
  fetchDocumentsByType,
  fetchTags,
  type BacklinkRow,
  type DocumentRow,
} from './db'
import { renderMarkdownToHtml } from './markdown-render'

function toBacklinkData(rows: BacklinkRow[]) {
  return rows.map((r) => ({ fromId: r.from_id, docType: r.doc_type, title: r.title }))
}

async function toLiveEntry(row: DocumentRow) {
  const [html, backlinkRows, tags] = await Promise.all([
    renderMarkdownToHtml(row.body_md, row.content_hash),
    fetchBacklinks(row.id),
    fetchTags(row.id),
  ])
  return {
    id: row.id,
    data: {
      ...row.frontmatter,
      id: row.id,
      title: row.title,
      docDate: row.doc_date,
      updatedAt: row.updated_at,
      backlinks: toBacklinkData(backlinkRows),
      // tags 独立建表，不在 frontmatter 里；这里统一注入，页面按 entry.data.tags 取用
      tags,
    },
    rendered: { html },
    cacheHint: {
      tags: [`document-${row.id}`],
      lastModified: new Date(row.updated_at),
    },
  }
}

/** 工厂函数：给一个 doc_type 返回一个 Live Loader（loadCollection/loadEntry）。 */
export function postgresLiveLoader(docType: string): LiveLoader {
  return {
    name: `postgres-loader:${docType}`,
    async loadCollection() {
      const rows = await fetchDocumentsByType(docType)
      const entries = await Promise.all(rows.map(toLiveEntry))
      return { entries, cacheHint: { tags: [`doc_type-${docType}`] } }
    },
    async loadEntry({ filter }) {
      const row = await fetchDocumentById(filter.id)
      if (!row || row.doc_type !== docType) {
        return { error: new Error(`未找到 ${docType} 文档：${filter.id}`) }
      }
      return toLiveEntry(row)
    },
  }
}

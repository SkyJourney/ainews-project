// AInews · 全站搜索 API（M6 起替代 Pagefind，见 markdown-render.ts 同批改造的说明）
// GET /api/search?q=xxx → { results: [{ url, meta: { title }, excerpt }] }
export const prerender = false

import type { APIRoute } from 'astro'
import { searchDocuments } from '../../lib/db'
import { docHref, DOC_TYPE_LABEL, type DocType } from '../../lib/doc-type'

export const GET: APIRoute = async ({ url }) => {
  const q = url.searchParams.get('q')?.trim()
  if (!q) {
    return new Response(JSON.stringify({ results: [] }), { headers: { 'Content-Type': 'application/json' } })
  }

  try {
    const rows = await searchDocuments(q)
    const results = rows.map((r) => ({
      url: docHref(r.doc_type, r.id),
      docType: r.doc_type,
      docTypeLabel: DOC_TYPE_LABEL[r.doc_type as DocType] ?? r.doc_type,
      meta: { title: r.title ?? r.id },
      excerpt: r.excerpt,
    }))

    return new Response(JSON.stringify({ results }), { headers: { 'Content-Type': 'application/json' } })
  } catch (err) {
    // 数据库抖动/查询异常时优雅降级为空结果，而不是让整个请求变成未处理异常
    console.error('搜索查询失败：', err)
    return new Response(JSON.stringify({ results: [] }), { headers: { 'Content-Type': 'application/json' } })
  }
}

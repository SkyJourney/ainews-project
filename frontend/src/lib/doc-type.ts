// AInews · documents.doc_type ↔ 前端 URL segment / 展示标签映射
// 替代旧版 slug-utils.ts 里"从 slug 命名规律推断类型"的启发式（M6 起 doc_type 是
// Postgres 里的显式列，不需要再猜）

export type DocType = 'daily' | 'topic' | 'zettel' | 'original' | 'digest'

export const DOC_TYPE_SEGMENT: Record<DocType, string> = {
  daily: 'daily',
  topic: 'topics',
  zettel: 'zettel',
  original: 'originals',
  digest: 'digest',
}

export const DOC_TYPE_LABEL: Record<DocType, string> = {
  daily: '每日简报',
  topic: '主题',
  zettel: '原子卡片',
  original: '原文归档',
  digest: '摘要速览',
}

/** 生成文档详情页 URL；未知 doc_type 时退化为 originals（永远可靠的归档层） */
export function docHref(docType: string, id: string): string {
  const segment = DOC_TYPE_SEGMENT[docType as DocType] ?? DOC_TYPE_SEGMENT.original
  return `/${segment}/${id}/`
}

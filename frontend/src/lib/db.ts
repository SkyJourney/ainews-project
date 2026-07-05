// AInews · 只读连接 Postgres ainews_content 库（M6 Live Content Collections 数据源）
// 复用 backend 的同一个 Postgres 实例，独立 database，见 04-roadmap.md §2.7

import pg from 'pg'

let pool: pg.Pool | undefined

function getPool(): pg.Pool {
  if (!pool) {
    const connectionString = process.env.DATABASE_URL
    if (!connectionString) {
      throw new Error('DATABASE_URL 环境变量未设置（frontend 只读连接 Postgres ainews_content 库）')
    }
    pool = new pg.Pool({ connectionString })
  }
  return pool
}

export interface DocumentRow {
  id: string
  doc_type: string
  title: string | null
  doc_date: string | null
  frontmatter: Record<string, unknown>
  body_md: string
  content_hash: string
  updated_at: string
}

const DOCUMENT_COLUMNS = 'id, doc_type, title, doc_date, frontmatter, body_md, content_hash, updated_at'

export async function fetchDocumentsByType(docType: string): Promise<DocumentRow[]> {
  const { rows } = await getPool().query<DocumentRow>(
    `SELECT ${DOCUMENT_COLUMNS} FROM documents WHERE doc_type = $1 ORDER BY doc_date DESC NULLS LAST, updated_at DESC`,
    [docType],
  )
  return rows
}

export async function fetchDocumentById(id: string): Promise<DocumentRow | null> {
  const { rows } = await getPool().query<DocumentRow>(`SELECT ${DOCUMENT_COLUMNS} FROM documents WHERE id = $1`, [id])
  return rows[0] ?? null
}

export interface WikilinkTarget {
  id: string
  doc_type: string
  title: string | null
  gist: string | null
}

/** wiki-link.ts 批量解析用：一次查出本篇文档里出现的全部 [[target]] 是否存在 + 展示用字段。 */
export async function fetchWikilinkTargets(ids: string[]): Promise<WikilinkTarget[]> {
  if (ids.length === 0) return []
  const { rows } = await getPool().query<WikilinkTarget>(
    `SELECT id, doc_type, title, frontmatter->>'gist' AS gist FROM documents WHERE id = ANY($1)`,
    [ids],
  )
  return rows
}

export interface BacklinkRow {
  from_id: string
  doc_type: string
  title: string | null
}

/** LuminaBacklinks 用：谁引用了这篇文档（查 links 表，替代旧版内存反向 map）。 */
export async function fetchBacklinks(toId: string): Promise<BacklinkRow[]> {
  const { rows } = await getPool().query<BacklinkRow>(
    `SELECT l.from_id, d.doc_type, d.title FROM links l JOIN documents d ON d.id = l.from_id WHERE l.to_id = $1`,
    [toId],
  )
  return rows
}

/** tags 独立建表（不在 frontmatter 里），original/zettel 展示标签需要单独查这张表。 */
export async function fetchTags(docId: string): Promise<string[]> {
  const { rows } = await getPool().query<{ tag: string }>('SELECT tag FROM tags WHERE doc_id = $1 ORDER BY tag', [
    docId,
  ])
  return rows.map((r) => r.tag)
}

export interface TagCount {
  tag: string
  count: number
}

/** /tags/ 列表页用：全部标签 + 各自条目数。 */
export async function fetchAllTagsWithCounts(): Promise<TagCount[]> {
  const { rows } = await getPool().query<TagCount>(
    'SELECT tag, count(*)::int AS count FROM tags GROUP BY tag ORDER BY count DESC, tag',
  )
  return rows
}

export interface TaggedDocSummary {
  id: string
  doc_type: string
  title: string | null
  doc_date: string | null
}

/** /tags/{tag}/ 详情页用：命中该标签的全部文档（按日期倒序）。 */
export async function fetchDocumentsByTag(tag: string): Promise<TaggedDocSummary[]> {
  const { rows } = await getPool().query<TaggedDocSummary>(
    `SELECT d.id, d.doc_type, d.title, d.doc_date FROM tags t
     JOIN documents d ON d.id = t.doc_id
     WHERE t.tag = $1
     ORDER BY d.doc_date DESC NULLS LAST`,
    [tag],
  )
  return rows
}

export interface SearchResultRow {
  id: string
  doc_type: string
  title: string | null
  excerpt: string
}

const SEARCH_RESULT_LIMIT = 20

/** 全站搜索（M6 起替代 Pagefind）：body_tsv 是 documents 表的生成列，GIN 索引 M0 建表
 * 时就已经预留好（见 03-architecture-proposal.md §3），这里第一次真正用上。
 */
// 注意：正则参数化传入，不要把 \1 这类反向引用直接写进 JS 模板字符串——
// 模板字符串按 JS 转义规则解析，\1 会被当成非法的 legacy octal 转义，
// 在严格模式（ESM 模块天然严格模式）下直接抛 SyntaxError，构建都过不去。
const STRIP_MARKDOWN_IMAGES_RE = '!\\[[^\\]]*\\]\\([^)]*\\)'
const STRIP_MARKDOWN_LINKS_RE = '\\[([^\\]]*)\\]\\([^)]*\\)'
const STRIP_MARKDOWN_LINKS_REPLACEMENT = '\\1'

export async function searchDocuments(query: string): Promise<SearchResultRow[]> {
  const { rows } = await getPool().query<SearchResultRow>(
    `SELECT id, doc_type, title,
       ts_headline(
         'simple',
         -- 摘要用的是 body_md 原始 markdown 源码（不是渲染后的 HTML），先去掉图片/
         -- 链接语法噪音（![alt](url) 整体去掉，[text](url) 只留 text），避免命中片段
         -- 里夹杂一堆原始 URL
         regexp_replace(regexp_replace(body_md, $2, '', 'g'), $3, $4, 'g'),
         websearch_to_tsquery('simple', $1),
         'StartSel=<mark>, StopSel=</mark>, MaxFragments=1, MaxWords=30, MinWords=15'
       ) AS excerpt
     FROM documents
     WHERE body_tsv @@ websearch_to_tsquery('simple', $1)
     ORDER BY ts_rank(body_tsv, websearch_to_tsquery('simple', $1)) DESC
     LIMIT ${SEARCH_RESULT_LIMIT}`,
    [query, STRIP_MARKDOWN_IMAGES_RE, STRIP_MARKDOWN_LINKS_RE, STRIP_MARKDOWN_LINKS_REPLACEMENT],
  )
  return rows
}

// AInews · 只读连接 Postgres ainews_content 库（M6 Live Content Collections 数据源）
// 复用 backend 的同一个 Postgres 实例，独立 database，见 04-roadmap.md §2.7

import pg from 'pg'

// Postgres DATE 类型（oid 1082）node-postgres 默认解析成 JS Date 对象，页面直接插值
// 会触发 Date.prototype.toString()（"Sat Jul 04 2026 00:00:00 GMT+0800..."）。覆盖成
// 原样返回 Postgres 文本协议给出的 "yyyy-MM-dd" 字符串，不做任何转换——这样
// DocumentRow.doc_date 的 `string | null` 类型标注才是真的，不用在每个消费点各自格式化。
pg.types.setTypeParser(1082, (value: string) => value)

let pool: pg.Pool | undefined

function getPool(): pg.Pool {
  if (!pool) {
    const connectionString = process.env.DATABASE_URL
    if (!connectionString) {
      throw new Error('DATABASE_URL 环境变量未设置（frontend 只读连接 Postgres ainews_content 库）')
    }
    pool = new pg.Pool({ connectionString })
    // 空闲连接被数据库端断开（网络抖动/维护重启）时 pg.Pool 会 emit 'error'；不监听的话
    // Node 会把它当成未捕获异常，直接终止这个常驻 SSR 进程，而不只是让当次请求失败。
    pool.on('error', (err) => {
      console.error('Postgres 连接池空闲连接异常：', err)
    })
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

// ---------------------------------------------------------------------------
// 列表页轻量查询：不含 body_md，不经过 postgres-loader.ts 的 toLiveEntry
// （全文 markdown 渲染 + 逐条查 backlinks/tags 的 N+1，只有详情页需要）。
// ---------------------------------------------------------------------------

export interface DocumentSummaryRow {
  id: string
  doc_type: string
  title: string | null
  doc_date: string | null
  frontmatter: Record<string, unknown>
  updated_at: string
}

const SUMMARY_COLUMNS = 'id, doc_type, title, doc_date, frontmatter, updated_at'

export interface PaginatedResult<T> {
  rows: T[]
  total: number
}

/** 列表页首屏 + 懒加载"更多"共用同一个查询：LIMIT/OFFSET 分页，total 用于 hasMore 判断。 */
export async function fetchDocumentSummariesByType(
  docType: string,
  { limit, offset }: { limit: number; offset: number },
): Promise<PaginatedResult<DocumentSummaryRow>> {
  const pool = getPool()
  const [{ rows }, countResult] = await Promise.all([
    pool.query<DocumentSummaryRow>(
      `SELECT ${SUMMARY_COLUMNS} FROM documents WHERE doc_type = $1
       ORDER BY doc_date DESC NULLS LAST, updated_at DESC
       LIMIT $2 OFFSET $3`,
      [docType, limit, offset],
    ),
    pool.query<{ count: number }>('SELECT count(*)::int AS count FROM documents WHERE doc_type = $1', [docType]),
  ])
  return { rows, total: countResult.rows[0]?.count ?? 0 }
}

/** Zettel 列表卡片要显示反链数量（不需要具体谁引用），批量查一次避免逐条 N+1。 */
export async function fetchBacklinkCounts(ids: string[]): Promise<Map<string, number>> {
  if (ids.length === 0) return new Map()
  const { rows } = await getPool().query<{ to_id: string; count: number }>(
    'SELECT to_id, count(*)::int AS count FROM links WHERE to_id = ANY($1) GROUP BY to_id',
    [ids],
  )
  return new Map(rows.map((r) => [r.to_id, r.count]))
}

/** 列表页 eyebrow 统计用：某个 doc_type 的总数，跟当前分页无关，随时反映全表真实值。 */
export async function fetchDocTypeCount(docType: string): Promise<number> {
  const { rows } = await getPool().query<{ count: number }>(
    'SELECT count(*)::int AS count FROM documents WHERE doc_type = $1',
    [docType],
  )
  return rows[0]?.count ?? 0
}

/** 列表页 eyebrow 统计用：某个 doc_type 里 frontmatter 某个字段的去重值数量
 * （如 Originals 的 source_name、Zettel 的 topic_slug）。 */
export async function fetchDistinctFrontmatterFieldCount(docType: string, field: string): Promise<number> {
  const { rows } = await getPool().query<{ count: number }>(
    `SELECT count(DISTINCT frontmatter->>$2)::int AS count FROM documents WHERE doc_type = $1`,
    [docType, field],
  )
  return rows[0]?.count ?? 0
}

/** Topics 列表页 eyebrow 用："更新至 xxx"，doc_date 在 aggregate.py 里每次更新都会
 * 跟着刷新，直接取全表最大值即可，不需要额外解析 frontmatter。 */
export async function fetchMaxDocDate(docType: string): Promise<string | null> {
  const { rows } = await getPool().query<{ max: string | null }>(
    'SELECT max(doc_date) AS max FROM documents WHERE doc_type = $1',
    [docType],
  )
  return rows[0]?.max ?? null
}

/** 首页专用：只取最新一条的 id，交给 getLiveEntry 单条渲染，避免首页把整个 doc_type
 * 的全部文档都跑一遍 toLiveEntry（markdown 渲染 + backlinks/tags 查询）却只用第一条。 */
export async function fetchLatestDocumentId(docType: string): Promise<string | null> {
  const { rows } = await getPool().query<{ id: string }>(
    'SELECT id FROM documents WHERE doc_type = $1 ORDER BY doc_date DESC NULLS LAST, updated_at DESC LIMIT 1',
    [docType],
  )
  return rows[0]?.id ?? null
}

/** Daily 专用：frontmatter.topics 是数组字段，去重需要先展开再计数。 */
export async function fetchDailyDistinctTopicsCount(): Promise<number> {
  const { rows } = await getPool().query<{ count: number }>(
    `SELECT count(DISTINCT t)::int AS count
     FROM documents, jsonb_array_elements_text(frontmatter->'topics') AS t
     WHERE doc_type = 'daily'`,
  )
  return rows[0]?.count ?? 0
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

/** /tags/{tag}/ 详情页用：命中该标签的文档（按日期倒序），LIMIT/OFFSET 分页。 */
export async function fetchDocumentsByTag(
  tag: string,
  { limit, offset }: { limit: number; offset: number },
): Promise<PaginatedResult<TaggedDocSummary>> {
  const pool = getPool()
  const [{ rows }, countResult] = await Promise.all([
    pool.query<TaggedDocSummary>(
      `SELECT d.id, d.doc_type, d.title, d.doc_date FROM tags t
       JOIN documents d ON d.id = t.doc_id
       WHERE t.tag = $1
       ORDER BY d.doc_date DESC NULLS LAST
       LIMIT $2 OFFSET $3`,
      [tag, limit, offset],
    ),
    pool.query<{ count: number }>('SELECT count(*)::int AS count FROM tags WHERE tag = $1', [tag]),
  ])
  return { rows, total: countResult.rows[0]?.count ?? 0 }
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

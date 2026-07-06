// AInews · Live Loader 专用 markdown 渲染
// getLiveEntry/getLiveCollection 拿到的条目没有走 Astro 内置的 build-time 渲染管线
// （那条管线只认 filePath/deferredRender），Live Loader 必须自己把 markdown 编译成
// HTML 并塞进 LiveDataEntry.rendered.html，`astro:content` 的 render() 才能用。
// 复用 astro.config.mjs 里同一个 remarkWikiLink 插件，保证渲染结果一致。

import { createMarkdownProcessor } from '@astrojs/markdown-remark'
import rehypeSanitize, { defaultSchema } from 'rehype-sanitize'
import { remarkWikiLink } from './wiki-link'

// body_md 来源含外部信源原文抓取 + LLM 翻译/摘要产出，Astro 的 markdown 处理器默认
// allowDangerousHtml + rehypeRaw（原始 HTML 标签会原样保留），不加清洗层就是存储型
// XSS 风险面。用 GitHub 风格默认白名单兜底，只额外放行两类我们自己代码生成、已知
// 安全的属性：
//   - wikilink 悬浮预览（wiki-link.ts）：<a> 上的 data-wiki-target/data-preview-title/
//     data-preview-excerpt，走 mdast hProperties 桥接，属性名保持字面 kebab-case
//     （不会被规范化成 camelCase，需要按实际出现的 key 放行）。
//   - Shiki 代码高亮（Astro 内置）：<pre>/<span> 上的 class/style/tabindex/data-language，
//     Shiki 自己构造 hast 节点时用的是原始 HTML 属性名（class/tabindex），不是 hast
//     规范化后的 className/tabIndex，同样需要按字面 key 放行（实测确认过，见
//     .claude/memory/known_issues.md）。
// 默认白名单本身已经拦截 script/iframe/on* 事件属性/javascript: 协议等危险内容，
// 这里不放宽这部分。
function buildSanitizeSchema() {
  const schema = structuredClone(defaultSchema)
  const existingA = (schema.attributes?.a ?? []).filter(
    (entry) => (typeof entry === 'string' ? entry : entry[0]) !== 'className',
  )
  schema.attributes = {
    ...schema.attributes,
    a: [
      ...existingA,
      ['className', 'data-footnote-backref', 'wikilink', 'broken'],
      'data-wiki-target',
      'data-preview-title',
      'data-preview-excerpt',
    ],
    pre: [...(schema.attributes?.pre ?? []), 'class', 'style', 'tabindex', 'dataLanguage'],
    span: [...(schema.attributes?.span ?? []), 'class', 'style'],
  }
  return schema
}

let processorPromise: ReturnType<typeof createMarkdownProcessor> | undefined

function getProcessor() {
  if (!processorPromise) {
    processorPromise = createMarkdownProcessor({
      remarkPlugins: [remarkWikiLink],
      rehypePlugins: [[rehypeSanitize, buildSanitizeSchema()]],
    })
  }
  return processorPromise
}

// M4 配图分级渲染下载成功的图片，body_md 里用自定义 scheme 引用（不用 /media 根相对
// 路径，是因为 trafilatura.extract() 会用 urljoin 把根相对路径误解析成文章原站域名，
// 见 backend .claude/memory/decisions.md「M4 原文抓取三态...」）。前端渲染时改写成
// nginx 实际会服务的静态资源路径（Stage I 把 IMAGE_STORAGE_DIR 同一个 Docker 卷只读
// 挂进 web 容器，nginx 配 `location /media/`）。
const IMAGE_URL_PREFIX = 'ainews-media://'
const MEDIA_SERVE_PREFIX = '/media/'

function rewriteMediaUrls(html: string): string {
  return html.split(IMAGE_URL_PREFIX).join(MEDIA_SERVE_PREFIX)
}

// 渲染结果内存缓存（进程内，不跨重启/多副本共享）：markdown→HTML 是 CPU 密集操作
// （remark AST 遍历 + wikilink 批量查库），文档内容一天最多变一次（跟着批次追加），
// 但改造前每次页面访问都会重新渲染同一份没变过的内容。cacheKey 用 documents.content_hash
// （内容变了 hash 就变，天然失效，不需要显式invalidate）。容量上限防止长期运行的
// Node 进程无限累积缓存吃满内存，用 Map 的插入顺序做简单 LRU（命中时移到最新位置）。
const renderCache = new Map<string, string>()
const RENDER_CACHE_MAX_ENTRIES = 300

function cacheGet(key: string): string | undefined {
  const value = renderCache.get(key)
  if (value !== undefined) {
    renderCache.delete(key)
    renderCache.set(key, value) // 命中后移到最新位置，实现近似 LRU
  }
  return value
}

function cacheSet(key: string, value: string): void {
  if (renderCache.size >= RENDER_CACHE_MAX_ENTRIES) {
    const oldestKey = renderCache.keys().next().value
    if (oldestKey !== undefined) renderCache.delete(oldestKey)
  }
  renderCache.set(key, value)
}

export async function renderMarkdownToHtml(markdown: string, cacheKey?: string): Promise<string> {
  if (cacheKey) {
    const cached = cacheGet(cacheKey)
    if (cached !== undefined) return cached
  }
  const processor = await getProcessor()
  // ainews-media:// 必须在喂给 markdown 处理器之前就改写成 /media/ 相对路径——
  // rehypeSanitize（M6 审查修复新增）的默认协议白名单不认识这个自定义 scheme，
  // 会把整个 src 属性清空，rewriteMediaUrls 这个字符串替换等 render() 跑完再做
  // 就已经晚了（已实测复现：站内全部本地图片 src 变空字符串，naturalWidth=0）。
  const { code } = await processor.render(rewriteMediaUrls(markdown))
  if (cacheKey) cacheSet(cacheKey, code)
  return code
}

// AInews · Live Loader 专用 markdown 渲染
// getLiveEntry/getLiveCollection 拿到的条目没有走 Astro 内置的 build-time 渲染管线
// （那条管线只认 filePath/deferredRender），Live Loader 必须自己把 markdown 编译成
// HTML 并塞进 LiveDataEntry.rendered.html，`astro:content` 的 render() 才能用。
// 复用 astro.config.mjs 里同一个 remarkWikiLink 插件，保证渲染结果一致。

import { createMarkdownProcessor } from '@astrojs/markdown-remark'
import { remarkWikiLink } from './wiki-link'

let processorPromise: ReturnType<typeof createMarkdownProcessor> | undefined

function getProcessor() {
  if (!processorPromise) {
    processorPromise = createMarkdownProcessor({ remarkPlugins: [remarkWikiLink] })
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
  const { code } = await processor.render(markdown)
  const html = rewriteMediaUrls(code)
  if (cacheKey) cacheSet(cacheKey, html)
  return html
}

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

export async function renderMarkdownToHtml(markdown: string): Promise<string> {
  const processor = await getProcessor()
  const { code } = await processor.render(markdown)
  return rewriteMediaUrls(code)
}

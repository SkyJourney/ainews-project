// AInews · 配图静态服务（M6 Stage F/I）
// M4 配图分级渲染下载的图片落在 IMAGE_STORAGE_DIR 这个 Docker 卷（backend/frontend
// 只读挂载同一个卷）。这里没有引入 nginx 反代做静态服务——M6 范围是"把 web 服务跑
// 起来"，nginx 反代属于更完整的生产部署 topology，留给 M7 生产化收尾阶段决定是否引入；
// 现阶段一个简单的 Node 端点足够满足"图片能正常显示"这个需求。
export const prerender = false

import type { APIRoute } from 'astro'
import { createReadStream, existsSync, statSync } from 'node:fs'
import { Readable } from 'node:stream'
import path from 'node:path'

const MEDIA_ROOT = path.resolve(process.env.IMAGE_STORAGE_DIR || '/app/media')

const MIME_TYPES: Record<string, string> = {
  '.png': 'image/png',
  '.jpg': 'image/jpeg',
  '.jpeg': 'image/jpeg',
  '.gif': 'image/gif',
  '.webp': 'image/webp',
  '.svg': 'image/svg+xml',
  '.avif': 'image/avif',
  '.bmp': 'image/bmp',
}

export const GET: APIRoute = async ({ params }) => {
  const relPath = params.path
  if (!relPath) return new Response(null, { status: 404 })

  // 防路径穿越：解析后的绝对路径必须仍然落在 MEDIA_ROOT 之内
  const resolved = path.resolve(MEDIA_ROOT, relPath)
  if (!resolved.startsWith(MEDIA_ROOT + path.sep)) {
    return new Response(null, { status: 400 })
  }
  if (!existsSync(resolved) || !statSync(resolved).isFile()) {
    return new Response(null, { status: 404 })
  }

  const ext = path.extname(resolved).toLowerCase()
  const contentType = MIME_TYPES[ext] ?? 'application/octet-stream'
  const body = Readable.toWeb(createReadStream(resolved)) as ReadableStream

  const headers: Record<string, string> = {
    'Content-Type': contentType,
    // 配图文件名带内容特征（article_key + 序号），内容不会原地变化，可以长缓存
    'Cache-Control': 'public, max-age=31536000, immutable',
  }

  // 图片来自外部信息源（backend enrich.py 下载时接受 image/svg+xml），SVG 可以内嵌
  // <script>——用 image/svg+xml 同源直出，直接导航/被 <object>/<iframe> 嵌入时浏览器
  // 会执行其中的脚本（<img> 标签场景本身不受影响，浏览器不会在 <img> 里跑 SVG 脚本，
  // 这条 CSP 是给"有人直接打开图片链接"这类场景做的兜底防护）。
  if (ext === '.svg') {
    headers['Content-Security-Policy'] = "default-src 'none'; style-src 'unsafe-inline'; sandbox"
  }

  return new Response(body, { headers })
}

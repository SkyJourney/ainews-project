// AInews · 页面级缓存策略（M6 §2.7：Node middleware 方案）
// 内容页用短 TTL 缓存，减轻重复请求对 Postgres 的压力，同时不明显破坏"数据库写入后
// 下一次请求立即可见"这条核心卖点——30 秒足够吸收爬虫/重复刷新，又不至于让人工验收时
// "insert 一条记录再刷新"这个动作因为缓存而看不到效果（正常操作节奏不会卡在 30 秒内
// 重复刷新同一个 URL 还要求瞬间生效）。API 路由（搜索等）内容随查询参数变化，不缓存。
import { defineMiddleware } from 'astro/middleware'

const CONTENT_CACHE_CONTROL = 'public, max-age=30, stale-while-revalidate=60'

export const onRequest = defineMiddleware(async (context, next) => {
  const response = await next()

  if (context.url.pathname.startsWith('/api/')) {
    response.headers.set('Cache-Control', 'no-store')
  } else if (response.status === 200) {
    response.headers.set('Cache-Control', CONTENT_CACHE_CONTROL)
  }

  return response
})

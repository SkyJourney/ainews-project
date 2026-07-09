// AInews · Live Content Collections 定义（M6，请求时查 Postgres）
// 六类文档（含 M10 新增的 deep_dive 周报）全部收拢到这里；content.config.ts +
// vault-loader.ts（build-time，旧 vault 数据源）已经完全没有页面引用，可以删除。

import { defineLiveCollection } from 'astro:content'
import { postgresLiveLoader } from './lib/postgres-loader'

const daily = defineLiveCollection({
  type: 'live',
  loader: postgresLiveLoader('daily'),
})

const topics = defineLiveCollection({
  type: 'live',
  loader: postgresLiveLoader('topic'),
})

const zettel = defineLiveCollection({
  type: 'live',
  loader: postgresLiveLoader('zettel'),
})

const originals = defineLiveCollection({
  type: 'live',
  loader: postgresLiveLoader('original'),
})

const digest = defineLiveCollection({
  type: 'live',
  loader: postgresLiveLoader('digest'),
})

const deepDives = defineLiveCollection({
  type: 'live',
  loader: postgresLiveLoader('deep_dive'),
})

export const collections = { daily, topics, zettel, originals, digest, deepDives }

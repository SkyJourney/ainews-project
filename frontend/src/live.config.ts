// AInews · Live Content Collections 定义（M6，请求时查 Postgres）
// 五类文档除 deep-dives（04-roadmap 五类文档不含这个类型，且该内容当前是空的，
// 前端保留纯静态占位页，不接查询）外全部收拢到这里；content.config.ts +
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

export const collections = { daily, topics, zettel, originals, digest }

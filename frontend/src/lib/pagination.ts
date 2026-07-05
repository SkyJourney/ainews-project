// AInews · 六个导航列表页共用的分页大小常量（Stage 3）
// 按各页面卡片形态/增长速度各自设置，不强求统一——Originals 增长最快、卡片最密，
// 页面尺寸也最大；Topics 数量天然有上限（业务规则封顶个位数到十几个），给一个足够大
// 的值让它实际上很少触发第二页，但保留同一套分页机制不特殊处理。

export const DAILY_PAGE_SIZE = 10 // 每篇卡片信息量大，按月分组展示
export const DIGEST_PAGE_SIZE = 15
export const TOPICS_PAGE_SIZE = 30
export const ZETTEL_PAGE_SIZE = 20 // 2 列 masonry，卡片中等高度
export const ORIGINALS_PAGE_SIZE = 24 // 3 列杂志网格，增长最快、最先出现性能问题的页面

// Topic 详情页：正文按日期区块懒加载（不是跨文档分页，是单篇文档内部按区块切分）。
// Topic 是唯一无限期持续累积内容的文档类型，实测 46KB/9 区块渲染 56ms、合成 20 倍
// 规模（938KB）渲染到 1.7s 且是超线性增长——首屏只渲染最近几天，其余按需渲染，
// 避免单次渲染耗时随历史无限增长。
export const TOPIC_DETAIL_SECTIONS_PER_BATCH = 3

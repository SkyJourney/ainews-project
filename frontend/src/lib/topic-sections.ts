// AInews · Topic 文档正文按 "## YYYY-MM-DD" 日期区块拆分
// Topic 是唯一会无限期持续累积内容的文档类型（每天有新文章就追加一个区块，见
// backend/worker/aggregate.py 的 _insert_topic_block）；详情页不再一次性把整篇
// body_md 渲染成 HTML，只渲染最近几个区块，其余靠滑动懒加载按需渲染（见
// topics/[slug].astro 与 topics/[slug]/more.astro）。
// body_md 里区块永远按日期降序排列（最新的在最前），这里只做切分，不重新排序。

export interface TopicSection {
  key: string // "YYYY-MM-DD"
  markdown: string // 该区块的完整 markdown（含 "## YYYY-MM-DD" 标题行本身）
}

const SECTION_HEADING_RE = /^## (\d{4}-\d{2}-\d{2})\s*$/

export function splitTopicSections(bodyMd: string): TopicSection[] {
  const lines = bodyMd.split('\n')
  const sections: TopicSection[] = []
  let currentKey: string | null = null
  let buffer: string[] = []

  function flush() {
    if (currentKey !== null) {
      sections.push({ key: currentKey, markdown: buffer.join('\n').trim() })
    }
  }

  for (const line of lines) {
    const m = line.match(SECTION_HEADING_RE)
    if (m) {
      flush()
      currentKey = m[1]
      buffer = [line]
    } else if (currentKey !== null) {
      buffer.push(line)
    }
    // 游离在第一个日期标题之前的内容（正常情况下不应该存在）直接丢弃，不影响后续区块
  }
  flush()
  return sections
}

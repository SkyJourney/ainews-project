// AInews · Daily 列表按 "YYYY-MM" 分组，index.astro 首屏与 more.astro 懒加载片段共用
import type { DocumentSummaryRow } from './db'

export interface DailyMonthGroup {
  key: string
  label: string
  entries: DocumentSummaryRow[]
}

export function groupByMonth(rows: DocumentSummaryRow[]): DailyMonthGroup[] {
  const groupsMap = new Map<string, DailyMonthGroup>()
  for (const row of rows) {
    const [year, month] = row.id.split('-')
    const key = `${year}-${month}`
    const label = `${year} · ${month} 月`
    if (!groupsMap.has(key)) groupsMap.set(key, { key, label, entries: [] })
    groupsMap.get(key)!.entries.push(row)
  }
  return Array.from(groupsMap.values())
}

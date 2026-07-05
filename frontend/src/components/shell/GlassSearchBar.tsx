// AInews · 顶栏全站搜索（Preact island + Postgres 全文搜索 API）
// v1 alert 桩 → v2 Pagefind（静态构建时扫描 dist/ 建索引）→ M6 起改为请求时查
// /api/search（Pagefind 的"构建时扫描静态 HTML"模型与 SSR 架构冲突：SSR 页面不再
// 一次性生成，body_tsv 走 Postgres 全文检索才是跟数据源一致的方案）

import { useState, useEffect, useRef, useCallback } from 'preact/hooks'
import { createPortal } from 'preact/compat'

interface SearchResultData {
  url: string
  docType: string
  docTypeLabel: string
  meta: { title?: string }
  excerpt: string
}

async function searchApi(query: string): Promise<SearchResultData[]> {
  const res = await fetch(`/api/search?q=${encodeURIComponent(query)}`)
  if (!res.ok) return []
  const { results } = (await res.json()) as { results: SearchResultData[] }
  return results
}

/** ts_headline 输出的 excerpt 里非 <mark> 部分是标准 HTML 实体转义文本，需要解码回明文再当 Preact 文本节点渲染 */
function decodeEntities(s: string): string {
  return s
    .replace(/&lt;/g, '<')
    .replace(/&gt;/g, '>')
    .replace(/&quot;/g, '"')
    .replace(/&#0?39;/g, "'")
    .replace(/&amp;/g, '&') // 必须最后处理，避免把其它实体里的 & 二次解码
}

/**
 * 把 Pagefind excerpt（形如 "...文本 <mark>命中词</mark> 文本..."）拆成安全渲染的节点数组，
 * 不用 dangerouslySetInnerHTML——全程走 Preact 的文本子节点，杜绝任何 HTML 注入面。
 */
function renderExcerpt(excerpt: string) {
  return excerpt.split(/(<mark>.*?<\/mark>)/g).map((part, i) => {
    const matched = part.match(/^<mark>(.*)<\/mark>$/)
    return matched ? <mark key={i}>{decodeEntities(matched[1])}</mark> : decodeEntities(part)
  })
}

export default function GlassSearchBar() {
  const [macOs, setMacOs] = useState(false)
  const [open, setOpen] = useState(false)
  const [query, setQuery] = useState('')
  const [loading, setLoading] = useState(false)
  const [results, setResults] = useState<SearchResultData[]>([])
  const inputRef = useRef<HTMLInputElement>(null)
  const debounceRef = useRef<ReturnType<typeof setTimeout>>()

  useEffect(() => {
    setMacOs(navigator.platform.toUpperCase().includes('MAC'))
  }, [])

  const runSearch = useCallback(async (q: string) => {
    if (!q.trim()) {
      setResults([])
      setLoading(false)
      return
    }
    setLoading(true)
    const data = await searchApi(q)
    setResults(data)
    setLoading(false)
  }, [])

  useEffect(() => {
    clearTimeout(debounceRef.current)
    debounceRef.current = setTimeout(() => runSearch(query), 200)
    return () => clearTimeout(debounceRef.current)
  }, [query, runSearch])

  const openModal = useCallback(() => {
    setOpen(true)
    requestAnimationFrame(() => inputRef.current?.focus())
  }, [])

  const closeModal = useCallback(() => {
    setOpen(false)
    setQuery('')
    setResults([])
  }, [])

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') {
        e.preventDefault()
        if (open) closeModal()
        else openModal()
      } else if (e.key === 'Escape' && open) {
        closeModal()
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [open, openModal, closeModal])

  const modKey = macOs ? '⌘' : 'Ctrl'

  return (
    <>
      <button class="lumina-searchbar" onClick={openModal} aria-label="全站搜索">
        <span class="material-symbols-outlined lumina-searchbar-icon">search</span>
        <span class="lumina-searchbar-hint">
          <kbd>{modKey}</kbd>
          <kbd>K</kbd>
        </span>
      </button>

      {open &&
        createPortal(
          <div class="search-overlay" onClick={closeModal}>
            <div class="search-modal" onClick={(e) => e.stopPropagation()}>
              <div class="search-modal-input-row">
                <span class="material-symbols-outlined">search</span>
                <input
                  ref={inputRef}
                  type="text"
                  value={query}
                  onInput={(e) => setQuery((e.target as HTMLInputElement).value)}
                  placeholder="搜索 Daily / Topics / Zettel / Originals…"
                />
                <kbd>Esc</kbd>
              </div>
              <div class="search-modal-results">
                {loading && <div class="search-modal-status">搜索中…</div>}
                {!loading && query.trim() && results.length === 0 && (
                  <div class="search-modal-status">没有找到匹配结果</div>
                )}
                {!loading &&
                  results.map((r) => (
                    <a key={r.url} href={r.url} class="search-result">
                      <span class="search-result-tag">{r.docTypeLabel}</span>
                      <div class="search-result-title">{r.meta.title ?? r.url}</div>
                      <div class="search-result-excerpt">{renderExcerpt(r.excerpt)}</div>
                    </a>
                  ))}
              </div>
            </div>
          </div>,
          document.body,
        )}

      <style>{`
        .lumina-searchbar {
          display: inline-flex;
          align-items: center;
          gap: 0.75rem;
          padding: 0.4rem 0.75rem 0.4rem 0.85rem;
          background: var(--surface-container);
          border: 1px solid var(--glass-border-panel);
          border-radius: var(--radius-full);
          color: var(--color-darkgray);
          font-family: var(--font-header);
          font-size: 0.8rem;
          cursor: pointer;
          transition: background 0.15s ease, border-color 0.15s ease;
        }
        .lumina-searchbar:hover {
          background: var(--surface-container-high);
          border-color: var(--color-secondary);
          color: var(--color-dark);
        }
        .lumina-searchbar-icon {
          font-size: 1.1rem;
        }
        .lumina-searchbar-hint {
          display: inline-flex;
          gap: 0.15rem;
        }
        .lumina-searchbar-hint kbd {
          font-family: var(--font-code);
          background: var(--surface-container-highest);
          color: var(--color-darkgray);
          padding: 0.1rem 0.35rem;
          border-radius: var(--radius-sm);
          font-size: 0.7rem;
          box-shadow: 0 1px 0 var(--color-lightgray);
        }

        .search-overlay {
          position: fixed;
          inset: 0;
          z-index: 200;
          display: flex;
          align-items: flex-start;
          justify-content: center;
          padding-top: 12vh;
          background: rgba(11, 14, 26, 0.5);
          backdrop-filter: blur(4px);
        }
        .search-modal {
          width: min(600px, 90vw);
          max-height: 70vh;
          display: flex;
          flex-direction: column;
          background: var(--surface-container-lowest);
          border: 1px solid var(--glass-border-card);
          border-radius: var(--radius-2xl);
          box-shadow: var(--shadow-featured);
          overflow: hidden;
        }
        .search-modal-input-row {
          display: flex;
          align-items: center;
          gap: 0.65rem;
          padding: 0.9rem 1.1rem;
          border-bottom: 1px solid var(--color-lightgray);
        }
        .search-modal-input-row input {
          flex: 1;
          border: none;
          outline: none;
          background: transparent;
          font-size: 1rem;
          color: var(--color-dark);
          font-family: var(--font-body);
        }
        .search-modal-input-row kbd {
          font-family: var(--font-code);
          font-size: 0.7rem;
          color: var(--color-gray);
          background: var(--surface-container-high);
          padding: 0.1rem 0.4rem;
          border-radius: var(--radius-sm);
        }
        .search-modal-results {
          overflow-y: auto;
          padding: 0.5rem;
        }
        .search-modal-status {
          padding: 1.5rem 1rem;
          text-align: center;
          color: var(--color-gray);
          font-size: 0.85rem;
        }
        .search-result {
          display: block;
          padding: 0.65rem 0.85rem;
          border-radius: var(--radius-lg);
          text-decoration: none;
          color: inherit;
          transition: background 0.12s ease;
        }
        .search-result:hover {
          background: var(--surface-container-high);
        }
        .search-result-tag {
          display: inline-block;
          margin-bottom: 0.25rem;
          font-family: var(--font-header);
          font-size: 0.65rem;
          letter-spacing: 0.05em;
          color: var(--color-secondary);
          text-transform: uppercase;
        }
        .search-result-title {
          font-family: var(--font-header);
          font-weight: 600;
          font-size: 0.9rem;
          color: var(--color-dark);
        }
        .search-result-excerpt {
          margin-top: 0.15rem;
          font-size: 0.8rem;
          color: var(--color-darkgray);
          line-height: 1.5;
        }
        .search-result-excerpt mark {
          background: var(--color-highlight);
          color: var(--color-secondary);
          border-radius: 2px;
        }
      `}</style>
    </>
  )
}

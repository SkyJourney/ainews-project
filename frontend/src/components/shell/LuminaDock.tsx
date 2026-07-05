// AInews · FloatingDock（Preact island）
// 6 tab 主导航：Daily / Topics / Deep-Dives / Zettel / Sources / Digest
// 视觉：底部悬浮 pill · 玻璃拟态 · active tab 高亮
// 交互：根据 window.location.pathname 判定 active

import { useEffect, useState } from 'preact/hooks'

interface DockItem {
  label: string
  icon: string // Material Symbols name
  href: string
  matchPrefix: string // location.pathname 命中前缀就算 active
}

const ITEMS: DockItem[] = [
  { label: 'Daily', icon: 'calendar_today', href: '/daily/', matchPrefix: '/daily' },
  { label: 'Topics', icon: 'category', href: '/topics/', matchPrefix: '/topics' },
  { label: 'Deep Dives', icon: 'psychology', href: '/deep-dives/', matchPrefix: '/deep-dives' },
  { label: 'Zettel', icon: 'style', href: '/zettel/', matchPrefix: '/zettel' },
  { label: 'Sources', icon: 'article', href: '/originals/', matchPrefix: '/originals' },
  // M6 新增：旧前端从未消费过 Digest 这类文档，这次数据源切换顺带补上入口
  { label: 'Digest', icon: 'summarize', href: '/digest/', matchPrefix: '/digest' },
]

export default function LuminaDock() {
  const [pathname, setPathname] = useState<string>('/')

  useEffect(() => {
    setPathname(window.location.pathname)
    // 监听 Astro ClientRouter 的 navigate 完成事件
    const onNav = () => setPathname(window.location.pathname)
    document.addEventListener('astro:page-load', onNav)
    return () => document.removeEventListener('astro:page-load', onNav)
  }, [])

  return (
    <nav class="lumina-dock lumina-panel" aria-label="主导航">
      {ITEMS.map((item) => {
        const active = pathname.startsWith(item.matchPrefix)
        return (
          <a
            href={item.href}
            class={`lumina-dock-item ${active ? 'is-active' : ''}`}
            aria-current={active ? 'page' : undefined}
          >
            <span class="material-symbols-outlined lumina-dock-icon">{item.icon}</span>
            <span class="lumina-dock-label">{item.label}</span>
          </a>
        )
      })}
      <style>{`
        .lumina-dock {
          position: fixed;
          bottom: 1.5rem;
          left: 50%;
          transform: translateX(-50%);
          z-index: 50;
          display: flex;
          align-items: center;
          gap: 0.25rem;
          padding: 0.5rem;
        }
        .lumina-dock-item {
          display: inline-flex;
          flex-direction: column;
          align-items: center;
          gap: 0.15rem;
          padding: 0.55rem 0.85rem;
          border-radius: var(--radius-full);
          color: var(--color-darkgray);
          font-family: var(--font-header);
          font-size: 0.7rem;
          letter-spacing: 0.03em;
          text-decoration: none;
          transition: background 0.15s ease, color 0.15s ease, transform 0.15s ease;
          min-width: 4.5rem;
        }
        .lumina-dock-item:hover {
          background: var(--surface-container-high);
          color: var(--color-dark);
          transform: translateY(-1px);
        }
        .lumina-dock-item.is-active {
          background: var(--color-dark);
          color: var(--color-light);
        }
        .lumina-dock-item.is-active .lumina-dock-icon {
          color: var(--ai-accent);
          font-variation-settings: 'FILL' 1;
        }
        .lumina-dock-icon {
          font-size: 1.35rem;
        }
        @media (max-width: 480px) {
          .lumina-dock-item {
            min-width: 3.5rem;
            padding: 0.5rem 0.6rem;
          }
          .lumina-dock-label {
            display: none;
          }
        }
      `}</style>
    </nav>
  )
}

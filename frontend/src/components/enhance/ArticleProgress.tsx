// AInews · Original 详情页右栏阅读进度条（Preact island）
// 按目标区块（默认正文 .orig-detail-body）相对视口的滚动比例计算百分比

import { useEffect, useState } from 'preact/hooks'

interface Props {
  targetSelector?: string
}

export default function ArticleProgress({ targetSelector = '.orig-detail-body' }: Props) {
  const [progress, setProgress] = useState(0)

  useEffect(() => {
    const target = document.querySelector<HTMLElement>(targetSelector)
    if (!target) return

    let ticking = false
    const update = () => {
      ticking = false
      const rect = target.getBoundingClientRect()
      const total = rect.height - window.innerHeight
      if (total <= 0) {
        setProgress(rect.top <= 0 ? 100 : 0)
        return
      }
      const scrolled = -rect.top
      setProgress(Math.min(100, Math.max(0, (scrolled / total) * 100)))
    }

    const onScroll = () => {
      if (ticking) return
      ticking = true
      requestAnimationFrame(update)
    }

    update()
    window.addEventListener('scroll', onScroll, { passive: true })
    window.addEventListener('resize', onScroll)
    return () => {
      window.removeEventListener('scroll', onScroll)
      window.removeEventListener('resize', onScroll)
    }
  }, [targetSelector])

  return (
    <div class="article-progress" role="progressbar" aria-valuenow={Math.round(progress)} aria-valuemin={0} aria-valuemax={100} aria-label="阅读进度">
      <div class="article-progress-track">
        <div class="article-progress-fill" style={{ width: `${progress}%` }} />
      </div>
      <span class="article-progress-label">{Math.round(progress)}%</span>
      <style>{`
        .article-progress {
          display: flex;
          align-items: center;
          gap: 0.5rem;
        }
        .article-progress-track {
          flex: 1;
          height: 4px;
          background: var(--surface-container-high);
          border-radius: var(--radius-full);
          overflow: hidden;
        }
        .article-progress-fill {
          height: 100%;
          background: linear-gradient(90deg, var(--color-secondary), var(--ai-accent));
          transition: width 0.1s linear;
        }
        .article-progress-label {
          font-family: var(--font-code);
          font-size: 0.7rem;
          color: var(--color-gray);
          min-width: 2.2em;
          text-align: right;
        }
      `}</style>
    </div>
  )
}

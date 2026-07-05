// AInews · 全站 wikilink 交互：hover 预览 popover + 断链点击拦截
// 事件委托挂在 document.body 上，覆盖所有由 <Content /> 渲染出的静态 wikilink <a>

let popover: HTMLDivElement | null = null
let popoverTitleEl: HTMLElement | null = null
let popoverExcerptEl: HTMLElement | null = null
let hoverTimer: ReturnType<typeof setTimeout> | undefined

function ensurePopover(): HTMLDivElement {
  if (popover) return popover
  popover = document.createElement('div')
  popover.className = 'wikilink-popover'
  popover.setAttribute('role', 'tooltip')
  popoverTitleEl = document.createElement('strong')
  popoverExcerptEl = document.createElement('p')
  popover.append(popoverTitleEl, popoverExcerptEl)
  document.body.appendChild(popover)
  return popover
}

function showPopover(target: HTMLElement) {
  const title = target.dataset.previewTitle
  if (!title) return
  const excerpt = target.dataset.previewExcerpt ?? ''
  const el = ensurePopover()
  popoverTitleEl!.textContent = title
  popoverExcerptEl!.textContent = excerpt
  const rect = target.getBoundingClientRect()
  el.style.left = `${rect.left + window.scrollX}px`
  el.style.top = `${rect.bottom + window.scrollY + 6}px`
  el.classList.add('is-visible')
}

function hidePopover() {
  popover?.classList.remove('is-visible')
}

document.body.addEventListener('mouseover', (e) => {
  const target = (e.target as HTMLElement).closest<HTMLElement>('a.wikilink[data-preview-title]')
  if (!target) return
  clearTimeout(hoverTimer)
  hoverTimer = setTimeout(() => showPopover(target), 150)
})

document.body.addEventListener('mouseout', (e) => {
  const target = (e.target as HTMLElement).closest<HTMLElement>('a.wikilink[data-preview-title]')
  if (!target) return
  clearTimeout(hoverTimer)
  hidePopover()
})

// 断链（class="broken"）href 是占位 "#"，点击拦截避免跳到页面顶部
document.body.addEventListener('click', (e) => {
  const target = (e.target as HTMLElement).closest<HTMLElement>('a.wikilink.broken')
  if (!target) return
  e.preventDefault()
})

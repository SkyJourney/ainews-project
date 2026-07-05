// AInews · 深色模式切换
// 事件委托挂在 document 上（不是查询单个 button 绑定）——ClientRouter 每次导航都会
// 重新渲染 Header，若直接绑定旧 button 引用，导航后按钮会失去点击响应。
// astro:page-load 在首次加载和每次 view transition 后都会触发，用来同步图标状态。
//
// astro:before-swap 是关键一环：ClientRouter 客户端导航时用抓回来的新页面 document
// 替换当前 <html>，而新页面的原始 HTML 本身没有 data-theme（这个属性只在 BaseLayout
// 的 FOUC 防护 inline script 首次执行时才被写上）。不在 swap 前把旧 document 的
// data-theme 同步过去，每次点链接主题都会被新页面的"裸" <html> 冲掉，只有整页刷新
// （FOUC script 重新跑一遍）才会恢复——这正是之前"点链接变浅色、刷新变回深色"的成因。

const STORAGE_KEY = 'ainews-theme'

function currentTheme(): 'light' | 'dark' {
  return document.documentElement.dataset.theme === 'dark' ? 'dark' : 'light'
}

function syncIcon() {
  const icon = document.querySelector<HTMLElement>('[data-theme-toggle] .material-symbols-outlined')
  if (icon) icon.textContent = currentTheme() === 'dark' ? 'light_mode' : 'dark_mode'
}

document.addEventListener('astro:before-swap', (e) => {
  const event = e as Event & { newDocument: Document }
  event.newDocument.documentElement.dataset.theme = currentTheme()
})

document.addEventListener('astro:page-load', syncIcon)

document.addEventListener('click', (e) => {
  const target = (e.target as HTMLElement).closest<HTMLElement>('[data-theme-toggle]')
  if (!target) return
  const next: 'light' | 'dark' = currentTheme() === 'dark' ? 'light' : 'dark'
  document.documentElement.dataset.theme = next
  localStorage.setItem(STORAGE_KEY, next)
  syncIcon()
})

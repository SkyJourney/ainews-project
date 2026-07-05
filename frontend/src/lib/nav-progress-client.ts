// AInews · 顶部导航进度条驱动脚本
// astro:before-preparation 在导航请求发出前触发，astro:after-preparation 在新页面
// fetch + 解析完成、swap 之前触发——这对事件成对覆盖的正是 SSR 往返等待的时间窗口。
// 先切到 transition:none 强制归零、再用 getBoundingClientRect 触发一次同步重排，
// 才能让下一次 width 变化被浏览器当成新的过渡来播放，否则连续导航时会被合并跳过。

const fill = document.querySelector<HTMLDivElement>('#nav-progress-bar-fill')

if (fill) {
  let hideTimer: ReturnType<typeof setTimeout> | undefined

  const start = () => {
    clearTimeout(hideTimer)
    fill.style.transition = 'none'
    fill.style.opacity = '1'
    fill.style.width = '0%'
    fill.getBoundingClientRect()
    fill.style.transition = 'width 4s cubic-bezier(0.1, 0.7, 0.3, 1)'
    fill.style.width = '85%'
  }

  const finish = () => {
    fill.style.transition = 'width 0.2s ease-out'
    fill.style.width = '100%'
    hideTimer = setTimeout(() => {
      fill.style.transition = 'opacity 0.3s ease-out'
      fill.style.opacity = '0'
    }, 200)
  }

  document.addEventListener('astro:before-preparation', start)
  document.addEventListener('astro:after-preparation', finish)
}

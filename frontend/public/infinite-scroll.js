// AInews · 共享滑动懒加载脚本（Stage 3，六个导航列表页共用）
//
// 约定：
// - 容器：<div data-infinite-list data-endpoint="/xxx/more" data-page="2" data-mode="flat|grouped">
//   容器内是首屏已渲染的卡片 + 一个哨兵 <div data-scroll-sentinel></div>（放在最后一个）
// - 片段端点：GET {endpoint}?page=N，返回 <div data-fragment>...</div> 包裹的 HTML；
//   flat 模式包裹卡片元素序列；grouped 模式包裹完整的
//   <section data-month="YYYY-MM">...<div data-month-entries>...</div></section> 序列。
//   必须用 data-fragment 包一层显式取子节点——Astro 即使不接 layout 也会在片段响应里
//   带上该页面用到的组件 <style> 块（真实构建验证过），不能直接假设"顶层子节点就是卡片"。
// - 响应头 X-Has-More: "true" | "false" 标记是否还有更多，不用靠返回条数推断
//
// 不引入任何框架：全站只有这一份纯 vanilla 脚本，六个列表页共用同一套逻辑，
// 各自的差异（endpoint/mode/起始页码）都是纯数据，走 data-* 属性传入。
//
// 全站接了 Astro <ClientRouter />（View Transitions）：导航是客户端 DOM 替换，
// 不是整页刷新；同一个 <script src="/infinite-scroll.js"> 标签在切页后会被当作
// "未变化节点"保留、不会重新执行，顶层代码只在第一次加载时跑一次。必须监听
// astro:page-load（首次加载 + 每次切页完成后都会触发）才能在切到新列表页时正确
// 接管新容器——这也是"整页刷新恢复正常、单纯切页失效"这个 bug 的根因。

;(function () {
  const MAX_AUTO_FILL_ROUNDS = 20 // 安全阀：内容异常稀疏时也不会无限自动请求下去
  let activeObservers = []

  function initList(container) {
    if (container.dataset.infiniteListInitialized === 'true') return // 防止重复初始化
    container.dataset.infiniteListInitialized = 'true'

    const sentinel = container.querySelector('[data-scroll-sentinel]')
    if (!sentinel) return

    const endpoint = container.dataset.endpoint
    const mode = container.dataset.mode || 'flat'
    let page = Number(container.dataset.page || '2')
    let loading = false
    let hasMore = true
    let autoRounds = 0

    function appendFlat(fragmentRoot) {
      const root = fragmentRoot.querySelector('[data-fragment]')
      if (!root) return
      for (const el of Array.from(root.children)) {
        container.insertBefore(el, sentinel)
      }
    }

    function appendGrouped(fragmentRoot) {
      const root = fragmentRoot.querySelector('[data-fragment]')
      if (!root) return
      for (const section of Array.from(root.children)) {
        const monthKey = section.getAttribute('data-month')
        const existingGroups = container.querySelectorAll(':scope > [data-month]')
        const lastGroup = existingGroups[existingGroups.length - 1]
        if (lastGroup && lastGroup.getAttribute('data-month') === monthKey) {
          // 同一个月被分页切断在两批里，合并进已有分组，不重复渲染月份标题
          const targetEntries = lastGroup.querySelector('[data-month-entries]')
          const incomingEntries = section.querySelector('[data-month-entries]')
          if (targetEntries && incomingEntries) {
            targetEntries.append(...Array.from(incomingEntries.children))
            continue
          }
        }
        container.insertBefore(section, sentinel)
      }
    }

    async function loadMore() {
      if (loading || !hasMore) return
      loading = true
      try {
        const res = await fetch(`${endpoint}?page=${page}`)
        if (!res.ok) throw new Error(`片段请求失败：${res.status}`)
        hasMore = res.headers.get('X-Has-More') === 'true'
        const html = await res.text()
        const template = document.createElement('template')
        template.innerHTML = html.trim()
        if (mode === 'grouped') {
          appendGrouped(template.content)
        } else {
          appendFlat(template.content)
        }
        page += 1
      } catch (err) {
        console.error('[infinite-scroll]', err)
        hasMore = false
      } finally {
        loading = false
        if (!hasMore) {
          observer.disconnect()
          sentinel.remove()
        } else {
          maybeAutoFill()
        }
      }
    }

    // 首屏内容不够填满视口时（比如很短的列表、或者用户屏幕特别高），哨兵一开始
    // 就已经在视口内、正常滚动事件不会再触发——这里主动检测一次并立即补一批，
    // 循环直到填满视口或没有更多数据，保证懒加载不会卡在"看起来到底了其实没到"的空白状态。
    function maybeAutoFill() {
      if (!hasMore || loading || autoRounds >= MAX_AUTO_FILL_ROUNDS) return
      const sentinelNearViewport = sentinel.getBoundingClientRect().top < window.innerHeight
      const pageNotFull = document.documentElement.scrollHeight <= window.innerHeight
      if (sentinelNearViewport || pageNotFull) {
        autoRounds += 1
        loadMore()
      }
    }

    const observer = new IntersectionObserver(
      (entries) => {
        if (entries.some((e) => e.isIntersecting)) loadMore()
      },
      { rootMargin: '400px' }, // 提前 400px 触发，滚动到底前就已经加载好，视觉上更丝滑
    )
    observer.observe(sentinel)
    activeObservers.push(observer)
    maybeAutoFill()
  }

  function initAll() {
    document.querySelectorAll('[data-infinite-list]').forEach(initList)
  }

  // 首次加载 + 每次 ClientRouter 完成页面切换后都重新扫描当前 DOM 里的列表容器
  document.addEventListener('astro:page-load', initAll)

  // 切走当前页面前断开旧 observer，避免残留引用（旧容器会被 View Transitions 替换掉，
  // 不清理的话 observer 仍持有对已从文档移除的节点的引用）
  document.addEventListener('astro:before-swap', () => {
    for (const observer of activeObservers) observer.disconnect()
    activeObservers = []
  })
})()

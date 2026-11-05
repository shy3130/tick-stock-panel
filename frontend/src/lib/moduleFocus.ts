/**
 * 模块放大 — 点选界面上的某个卡片/面板时, 把它提到最上层、铺满当前浏览器视口,
 * 并按同一倍数把模块整体放大 (字号/图标/间距/图表一起放大, 见 index.css 里
 * .tf-module-max 的 --tf-max-zoom), 再点右上角「还原」或按 Esc 恢复。
 *
 * 为什么不是 React 组件: 要看住的是「全站任意卡片」(十几个页面, 数百个 section),
 * 逐个包一层成本高且要改所有页面。这里用一次性的全局委托:
 *   document click(capture) → 从落点向上找最近的可视"卡片"→ 加 .tf-module-max。
 * 视觉与 !important 覆盖全在 index.css「模块放大」一节, 这里只做判定与开关。
 *
 * 判定规则 (宁可少放大, 不误放大):
 * - 落点或任一祖先命中交互元素 (a/button/input/弹窗…) → 不处理。
 * - mousedown → click 之间位移 > 6px (拖拽/选文字) → 不处理。
 * - 有文本选区 / 按了修饰键 / 点击来自弹窗 → 不处理。
 * - 卡片判定: 有圆角 (≥4px) + 有边框或阴影 + 不透明底色, 且尺寸 ≥ 180×60;
 *   面积超过视口 82% 的视为页面容器, 停止向上找。
 * - 放大态下不再响应点击 (卡片内部交互保持原样), 退出只走 Esc/还原按钮。
 * - 需要退出放大的元素 (如自己就是全屏区域) 加 data-module-skip。
 *
 * fixed 定位会被祖先的 transform/filter/backdrop-filter 劫持 (react-grid-layout
 * 的定位 transform、framer-motion 动画层、卡片自带的 backdrop-blur),
 * 所以放大时给这些祖先挂 .tf-module-flat 临时抹掉, 退出时摘掉 — 不碰内联样式,
 * 也不会和 React/RGL 的写回打架。
 */
const SKIP_SELECTOR = [
  'a[href]',
  'button',
  'input',
  'select',
  'textarea',
  'label',
  'summary',
  'option',
  '[role="button"]',
  '[role="link"]',
  '[role="tab"]',
  '[role="menu"]',
  '[role="menuitem"]',
  '[role="dialog"]',
  '[role="tooltip"]',
  '[contenteditable="true"]',
  '[data-module-skip]',
  '.tf-module-bar',
].join(',')

/** 卡片最小可视尺寸 — 低于此尺寸的多是标签/小徽章, 放大没有意义。 */
const MIN_W = 180
const MIN_H = 60
/** 超过视口该比例的元素视为页面级容器, 不再当作模块。 */
const MAX_VIEWPORT_RATIO = 0.82
/** 位移超过该像素视为拖拽/划选, 不触发放大。 */
const DRAG_TOLERANCE = 6
/** 放大态下的失联巡检间隔 (卡片被 React 卸载后必须把祖先样式还原)。 */
const WATCHDOG_MS = 400

/** 整块放大的倍数区间 (字体/图标/间距/图表一起放大, 见 index.css .tf-module-max)。 */
const ZOOM_MAX = 1.5
const ZOOM_MIN = 1.05

/**
 * 自适应倍数: 放大后卡片内部按「视口/倍数」重新排版再整体放大, 倍数取太大会把
 * 原本就占满宽度的模块挤窄 (表格列被压扁)。所以倍数上限 = 维持原有布局密度所需的
 * 倍数 (min(视口宽/卡片宽, 视口高/卡片高)), 再夹在 [1.05, 1.5] 之间并取 0.05 步长。
 * 小卡片 (看板组件) 一般吃满 1.5; 整页宽的大表格基本只是铺满屏幕 + 轻微放大。
 * 卡片 rect 用视觉坐标 (已含界面缩放), 所以页面缩放不影响判定。
 */
function computeZoom(rect: DOMRect): number {
  const vw = window.innerWidth
  const vh = window.innerHeight
  if (!rect.width || !rect.height) return ZOOM_MAX
  const fit = Math.min(vw / rect.width, vh / rect.height)
  const zoom = Math.min(ZOOM_MAX, Math.max(ZOOM_MIN, fit))
  return Math.round(zoom * 20) / 20
}

interface ActiveMax {
  card: HTMLElement
  flattened: HTMLElement[]
  bar: HTMLElement
  scrollY: number
}

let active: ActiveMax | null = null
let installed = false
let watchdog: number | null = null
let downX = 0
let downY = 0

function areaOf(el: Element): number {
  const r = el.getBoundingClientRect()
  return r.width * r.height
}

/** 视觉上是否像一张卡片 (圆角 + 边框或阴影 + 不透明底色)。 */
function looksLikeModule(el: HTMLElement): boolean {
  const cs = getComputedStyle(el)
  if (cs.display === 'inline' || cs.display === 'contents' || cs.visibility === 'hidden') return false
  const rect = el.getBoundingClientRect()
  if (rect.width < MIN_W || rect.height < MIN_H) return false
  if (areaOf(el) > window.innerWidth * window.innerHeight * MAX_VIEWPORT_RATIO) return false

  const radius = Math.max(
    parseFloat(cs.borderTopLeftRadius) || 0,
    parseFloat(cs.borderTopRightRadius) || 0,
    parseFloat(cs.borderBottomLeftRadius) || 0,
    parseFloat(cs.borderBottomRightRadius) || 0,
  )
  if (radius < 4) return false

  const bordered = parseFloat(cs.borderTopWidth) > 0 && cs.borderTopStyle !== 'none' && cs.borderTopStyle !== 'hidden'
  const shadowed = !!cs.boxShadow && cs.boxShadow !== 'none'
  const bg = cs.backgroundColor || ''
  const hasBg = !!bg && bg !== 'transparent' && bg !== 'rgba(0, 0, 0, 0)'
  return bordered || (shadowed && hasBg)
}

/** 从落点向上找最外层的合格卡片 (即用户眼里的"这个模块")。 */
function findModule(target: Element | null): HTMLElement | null {
  if (!target) return null
  if (target.closest(SKIP_SELECTOR)) return null

  const limit = window.innerWidth * window.innerHeight * MAX_VIEWPORT_RATIO
  let best: HTMLElement | null = null
  let node: HTMLElement | null = target instanceof HTMLElement ? target : target.parentElement
  while (node && node !== document.body && node.id !== 'root') {
    if (looksLikeModule(node)) best = node
    else if (areaOf(node) > limit) break   // 已到页面级容器, 不再往上看
    node = node.parentElement
  }
  return best
}

/** 该祖先是否会劫持 fixed 定位 (transform/filter/backdrop-filter/contain…)。 */
function hijacksFixed(cs: CSSStyleDeclaration): boolean {
  const willChange = cs.willChange || ''
  return (
    (cs.transform && cs.transform !== 'none') ||
    (cs.translate && cs.translate !== 'none') ||
    (cs.rotate && cs.rotate !== 'none') ||
    (cs.scale && cs.scale !== 'none') ||
    (cs.filter && cs.filter !== 'none') ||
    (cs.backdropFilter && cs.backdropFilter !== 'none') ||
    (cs.perspective && cs.perspective !== 'none') ||
    (cs.contain && cs.contain !== 'none') ||
    willChange.includes('transform') ||
    willChange.includes('filter') ||
    willChange.includes('perspective')
  )
}

function createBar(zoom: number, onRestore: () => void): HTMLElement {
  const bar = document.createElement('div')
  bar.className = 'tf-module-bar'
  bar.setAttribute('role', 'status')

  const hint = document.createElement('span')
  hint.className = 'tf-module-bar-hint'
  // 显示实际生效的倍数, 便于用户判断这块被放大了多少
  hint.textContent = `已放大 ×${zoom}`

  const btn = document.createElement('button')
  btn.type = 'button'
  btn.className = 'tf-module-bar-btn'
  btn.textContent = '还原 (Esc)'
  btn.addEventListener('click', (e) => {
    e.preventDefault()
    e.stopPropagation()
    onRestore()
  })

  bar.append(hint, btn)
  return bar
}

function notifyResize(): void {
  // 图表/虚拟列表按容器尺寸重排: 连续两帧 + 过渡后再补一次
  requestAnimationFrame(() => {
    window.dispatchEvent(new Event('resize'))
    setTimeout(() => window.dispatchEvent(new Event('resize')), 160)
  })
}

function maximize(card: HTMLElement): void {
  if (active) return
  // 先量原尺寸 (此时还没套放大样式), 据此决定自适应倍数
  const zoom = computeZoom(card.getBoundingClientRect())

  const flattened: HTMLElement[] = []
  let parent = card.parentElement
  while (parent && parent !== document.body && parent.id !== 'root') {
    if (hijacksFixed(getComputedStyle(parent))) {
      parent.classList.add('tf-module-flat')
      flattened.push(parent)
    }
    parent = parent.parentElement
  }

  // 倍数走内联自定义属性覆盖 class 里的默认值 (1/zoom 用于把布局尺寸反向缩回去)
  card.style.setProperty('--tf-max-zoom', String(zoom))
  card.style.setProperty('--tf-max-zoom-inv', String(Math.round((1 / zoom) * 10000) / 10000))
  card.classList.add('tf-module-max')
  const bar = createBar(zoom, () => restore())
  document.body.appendChild(bar)
  active = { card, flattened, bar, scrollY: window.scrollY }
  // 卡片脱离原位置后可能顶出滚动条, 锁住页面滚动条位置不再抖动
  window.scrollTo({ top: active.scrollY })
  notifyResize()

  if (watchdog === null) {
    watchdog = window.setInterval(() => {
      if (active && !active.card.isConnected) restore()
    }, WATCHDOG_MS)
  }
}

function restore(): void {
  if (!active) return
  const { card, flattened, bar, scrollY } = active
  active = null
  card.classList.remove('tf-module-max')
  card.style.removeProperty('--tf-max-zoom')
  card.style.removeProperty('--tf-max-zoom-inv')
  for (const el of flattened) el.classList.remove('tf-module-flat')
  bar.remove()
  if (watchdog !== null) {
    window.clearInterval(watchdog)
    watchdog = null
  }
  window.scrollTo({ top: scrollY })
  notifyResize()
}

function onClick(e: MouseEvent): void {
  if (active) return                       // 放大态: 卡片内部照常交互, 退出只走 Esc/还原
  if (e.defaultPrevented) return
  if (e.button !== 0 || e.metaKey || e.ctrlKey || e.altKey) return
  if (Math.abs(e.clientX - downX) > DRAG_TOLERANCE || Math.abs(e.clientY - downY) > DRAG_TOLERANCE) return
  const selection = window.getSelection()
  if (selection && selection.toString().length > 0) return
  const target = e.target as Element | null
  if (!target || !(target instanceof Element)) return
  const card = findModule(target)
  if (card) maximize(card)
}

function onMouseDown(e: MouseEvent): void {
  downX = e.clientX
  downY = e.clientY
}

function onKeyDown(e: KeyboardEvent): void {
  if (!active || e.key !== 'Escape') return
  // 卡片内部弹出的对话框优先吃 Esc
  if (document.querySelector('[role="dialog"]')) return
  e.preventDefault()
  e.stopPropagation()
  restore()
}

/** 安装全局委托 (幂等); 返回卸载函数。 */
export function installModuleFocus(): () => void {
  if (installed) return () => {}
  installed = true
  document.addEventListener('mousedown', onMouseDown, true)
  document.addEventListener('click', onClick, true)
  document.addEventListener('keydown', onKeyDown, true)
  const onNavigate = () => restore()
  window.addEventListener('popstate', onNavigate)
  window.addEventListener('hashchange', onNavigate)
  return () => {
    installed = false
    document.removeEventListener('mousedown', onMouseDown, true)
    document.removeEventListener('click', onClick, true)
    document.removeEventListener('keydown', onKeyDown, true)
    window.removeEventListener('popstate', onNavigate)
    window.removeEventListener('hashchange', onNavigate)
    restore()
  }
}

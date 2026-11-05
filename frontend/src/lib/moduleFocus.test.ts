// @vitest-environment jsdom
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { installModuleFocus } from './moduleFocus'

/**
 * 全局点击委托的行为锁定 —— 它是全站生效的 (任何一次误判都会劫持正常点击),
 * 所以把「什么该放大 / 什么不该 / 怎么退出」写成测试。
 * jsdom 没有布局引擎: 卡片的尺寸与视觉样式在这里显式桩掉。
 */

const CARD_STYLE = {
  display: 'block',
  visibility: 'visible',
  borderTopLeftRadius: '8px',
  borderTopRightRadius: '8px',
  borderBottomLeftRadius: '8px',
  borderBottomRightRadius: '8px',
  borderTopWidth: '1px',
  borderTopStyle: 'solid',
  borderLeftWidth: '1px',
  borderLeftStyle: 'solid',
  boxShadow: 'none',
  backgroundColor: 'rgb(24, 24, 27)',
  transform: 'none',
  translate: 'none',
  rotate: 'none',
  scale: 'none',
  filter: 'none',
  backdropFilter: 'none',
  perspective: 'none',
  contain: 'none',
  willChange: 'auto',
}

const PLAIN_STYLE = {
  display: 'block',
  visibility: 'visible',
  borderTopLeftRadius: '0px',
  borderTopRightRadius: '0px',
  borderBottomLeftRadius: '0px',
  borderBottomRightRadius: '0px',
  borderTopWidth: '0px',
  borderTopStyle: 'none',
  borderLeftWidth: '0px',
  borderLeftStyle: 'none',
  boxShadow: 'none',
  backgroundColor: 'rgba(0, 0, 0, 0)',
  transform: 'none',
  translate: 'none',
  rotate: 'none',
  scale: 'none',
  filter: 'none',
  backdropFilter: 'none',
  perspective: 'none',
  contain: 'none',
  willChange: 'auto',
}

let uninstall: (() => void) | null = null

/** 真实浏览器里 click 前必有 mousedown — 位移容差判定依赖这两者的坐标。 */
function clickAt(el: Element, x = 10, y = 10) {
  el.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, clientX: x, clientY: y }))
  el.dispatchEvent(new MouseEvent('click', { bubbles: true, clientX: x, clientY: y }))
}

function rect(width: number, height: number) {
  return { width, height, top: 0, left: 0, right: width, bottom: height, x: 0, y: 0, toJSON: () => ({}) } as DOMRect
}

function buildCard(opts: { withButton?: boolean } = {}) {
  document.body.innerHTML = ''
  const root = document.createElement('div')
  root.id = 'root'
  const page = document.createElement('div')
  const card = document.createElement('section')
  card.className = 'panel'
  const body = document.createElement('span')
  body.textContent = '正文'
  card.appendChild(body)
  if (opts.withButton) {
    const btn = document.createElement('button')
    btn.textContent = '明细'
    card.appendChild(btn)
  }
  page.appendChild(card)
  root.appendChild(page)
  document.body.appendChild(root)

  card.getBoundingClientRect = () => rect(400, 300)
  page.getBoundingClientRect = () => rect(1024, 700)
  root.getBoundingClientRect = () => rect(1024, 800)

  vi.spyOn(window, 'getComputedStyle').mockImplementation((el: Element) =>
    ({ ...(el.classList.contains('panel') ? CARD_STYLE : PLAIN_STYLE) }) as unknown as CSSStyleDeclaration,
  )
  return { card, body }
}

beforeEach(() => {
  window.scrollTo = vi.fn() as unknown as typeof window.scrollTo
  uninstall = installModuleFocus()
})

afterEach(() => {
  uninstall?.()
  uninstall = null
  vi.restoreAllMocks()
  document.body.innerHTML = ''
})

it('点击卡片正文 → 卡片提升为放大态, 并出现还原操作条', () => {
  const { card, body } = buildCard()
  clickAt(body)
  expect(card.classList.contains('tf-module-max')).toBe(true)
  expect(document.querySelector('.tf-module-bar')).not.toBeNull()
})

it('放大倍数按卡片尺寸自适应写入, 还原后清除', () => {
  const { card, body } = buildCard()
  // 卡片 400x300 / 视口 1024x768 → 维持原布局密度可放大 2.56 倍, 取上限 1.5
  clickAt(body)
  expect(card.style.getPropertyValue('--tf-max-zoom')).toBe('1.5')
  expect(Number(card.style.getPropertyValue('--tf-max-zoom-inv'))).toBeCloseTo(1 / 1.5, 3)
  document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))
  expect(card.style.getPropertyValue('--tf-max-zoom')).toBe('')
})

it('按 Esc → 退出放大并摘掉操作条', () => {
  const { card, body } = buildCard()
  clickAt(body)
  expect(card.classList.contains('tf-module-max')).toBe(true)
  document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))
  expect(card.classList.contains('tf-module-max')).toBe(false)
  expect(document.querySelector('.tf-module-bar')).toBeNull()
})

it('点操作条上的「还原」→ 退出放大', () => {
  const { card, body } = buildCard()
  clickAt(body)
  const btn = document.querySelector<HTMLButtonElement>('.tf-module-bar-btn')!
  clickAt(btn)
  expect(card.classList.contains('tf-module-max')).toBe(false)
})

it('点卡片内的按钮/链接 → 不放大 (交互优先)', () => {
  const { card } = buildCard({ withButton: true })
  clickAt(card.querySelector('button')!)
  expect(card.classList.contains('tf-module-max')).toBe(false)
})

it('拖拽/划选 (位移超过容差) → 不放大', () => {
  const { card, body } = buildCard()
  body.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, clientX: 10, clientY: 10 }))
  body.dispatchEvent(new MouseEvent('click', { bubbles: true, clientX: 40, clientY: 10 }))
  expect(card.classList.contains('tf-module-max')).toBe(false)
})

it('放大态下再点卡片内部 → 保持放大, 不重复处理', () => {
  const { card, body } = buildCard()
  clickAt(body)
  clickAt(body)
  expect(card.classList.contains('tf-module-max')).toBe(true)
  expect(document.querySelectorAll('.tf-module-bar').length).toBe(1)
})

it('祖先带 transform (如 react-grid-layout) 时临时抹平, 退出后恢复', () => {
  const { card, body } = buildCard()
  const page = card.parentElement as HTMLElement
  page.getBoundingClientRect = () => rect(1024, 700)
  // 让成该祖先带 transform
  const base = window.getComputedStyle
  vi.spyOn(window, 'getComputedStyle').mockImplementation((el: Element) => {
    const style = base(el)
    if (el === page) return { ...style, transform: 'translate(0px, 40px)' } as CSSStyleDeclaration
    return style
  })
  clickAt(body)
  expect(page.classList.contains('tf-module-flat')).toBe(true)
  document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))
  expect(page.classList.contains('tf-module-flat')).toBe(false)
})

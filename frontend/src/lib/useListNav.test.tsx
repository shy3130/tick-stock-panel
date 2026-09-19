// @vitest-environment jsdom
import { act, useState } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, expect, it } from 'vitest'
import { NavPager, NavWrapToast } from '@/components/NavPager'
import { navItemKey, type NavItem } from './listNav'
import { useListNav } from './useListNav'

// 用一个最小宿主组件覆盖「顶栏控件 + 键盘 + 循环」这条共享路径:
// 三处弹窗 (个股详情 / 交易回放 / 标的回放) 都只是换 items 与文案, 交互逻辑同源。
function Harness({
  items,
  initial,
  onNavigate,
}: {
  items: NavItem[]
  initial: string
  onNavigate?: (item: NavItem) => void
}) {
  const [current, setCurrent] = useState(initial)
  const nav = useListNav<NavItem>({
    items,
    keyOf: navItemKey,
    currentKey: current,
    onNavigate: item => { onNavigate?.(item); setCurrent(item.symbol) },
    wrapHints: { head: '已到榜首', tail: '已到末尾' },
  })
  return (
    <div>
      <NavPager nav={nav} prevLabel="上一只" nextLabel="下一只" />
      <NavWrapToast message={nav.wrapMsg} />
      <span data-cur>{current}</span>
    </div>
  )
}

const NAV: NavItem[] = [
  { symbol: '600000', name: '浦发银行' },
  { symbol: '000001', name: '平安银行' },
  { symbol: '300750', name: '宁德时代' },
]

let host: HTMLDivElement
let root: Root

beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  host = document.createElement('div')
  document.body.appendChild(host)
  root = createRoot(host)
})

afterEach(async () => {
  await act(async () => root.unmount())
  host.remove()
})

const cur = () => host.querySelector('[data-cur]')?.textContent
const pagerText = () => host.textContent?.replace(/600000|000001|300750|浦发银行|平安银行|宁德时代/g, '')
const button = (label: string) => host.querySelector<HTMLButtonElement>(`[aria-label="${label}"]`)

// key 递增: 每次 render 等价于用新的候选列表重新打开一次弹窗, 不复用上一次的选中状态
let mountSeq = 0
async function render(items: NavItem[], initial = items[0]?.symbol ?? '', onNavigate?: (i: NavItem) => void) {
  mountSeq += 1
  await act(async () => root.render(<Harness key={mountSeq} items={items} initial={initial} onNavigate={onNavigate} />))
}

async function click(label: string) {
  const el = button(label)
  expect(el).not.toBeNull()
  await act(async () => el!.click())
}

// 与浏览器一致: 键盘事件落在聚焦元素上 (无聚焦时为 body), 再由冒泡到 document 的监听器处理
async function press(key: string, target: EventTarget = document.body) {
  await act(async () => {
    target.dispatchEvent(new KeyboardEvent('keydown', { key, bubbles: true, cancelable: true }))
  })
}

it('顶栏计数与按钮按列表顺序切项', async () => {
  await render(NAV)
  expect(pagerText()).toContain('1 / 3')
  await click('下一只')
  expect(cur()).toBe('000001')
  expect(pagerText()).toContain('2 / 3')
  await click('上一只')
  expect(cur()).toBe('600000')
})

it('首↔尾循环: 末项前进回首项、首项后退到末项, 并给出弱提示', async () => {
  await render(NAV, '300750')
  expect(pagerText()).toContain('3 / 3')
  await click('下一只')
  expect(cur()).toBe('600000')
  expect(pagerText()).toContain('已到榜首')

  await click('上一只')
  expect(cur()).toBe('300750')
  expect(pagerText()).toContain('已到末尾')
})

it('左右方向键切项', async () => {
  await render(NAV)
  await press('ArrowRight')
  expect(cur()).toBe('000001')
  await press('ArrowLeft')
  expect(cur()).toBe('600000')
  // 非方向键不触发切项
  await press('a')
  expect(cur()).toBe('600000')
})

it('焦点在输入框时方向键让位给光标, 不切项', async () => {
  await render(NAV)
  const input = document.createElement('input')
  document.body.appendChild(input)
  await press('ArrowRight', input)
  expect(cur()).toBe('600000')
  input.remove()
})

it('同一标的重复出现时按 key 去重, 计数不虚高', async () => {
  await render([NAV[0], NAV[1], NAV[0]])
  expect(pagerText()).toContain('1 / 2')
  await click('下一只')
  expect(cur()).toBe('000001')
})

it('列表不足两项或当前项不在列表时不渲染切项控件', async () => {
  await render([NAV[0]])
  expect(button('下一只')).toBeNull()

  await render(NAV, '999999')
  expect(button('下一只')).toBeNull()
  // 不可用时方向键也不切项
  await press('ArrowRight')
  expect(cur()).toBe('999999')
})

it('切项回调带出目标 symbol 与 name', async () => {
  const seen: NavItem[] = []
  await render(NAV, '600000', item => seen.push(item))
  await click('下一只')
  expect(seen).toEqual([{ symbol: '000001', name: '平安银行' }])
})

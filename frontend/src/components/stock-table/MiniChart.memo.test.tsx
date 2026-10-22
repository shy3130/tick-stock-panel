// @vitest-environment jsdom
// 迷你图 React.memo 契约验证。
//
// memo 拦截本身无法从外部观测 (Profiler.onRender 统计的是 Profiler 子树 commit,
// memo 拦截时它照样触发; 重渲染输出不变时 DOM 也零变更), 因此这里断言的是可验证
// 的契约面: ① 两个组件必须保持 memo 类型 (Symbol.for('react.memo')) — 防止将来
// 重构改回普通函数组件, 实时行情每 tick 全量重渲染的退化静默复发; ② memo 包裹后
// 挂载/渲染行为不变。memo 生效的前提 (rows 引用由上游 reuseUnchangedSeries 收敛)
// 由 lib/series-identity.test.ts 覆盖。
import { act } from 'react'
import { createRoot } from 'react-dom/client'
import { afterEach, expect, it } from 'vitest'
import { MiniIntraday } from './MiniIntraday'
import { MiniCandlestick } from './MiniCandlestick'
import type { KlineRow, MinuteKlineRow } from '@/lib/api'

function mkMinuteRows(closes: number[]): MinuteKlineRow[] {
  return closes.map((c, i) => ({
    datetime: `2026-10-21 09:${String(31 + i).padStart(2, '0')}:00`,
    open: c, high: c + 0.1, low: c - 0.1, close: c, volume: 100, amount: 1000,
  }))
}

function mkKlineRows(closes: number[]): KlineRow[] {
  return closes.map((c, i) => ({
    date: `2026-10-${String(1 + i).padStart(2, '0')}`,
    open: c, high: c + 0.5, low: c - 0.5, close: c, volume: 1000,
  }))
}

const MEMO_TYPE = Symbol.for('react.memo')

let cleanup = async () => {}
afterEach(async () => { await cleanup() })

function render(node: React.ReactNode) {
  const host = document.createElement('div')
  const root = createRoot(host)
  act(() => { root.render(node) })
  cleanup = async () => { await act(async () => root.unmount()) }
  return host
}

it('MiniIntraday / MiniCandlestick 挂了 React.memo (渲染热路径契约, 防退化)', () => {
  expect((MiniIntraday as any).$$typeof).toBe(MEMO_TYPE)
  expect((MiniCandlestick as any).$$typeof).toBe(MEMO_TYPE)
})

it('MiniIntraday: memo 包裹后渲染行为不变 (折线 + 均线 + 渐变面)', () => {
  const host = render(<MiniIntraday rows={mkMinuteRows([10, 10.2, 10.1, 10.3, 10.2])} width={100} height={56} />)
  expect(host.querySelectorAll('polyline').length).toBe(2)
  expect(host.querySelectorAll('polygon').length).toBe(1)
})

it('MiniCandlestick: memo 包裹后渲染行为不变 (蜡烛实体 + 影线)', () => {
  const host = render(<MiniCandlestick rows={mkKlineRows([10, 10.5, 10.3, 10.8, 10.6])} width={100} height={80} />)
  expect(host.querySelectorAll('rect').length).toBe(5)
  expect(host.querySelectorAll('line').length).toBe(5)
})

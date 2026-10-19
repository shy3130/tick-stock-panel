// @vitest-environment jsdom
import { act } from 'react'
import { createRoot } from 'react-dom/client'
import { afterEach, expect, it } from 'vitest'
import { MiniIntraday } from './MiniIntraday'
import type { MinuteKlineRow } from '@/lib/api'

const BULL = '#C74040'
const BEAR = '#2D9B65'
const FLAT = '#A1A1AA'

/** n 根递增/递减/平的分钟K, close 从 start 开始每根 ±step */
function mkRows(n: number, start: number, step: number): MinuteKlineRow[] {
  return Array.from({ length: n }, (_, i) => ({
    datetime: `2026-10-19 09:${30 + i}:00`,
    open: start, high: start, low: start,
    close: start + step * i,
    volume: 100, amount: 1000,
  }))
}

let cleanup = async () => {}
afterEach(async () => { await cleanup() })

function renderAndGetPriceStroke(props: Parameters<typeof MiniIntraday>[0]): string | null {
  const host = document.createElement('div')
  const root = createRoot(host)
  cleanup = async () => { await act(async () => root.unmount()) }
  act(() => { root.render(<MiniIntraday {...props} />) })
  // 最后一条 polyline 是价格折线 (前面是均价线, 均线恒为暖黄)
  const lines = Array.from(host.querySelectorAll('polyline'))
  return lines[lines.length - 1]?.getAttribute('stroke')
}

it('线过昨收基准线涂红 — 即便冻结的 changePct 为负 (盘中穿越昨收场景)', () => {
  // 3 根 close 6.90→7.00→7.03, 昨收 6.97: 线在基准线上方 → 红;
  // 行数据快照 changePct=-0.043 (002469.SZ 2026-10-19 案例) 不得再抢占着色
  const stroke = renderAndGetPriceStroke({ rows: [6.9, 7.0, 7.03].map((c, i) => ({ ...mkRows(1, c, 0)[0], close: c, datetime: `09:3${i}` })), prevClose: 6.97, changePct: -0.043 })
  expect(stroke).toBe(BULL)
})

it('线在昨收基准线下方涂绿 — 即便冻结的 changePct 为正', () => {
  const stroke = renderAndGetPriceStroke({ rows: mkRows(3, 7.0, -0.05), prevClose: 6.97, changePct: 0.05 })
  expect(stroke).toBe(BEAR)
})

it('正好压在基准线上涂灰 (与详情图三态一致)', () => {
  const stroke = renderAndGetPriceStroke({ rows: mkRows(3, 6.97, 0), prevClose: 6.97 })
  expect(stroke).toBe(FLAT)
})

it('无 prevClose 时基准线由 changePct 反算, 着色仍看线的位置', () => {
  // close=10.3, changePct=+3% → 基准 = 10.3/1.03 = 10.0, 线在上方 → 红
  const rows = mkRows(3, 10.1, 0.1) // 10.1, 10.2, 10.3
  const stroke = renderAndGetPriceStroke({ rows, changePct: 0.03 })
  expect(stroke).toBe(BULL)
})

it('数据不足两根时渲染占位空 SVG', () => {
  const host = document.createElement('div')
  const root = createRoot(host)
  cleanup = async () => { await act(async () => root.unmount()) }
  act(() => { root.render(<MiniIntraday rows={[]} prevClose={6.97} />) })
  expect(host.querySelectorAll('polyline')).toHaveLength(0)
  expect(host.querySelector('svg')).not.toBeNull()
})

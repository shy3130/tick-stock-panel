// @vitest-environment jsdom
import { act } from 'react'
import { createRoot } from 'react-dom/client'
import { afterEach, expect, it } from 'vitest'
import { MiniIntraday } from './MiniIntraday'
import type { MinuteKlineRow } from '@/lib/api'

const BULL = '#C74040'
const BEAR = '#2D9B65'
const GOLD = '#E0B84A'
const BG = '#7A7A85'

function mkRows(closes: number[], volume = 100): MinuteKlineRow[] {
  return closes.map((c, i) => ({
    datetime: `2026-10-19 09:${String(30 + i).padStart(2, '0')}:00`,
    open: c, high: c, low: c, close: c, volume, amount: 1000,
  }))
}

let cleanup = async () => {}
afterEach(async () => { await cleanup() })

function render(props: Parameters<typeof MiniIntraday>[0]) {
  const host = document.createElement('div')
  const root = createRoot(host)
  cleanup = async () => { await act(async () => root.unmount()) }
  act(() => { root.render(<MiniIntraday {...props} />) })
  return host
}

/** 分钟成交量柱: 红/绿 fill + opacity 0.4 (竞价未匹配柱同为红绿但 opacity 0.55,
 *  无竞价栏时也不能用 x 筛 — auctionW=0, 一律按 fill+opacity 直筛) */
function minuteBars(host: HTMLElement) {
  return Array.from(host.querySelectorAll('rect'))
    .filter(r => [BULL, BEAR].includes(r.getAttribute('fill') ?? ''))
    .filter(r => r.getAttribute('opacity') === '0.4')
}

/** 价格区/量带分隔线: stroke #7A7A85 + opacity 0.25 的水平线 (竖直的是栏分隔线) */
function bandSeparator(host: HTMLElement) {
  return Array.from(host.querySelectorAll('line'))
    .filter(l => l.getAttribute('stroke') === BG && l.getAttribute('opacity') === '0.25')
    .filter(l => l.getAttribute('y1') === l.getAttribute('y2'))
}

/** 最后一条 polyline 是连续竞价价格线 */
function priceYs(host: HTMLElement): number[] {
  const lines = Array.from(host.querySelectorAll('polyline'))
  return (lines[lines.length - 1].getAttribute('points') ?? '')
    .trim().split(/\s+/).map(p => Number(p.split(',')[1]))
}

/** 主体量带几何 (与组件同式): volActive 时价格区止于 ~64%, 量带独立于 [volTop, H-3] */
function bandGeom(H: number) {
  const padY = 3
  const volTop = Math.round(H * 0.64) + 2
  return { priceBottom: Math.round(H * 0.64), volTop, volBandH: (H - padY) - volTop, bottomY: H - padY }
}

it('volume 开+无竞价: 分钟量柱 (opacity 0.4) + 分隔线 y=52, 无窄栏底色无金柱', () => {
  const host = render({ rows: mkRows([10.0, 10.1]), prevClose: 10, volume: true, width: 100, height: 80 })
  expect(minuteBars(host).length).toBeGreaterThanOrEqual(1)
  const seps = bandSeparator(host)
  expect(seps).toHaveLength(1)
  expect(Number(seps[0].getAttribute('y1'))).toBe(52)
  expect(host.querySelectorAll(`rect[fill="${BG}"]`)).toHaveLength(0)
  expect(host.querySelectorAll(`rect[fill="${GOLD}"]`)).toHaveLength(0)
})

it('volume 开+无竞价: 价格折线全部 y ≤ priceBottom=51 (分区生效)', () => {
  const host = render({ rows: mkRows([10.0, 10.1]), prevClose: 10, volume: true, width: 100, height: 80 })
  const ys = priceYs(host)
  expect(ys.length).toBe(2)
  for (const y of ys) expect(y).toBeLessThanOrEqual(51)
})

it('volume 默认关 (回归锚): 无量柱无分隔线, 价格线可达底边 (历史全高形态)', () => {
  const host = render({ rows: mkRows([10.1, 10.0]), prevClose: 10, width: 100, height: 80 })
  expect(minuteBars(host)).toHaveLength(0)
  expect(bandSeparator(host)).toHaveLength(0)
  const ys = priceYs(host)
  expect(Math.max(...ys)).toBeCloseTo(77, 5)
})

it('volume 开+竞价量: 金柱 + 竞价红绿柱 (0.55) + 分钟柱 (0.4) 三者共存', () => {
  const host = render({
    rows: mkRows([10.0, 10.1]),
    prevClose: 10,
    volume: true,
    width: 100,
    height: 80,
    auction: {
      price: [['09:15:00', 10.0], ['09:25:00', 10.1]],
      match: [['09:15:00', 100], ['09:25:00', 500]],
      unmatched: [['09:15:00', 300], ['09:25:00', -200]],
    },
  })
  expect(host.querySelectorAll(`rect[fill="${GOLD}"]`).length).toBeGreaterThanOrEqual(1)
  const auctionRg = Array.from(host.querySelectorAll('rect'))
    .filter(r => [BULL, BEAR].includes(r.getAttribute('fill') ?? '') && r.getAttribute('opacity') === '0.55')
  expect(auctionRg.length).toBeGreaterThanOrEqual(1)
  expect(minuteBars(host).length).toBeGreaterThanOrEqual(1)
})

it('volume 开+竞价只配价格: 窄栏底色存在 + 分钟柱存在, 金柱 0', () => {
  const host = render({
    rows: mkRows([10.0, 10.1]),
    prevClose: 10,
    volume: true,
    width: 100,
    height: 80,
    auction: { price: [['09:15:00', 10.0], ['09:25:00', 10.1]] },
  })
  expect(host.querySelectorAll(`rect[fill="${BG}"]`)).toHaveLength(1)
  expect(host.querySelectorAll(`rect[fill="${GOLD}"]`)).toHaveLength(0)
  expect(minuteBars(host).length).toBeGreaterThanOrEqual(1)
})

it('volume 开+rows 全部 volume=0: 不画空带 (无分隔线无量柱), 价格线可达 H-3', () => {
  const host = render({ rows: mkRows([10.1, 10.0], 0), prevClose: 10, volume: true, width: 100, height: 80 })
  expect(host.querySelectorAll('rect')).toHaveLength(0)
  expect(bandSeparator(host)).toHaveLength(0)
  expect(Math.max(...priceYs(host))).toBeCloseTo(77, 5)
})

it('盘前 AuctionOnly + volume 开 + 无量字段: 整幅价格, 不画空带 (R6)', () => {
  const host = render({
    rows: [],
    prevClose: 10,
    volume: true,
    width: 100,
    height: 80,
    auction: { price: [['09:15:00', 9.9], ['09:20:00', 10.05], ['09:25:00', 10.1]] },
  })
  expect(host.querySelector('svg')?.getAttribute('aria-label')).toBe('集合竞价')
  expect(bandSeparator(host)).toHaveLength(0)
  expect(host.querySelectorAll('rect')).toHaveLength(0)
})

it('盘前 AuctionOnly + 竞价量数据 + volume 未传: 量带照画 (R2 渲染侧保障)', () => {
  const host = render({
    rows: [],
    prevClose: 10,
    width: 100,
    height: 80,
    auction: {
      price: [['09:15:00', 9.9], ['09:20:00', 10.05], ['09:25:00', 10.1]],
      match: [['09:15:00', 100], ['09:25:00', 500]],
    },
  })
  expect(host.querySelector('svg')?.getAttribute('aria-label')).toBe('集合竞价')
  expect(bandSeparator(host)).toHaveLength(1)
  expect(host.querySelectorAll(`rect[fill="${GOLD}"]`).length).toBeGreaterThanOrEqual(1)
})

it('yMode=limit 与 volume 开正交共存: 价格线 y ∈ [3, priceBottom], 量柱在带内 (R6)', () => {
  const host = render({
    rows: mkRows([10.0, 10.05, 10.1]),
    prevClose: 10,
    volume: true,
    yMode: 'limit',
    limitRate: 0.1,
    width: 100,
    height: 80,
  })
  const ys = priceYs(host)
  for (const y of ys) {
    expect(y).toBeGreaterThanOrEqual(3)
    expect(y).toBeLessThanOrEqual(51)
  }
  expect(minuteBars(host).length).toBeGreaterThanOrEqual(1)
  expect(bandSeparator(host)).toHaveLength(1)
})

it('行高 60: 量带 volBandH=17 (≥15 可读下限), 柱高 ≤17 (R6)', () => {
  const host = render({ rows: mkRows([10.0, 10.1, 10.0, 10.1]), prevClose: 10, volume: true, width: 100, height: 60 })
  const { volTop, volBandH } = bandGeom(60)
  expect(volTop).toBe(40)
  expect(volBandH).toBe(17)
  expect(volBandH).toBeGreaterThanOrEqual(15)
  const bars = minuteBars(host)
  expect(bars.length).toBeGreaterThanOrEqual(1)
  for (const b of bars) {
    const h = Number(b.getAttribute('height'))
    expect(h).toBeLessThanOrEqual(17)
    expect(h).toBeGreaterThan(0)
  }
})

it('强制联动渲染侧: 竞价量数据 + volume=false → 量带仍显示', () => {
  const host = render({
    rows: mkRows([10.0, 10.1]),
    prevClose: 10,
    volume: false,
    width: 100,
    height: 80,
    auction: {
      price: [['09:15:00', 10.0], ['09:25:00', 10.1]],
      match: [['09:15:00', 100], ['09:25:00', 500]],
    },
  })
  expect(bandSeparator(host)).toHaveLength(1)
  expect(host.querySelectorAll(`rect[fill="${GOLD}"]`).length).toBeGreaterThanOrEqual(1)
})

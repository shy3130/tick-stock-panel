// @vitest-environment jsdom
import { act } from 'react'
import { createRoot } from 'react-dom/client'
import { afterEach, expect, it } from 'vitest'
import { MiniCandlestick } from './MiniCandlestick'
import type { KlineRow } from '@/lib/api'

const BULL = '#C74040'
const BEAR = '#2D9B65'
const NEUTRAL = '#A1A1AA'
const BG = '#7A7A85'

/** [open, high, low, close, volume?] — volume 缺省表示该 K 无量数据 */
function mkKlines(vals: Array<[number, number, number, number, number?]>): KlineRow[] {
  return vals.map(([o, h, l, c, v], i) => ({
    date: `2026-10-${String(i + 1).padStart(2, '0')}`,
    open: o, high: h, low: l, close: c, ...(v != null ? { volume: v } : {}),
  }))
}

/** 两根 K (阳线+阴线): hi=10.2 lo=9.8 → range=0.4; vmax=200 */
const TWO = mkKlines([
  [9.9, 10.1, 9.8, 10.0, 100],
  [10.1, 10.2, 9.9, 10.0, 200],
])

let cleanup = async () => {}
afterEach(async () => { await cleanup() })

function render(props: Parameters<typeof MiniCandlestick>[0]) {
  const host = document.createElement('div')
  const root = createRoot(host)
  cleanup = async () => { await act(async () => root.unmount()) }
  act(() => { root.render(<MiniCandlestick {...props} />) })
  return host
}

/** 日k量柱: opacity 0.4 的 rect (蜡烛实体 rect 无 opacity 属性) */
function volBars(host: HTMLElement) {
  return Array.from(host.querySelectorAll('rect'))
    .filter(r => r.getAttribute('opacity') === '0.4')
}

/** 价格区/量带分隔线: stroke #7A7A85 + opacity 0.25 的水平线 */
function bandSeparator(host: HTMLElement) {
  return Array.from(host.querySelectorAll('line'))
    .filter(l => l.getAttribute('stroke') === BG && l.getAttribute('opacity') === '0.25')
    .filter(l => l.getAttribute('y1') === l.getAttribute('y2'))
}

/** 蜡烛影线 (stroke 为涨跌色; 分隔线是灰色) */
function wicks(host: HTMLElement) {
  return Array.from(host.querySelectorAll('line'))
    .filter(l => [BULL, BEAR, NEUTRAL].includes(l.getAttribute('stroke') ?? ''))
}

it('volume 默认关 (回归锚): 无量柱无分隔线, 最低影线触底 y=H-2=78 (历史全高形态逐像素一致)', () => {
  const host = render({ rows: TWO, width: 100, height: 80 })
  expect(volBars(host)).toHaveLength(0)
  expect(bandSeparator(host)).toHaveLength(0)
  const w = wicks(host)
  expect(w).toHaveLength(2)
  // 原 yScale: padY=2 + (1-(v-lo)/range)*(H-2*padY) → 最低价 9.8 → 78, 最高价 10.2 → 2
  expect(Math.max(...w.map(l => Number(l.getAttribute('y2'))))).toBe(78)
  expect(Math.min(...w.map(l => Number(l.getAttribute('y1'))))).toBe(2)
})

it('volume 开 (H=80): 分隔线 y=52, 价格区止于 51, 量柱在带内按窗口最大量归一', () => {
  const host = render({ rows: TWO, width: 100, height: 80, volume: true })
  const seps = bandSeparator(host)
  expect(seps).toHaveLength(1)
  expect(Number(seps[0].getAttribute('y1'))).toBe(52)
  // 价格区止于 priceBottom=round(80*0.64)=51: 影线不得进入量带
  for (const l of wicks(host)) {
    expect(Number(l.getAttribute('y1'))).toBeLessThanOrEqual(51)
    expect(Number(l.getAttribute('y2'))).toBeLessThanOrEqual(51)
  }
  const bars = volBars(host)
  expect(bars).toHaveLength(2)
  // volBandH = (80-2) - (51+2) = 25; vmax=200 → 高度 100→12.5 / 200→25
  expect(Number(bars[0].getAttribute('height'))).toBeCloseTo(12.5, 5)
  expect(Number(bars[1].getAttribute('height'))).toBeCloseTo(25, 5)
  for (const b of bars) {
    expect(Number(b.getAttribute('y')) + Number(b.getAttribute('height'))).toBeCloseTo(78, 5)
    expect(Number(b.getAttribute('y'))).toBeGreaterThanOrEqual(53) // ≥ volTop
  }
  // 颜色随蜡烛实体: 阳线红 / 阴线绿; 宽度与实体同款 bodyW=max(barW*0.55,2)=27.5
  expect(bars[0].getAttribute('fill')).toBe(BULL)
  expect(bars[1].getAttribute('fill')).toBe(BEAR)
  expect(Number(bars[0].getAttribute('width'))).toBeCloseTo(27.5, 5)
})

it('H=44: volBandH=12, 分隔线 y=29', () => {
  const host = render({ rows: TWO, width: 100, height: 44, volume: true })
  const seps = bandSeparator(host)
  expect(seps).toHaveLength(1)
  expect(Number(seps[0].getAttribute('y1'))).toBe(29)
  expect(volBars(host)).toHaveLength(2)
})

it('H=40 volume=true: 带宽 10px 仍画带 (阈值 3px, 与 MiniIntraday 行为一致)', () => {
  // 曾用 volBandH>=12 守卫: 同样开量带、同样 40px 时 日K不画而分时画, 用户看到
  // "开关开了但日k没量柱"。阈值降到 3px 后两者一致 (分时 H=32 时带宽 3.5 也画)。
  const host = render({ rows: TWO, width: 100, height: 40, volume: true })
  expect(volBars(host)).toHaveLength(2)
  expect(bandSeparator(host)).toHaveLength(1)
  // 价格区止于 round(40*0.64)=26, 影线不进入量带
  for (const l of wicks(host)) {
    expect(Number(l.getAttribute('y2'))).toBeLessThanOrEqual(26)
  }
})

it('H=32 (最小展开高度): 带宽 3.5px 仍画带, 与分时同高度一致', () => {
  const host = render({ rows: TWO, width: 100, height: 32, volume: true })
  expect(volBars(host)).toHaveLength(2)
  expect(bandSeparator(host)).toHaveLength(1)
})

it('极小高度: 量柱高度不为负 (几何安全)', () => {
  const host = render({ rows: TWO, width: 100, height: 20, volume: true })
  for (const b of volBars(host)) {
    expect(Number(b.getAttribute('height'))).toBeGreaterThanOrEqual(0)
  }
})

it('volume 开但 rows 全部无量 (缺失或 0): 不画空带, 价格回全幅', () => {
  const rows = mkKlines([[9.9, 10.1, 9.8, 10.0], [10.1, 10.2, 9.9, 10.0, 0]])
  const host = render({ rows, width: 100, height: 80, volume: true })
  expect(volBars(host)).toHaveLength(0)
  expect(bandSeparator(host)).toHaveLength(0)
  expect(Math.max(...wicks(host).map(l => Number(l.getAttribute('y2'))))).toBe(78)
})

it('部分 K 无量: 只为有量的 K 画柱 (无量 K 不画 0 高柱)', () => {
  const rows = mkKlines([
    [9.9, 10.1, 9.8, 10.0, 100],
    [10.1, 10.2, 9.9, 10.0],
    [9.95, 10.05, 9.9, 10.02, 300],
  ])
  const host = render({ rows, width: 120, height: 80, volume: true })
  expect(volBars(host)).toHaveLength(2)
})

it('量柱颜色含一字板前收规则: 平盘 K (open===close) 按前日收盘定色', () => {
  const rows = mkKlines([
    [9.9, 10.0, 9.8, 10.0, 100],  // 阳线
    [10.0, 10.1, 10.0, 10.0, 50], // 平盘且等于前收 → NEUTRAL
    [10.1, 10.2, 10.1, 10.1, 80], // 一字板高于前收 → BULL
  ])
  const host = render({ rows, width: 120, height: 80, volume: true })
  expect(volBars(host).map(b => b.getAttribute('fill'))).toEqual([BULL, NEUTRAL, BULL])
})

it('空数据: 等尺寸占位 svg (aria-label=加载中), 不画内容不预留量带', () => {
  const host = render({ rows: [], width: 100, height: 80, volume: true })
  const svg = host.querySelector('svg')
  expect(svg?.getAttribute('aria-label')).toBe('加载中')
  expect(svg?.getAttribute('width')).toBe('100')
  expect(svg?.getAttribute('height')).toBe('80')
  expect(host.querySelectorAll('rect').length + host.querySelectorAll('line').length).toBe(0)
})

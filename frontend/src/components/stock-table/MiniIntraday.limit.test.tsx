// @vitest-environment jsdom
import { act } from 'react'
import { createRoot } from 'react-dom/client'
import { afterEach, expect, it } from 'vitest'
import { MiniIntraday } from './MiniIntraday'
import { boardLimitRate } from '@/lib/price-limit'
import type { MinuteKlineRow } from '@/lib/api'

function mkRows(closes: number[]): MinuteKlineRow[] {
  return closes.map((c, i) => ({
    datetime: `2026-10-19 09:${String(30 + i).padStart(2, '0')}:00`,
    open: c, high: c, low: c, close: c, volume: 100, amount: 1000,
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

/** 折线在 SVG 内的 y 坐标集合 */
function priceYs(host: HTMLElement): number[] {
  const lines = Array.from(host.querySelectorAll('polyline'))
  const price = lines[lines.length - 1]
  return (price?.getAttribute('points') ?? '')
    .trim().split(/\s+/)
    .map(p => Number(p.split(',')[1]))
}

it('涨跌停幅度: 主板 10% / 创业板科创板 20% / 北交所 30%', () => {
  expect(boardLimitRate('600000.SH')).toBeCloseTo(0.10)
  expect(boardLimitRate('000001.SZ')).toBeCloseTo(0.10)
  expect(boardLimitRate('300750.SZ')).toBeCloseTo(0.20)
  expect(boardLimitRate('688981.SH')).toBeCloseTo(0.20)
  expect(boardLimitRate('830799.BJ')).toBeCloseTo(0.30)
})

it('limit 模式: 小幅波动被压向中线 (涨跌停带撑开纵轴)', () => {
  // 昨收 10, 涨跌停 ±10% → 纵轴上界 11、下界 9; close 只在 10.0~10.1 波动
  const rows = mkRows([10.0, 10.05, 10.1])
  const host = render({ rows, prevClose: 10, yMode: 'limit', limitRate: 0.1, width: 100, height: 80 })
  const ys = priceYs(host)
  // 自适应模式下 10.0 会贴近底边; limit 模式下 10.0 落在中线附近 (40)
  expect(ys[0]).toBeGreaterThan(20)
  expect(ys[0]).toBeLessThan(60)
})

it('adaptive 模式保持历史行为: 小波动铺满图高', () => {
  const rows = mkRows([10.0, 10.05, 10.1])
  const host = render({ rows, prevClose: 10, yMode: 'adaptive', width: 100, height: 80 })
  const ys = priceYs(host)
  // 数据范围即纵轴范围, 最低点贴近底边 (padY=3 → 77)
  expect(ys[0]).toBeGreaterThan(60)
})

it('limit 模式绘制涨跌停边界虚线 (自适应模式不画)', () => {
  const rows = mkRows([10.0, 10.05, 10.1])
  const withLimit = render({ rows, prevClose: 10, yMode: 'limit', limitRate: 0.1 })
  expect(withLimit.querySelectorAll('line[stroke-dasharray]').length).toBeGreaterThanOrEqual(2)
})

it('数据超出涨跌停带时区间跟随数据, 不裁剪曲线', () => {
  // 新股无涨跌幅: close 冲到 12 (+20%), 超出 ±10% 带; 折线必须仍在 0~H 内
  const rows = mkRows([10.0, 11.5, 12.0])
  const host = render({ rows, prevClose: 10, yMode: 'limit', limitRate: 0.1, height: 80 })
  const ys = priceYs(host)
  for (const y of ys) {
    expect(y).toBeGreaterThanOrEqual(0)
    expect(y).toBeLessThanOrEqual(80)
  }
})

it('limitRate 为 null (ETF/指数) 时退化为自适应, 不画涨跌停线', () => {
  const rows = mkRows([10.0, 10.05, 10.1])
  const host = render({ rows, prevClose: 10, yMode: 'limit', limitRate: null, height: 80 })
  expect(host.querySelectorAll('line[stroke-dasharray]')).toHaveLength(0)
  expect(priceYs(host).length).toBe(3)
})

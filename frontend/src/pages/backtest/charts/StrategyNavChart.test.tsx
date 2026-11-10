// @vitest-environment jsdom
// StrategyNavChart (lightweight-charts v5 双窗格): mock 'lightweight-charts' 模块,
// 断言窗格拓扑 / 序列参数 / 图例切换 / 左轴显隐 / 空数据守卫。
// mock 模式沿用 FinancialPlot.test.tsx。
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import type { AutoscaleInfo, AutoscaleInfoProvider } from 'lightweight-charts'
import type { StrategyBacktestResult } from '@/lib/api'

type Handler = (event: unknown) => void

const fake = vi.hoisted(() => {
  const calls = { addSeries: [] as Array<{ def: unknown; pane: number; options: Record<string, unknown> }>, applyOptions: [] as unknown[], priceScales: [] as Array<{ id: string; pane: number; options: Record<string, unknown> }>, removeSeries: 0, fitContent: 0 }
  const series: { setData: ReturnType<typeof vi.fn>; applyOptions: ReturnType<typeof vi.fn> }[] = []
  const handlers: Record<string, Handler[]> = { crosshair: [] }
  const setVisibleLogicalRange = vi.fn()
  const timeScale = {
    getVisibleLogicalRange: () => ({ from: 0, to: 9 }),
    setVisibleLogicalRange,
    fitContent: () => { calls.fitContent += 1 },
    subscribeVisibleLogicalRangeChange: vi.fn(),
    unsubscribeVisibleLogicalRangeChange: vi.fn(),
    subscribeSizeChange: vi.fn(),
    unsubscribeSizeChange: vi.fn(),
  }
  const chart = {
    addSeries: (def: unknown, options: Record<string, unknown>, pane: number) => {
      calls.addSeries.push({ def, pane, options })
      const s = { setData: vi.fn(), applyOptions: vi.fn(), setSeriesOrder: vi.fn() }
      series.push(s)
      return s
    },
    removeSeries: () => { calls.removeSeries += 1 },
    applyOptions: (options: unknown) => { calls.applyOptions.push(options) },
    panes: () => [{ setStretchFactor: vi.fn() }, { setStretchFactor: vi.fn() }, { setStretchFactor: vi.fn() }],
    priceScale: (id: string, pane = 0) => ({ applyOptions: (options: Record<string, unknown>) => { calls.priceScales.push({ id, pane, options }) } }),
    timeScale: () => timeScale,
    subscribeCrosshairMove: (fn: Handler) => { handlers.crosshair.push(fn) },
    unsubscribeCrosshairMove: vi.fn(),
    remove: vi.fn(),
  }
  const instances: Array<{ host: HTMLElement; remove: ReturnType<typeof vi.fn> }> = []
  const createChart = vi.fn((host: HTMLElement, _options: Record<string, unknown>) => {
    const canvas = document.createElement('canvas')
    host.appendChild(canvas)
    const remove = vi.fn(() => canvas.remove())
    instances.push({ host, remove })
    return { ...chart, remove }
  })
  return { calls, series, handlers, chart, setVisibleLogicalRange, createChart, instances }
})

vi.mock('lightweight-charts', () => ({
  createChart: fake.createChart,
  AreaSeries: 'area', BaselineSeries: 'baseline', LineSeries: 'line',
  ColorType: { Solid: 'solid' }, CrosshairMode: { Normal: 0 }, LineStyle: { Dashed: 2 }, LineType: { WithSteps: 1 },
}))
vi.mock('@/lib/theme', () => ({ useChartTheme: () => ({ text: '#ccc', grid: '#333', border: '#444', crosshair: '#888', crosshairLabelBg: '#222' }) }))

const mkResult = (p: Partial<StrategyBacktestResult> = {}): StrategyBacktestResult => ({
  run_id: 'r1',
  equity_curve: [
    { date: '2026-01-02', value: 1_000_000, exposure: 0.8 },
    { date: '2026-01-05', value: 1_050_000, exposure: 1.0 },
    { date: '2026-01-06', value: 990_000, cash: 500_000 },
    { date: '2026-01-07', value: 1_100_000, exposure: 0.5 },
  ],
  drawdown_curve: [
    { date: '2026-01-02', value: 0 },
    { date: '2026-01-05', value: -0.01 },
    { date: '2026-01-06', value: -0.05 },
    { date: '2026-01-07', value: -0.02 },
  ],
  benchmark_curve: [
    { date: '2026-01-02', close: 3200 },
    { date: '2026-01-06', close: 3250 }, // 01-05 缺口 → whitespace
    { date: '2026-01-07', close: 3260 },
  ],
  stats: {},
  ...p,
} as StrategyBacktestResult)

let host: HTMLDivElement
let root: Root | null = null

beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  fake.calls.addSeries.length = 0
  fake.calls.applyOptions.length = 0
  fake.calls.priceScales.length = 0
  fake.calls.removeSeries = 0
  fake.calls.fitContent = 0
  fake.series.length = 0
  fake.handlers.crosshair.length = 0
  fake.instances.length = 0
  vi.clearAllMocks()
  host = document.createElement('div')
  document.body.appendChild(host)
})
afterEach(() => {
  if (root) { act(() => { root!.unmount() }); root = null }
  host.remove()
})

async function render(result: StrategyBacktestResult) {
  const { StrategyNavChart } = await import('./StrategyNavChart')
  root ??= createRoot(host)
  await act(async () => { root!.render(<StrategyNavChart result={result} />) })
}

it('overlays position across the net value pane and starts drawdown at the upper edge', async () => {
  await render(mkResult())
  const defs = fake.calls.addSeries.map(call => call.def)
  const panes = fake.calls.addSeries.map(call => call.pane)
  expect(defs).toEqual(['area', 'line', 'baseline', 'baseline'])
  expect(panes).toEqual([0, 0, 0, 1])
  // 仓位走独立覆盖刻度 (不与净值共轴, 不显示轴线/末值)
  const positionCall = fake.calls.addSeries[2]
  expect(positionCall.options.priceScaleId).toBe('position')
  expect(positionCall.options.priceLineVisible).toBe(false)
  expect(positionCall.options.lineType).toBe(1)
  expect(positionCall.options.lineVisible).toBe(false)
  expect(positionCall.options.baseValue).toEqual({ type: 'price', price: 0 })
  expect(fake.createChart.mock.calls[0][1]).toMatchObject({
    rightPriceScale: { scaleMargins: { top: 0.08, bottom: 0.06 } },
    leftPriceScale: { scaleMargins: { top: 0.08, bottom: 0.06 } },
  })
  expect(fake.calls.priceScales).toContainEqual({ id: 'position', pane: 0, options: { scaleMargins: { top: 0.08, bottom: 0.06 } } })
  expect(fake.calls.priceScales).toContainEqual({ id: 'right', pane: 0, options: { scaleMargins: { top: 0.08, bottom: 0.06 } } })
  expect(fake.calls.priceScales).toContainEqual({ id: 'left', pane: 0, options: { scaleMargins: { top: 0.08, bottom: 0.06 } } })
  expect(fake.calls.priceScales).toContainEqual({ id: 'right', pane: 1, options: { entireTextOnly: true, scaleMargins: { top: 0, bottom: 0.06 } } })
  // 左轴随基准显示
  expect(fake.calls.applyOptions).toContainEqual({ leftPriceScale: { visible: true } })
})

it('aligns benchmark gaps as whitespace and derives position from cash when exposure missing', async () => {
  await render(mkResult())
  // benchmark: 4 点, 01-05 为 whitespace (只有 time)
  const benchmarkData = fake.series[1].setData.mock.calls[0][0]
  expect(benchmarkData).toHaveLength(4)
  expect(benchmarkData[1]).toEqual({ time: '2026-01-05' })
  // position: exposure*100 优先, 缺失日用 (1-cash/value)*100 推算 (990000 中 50万 现金 ≈ 49.49)
  const positionData = fake.series[2].setData.mock.calls[0][0]
  const positionValues = positionData.map((row: { value: number }) => row.value)
  expect(positionValues.slice(0, 2)).toEqual([80, 100])
  expect(positionValues[2]).toBeCloseTo(49.49, 1)
  expect(positionValues[3]).toBe(50)
  // drawdown 百分比
  const ddData = fake.series[3].setData.mock.calls[0][0]
  expect(ddData.map((row: { value: number }) => row.value)).toEqual([0, -1, -5, -2])
})

it('toggling the position legend rebuilds without the position series and keeps the view', async () => {
  await render(mkResult())
  const btn = [...host.querySelectorAll('button')].find(b => b.textContent?.includes('仓位'))!
  await act(async () => { btn.click() })
  // 重建: 净值/基准/回撤, 仓位序列移除 (回撤保持 pane 1)
  const defs = fake.calls.addSeries.slice(-3).map(call => call.def)
  const panes = fake.calls.addSeries.slice(-3).map(call => call.pane)
  expect(defs).toEqual(['area', 'line', 'baseline'])
  expect(panes).toEqual([0, 0, 1])
  // 同一 run_id: 缩放窗口保留而非 fitContent
  expect(fake.calls.fitContent).toBe(1)
  expect(fake.setVisibleLogicalRange).toHaveBeenCalled()
  expect(fake.calls.priceScales.filter(call => call.id === 'right' && call.pane === 0)).toEqual([
    { id: 'right', pane: 0, options: { scaleMargins: { top: 0.08, bottom: 0.06 } } },
    { id: 'right', pane: 0, options: { scaleMargins: { top: 0.08, bottom: 0.06 } } },
  ])
})

it('hides benchmark series and left axis when benchmark data is absent', async () => {
  await render(mkResult({ benchmark_curve: undefined }))
  const defs = fake.calls.addSeries.map(call => call.def)
  const panes = fake.calls.addSeries.map(call => call.pane)
  expect(defs).toEqual(['area', 'baseline', 'baseline'])
  expect(panes).toEqual([0, 0, 1])
  expect(fake.calls.applyOptions).toContainEqual({ leftPriceScale: { visible: false } })
})

it('renders a placeholder when equity data is too short', async () => {
  await render(mkResult({ equity_curve: [{ date: '2026-01-02', value: 1 }], drawdown_curve: [] }))
  expect(host.textContent).toContain('净值数据不足')
  expect(fake.calls.addSeries).toHaveLength(0)
})

it.each([0, 1])('renders a later result after initially receiving only %i equity points', async count => {
  const initial = mkResult()
  await render({ ...initial, equity_curve: initial.equity_curve.slice(0, count) })
  expect(host.textContent).toContain('净值数据不足')
  expect(fake.createChart).not.toHaveBeenCalled()

  const next = mkResult({ run_id: 'later-run' })
  await render(next)
  expect(host.textContent).not.toContain('净值数据不足')
  expect(host.querySelectorAll('canvas')).toHaveLength(1)
  expect(fake.instances).toHaveLength(1)
  expect(fake.instances[0].host.isConnected).toBe(true)
  expect(fake.series[0].setData).toHaveBeenLastCalledWith(next.equity_curve.map(row => ({ time: row.date, value: row.value })))
})

it('disposes the old chart across a single-day placeholder and restores a later result', async () => {
  const initial = mkResult()
  await render(initial)
  const old = fake.instances[0]
  await render({ ...initial, run_id: 'single-day', equity_curve: initial.equity_curve.slice(0, 1) })
  expect(host.textContent).toContain('净值数据不足')
  expect(old.remove).toHaveBeenCalledOnce()
  expect(host.querySelectorAll('canvas')).toHaveLength(0)

  await render(mkResult({ run_id: 'multi-day-again' }))
  expect(fake.instances).toHaveLength(2)
  expect(host.querySelectorAll('canvas')).toHaveLength(1)
  expect(fake.instances[1].host.isConnected).toBe(true)
  expect(fake.calls.fitContent).toBe(2)
  await act(async () => { root!.unmount(); root = null })
  expect(fake.instances[1].remove).toHaveBeenCalledOnce()
})

it.each([
  { label: 'zoomed negative interval', min: -7.8, max: -5, expectedMin: -7.8, expectedMax: 0 },
  { label: 'flat negative interval', min: -3, max: -3, expectedMin: -3, expectedMax: 0 },
  { label: 'zero drawdown', min: 0, max: 0, expectedMin: -1, expectedMax: 0 },
  { label: 'range crossing zero', min: -2, max: 1, expectedMin: -2, expectedMax: 1 },
])('keeps the 0% drawdown baseline in the $label range', async ({ min, max, expectedMin, expectedMax }) => {
  await render(mkResult())
  const drawdown = fake.calls.addSeries.find(call => call.def === 'baseline' && call.pane === 1)!
  const provider = drawdown.options.autoscaleInfoProvider as AutoscaleInfoProvider
  expect(provider).toBeTypeOf('function')
  const original: AutoscaleInfo = { priceRange: { minValue: min, maxValue: max }, margins: { above: 4, below: 2 } }
  const getRange = vi.fn(() => original)
  expect(provider(getRange)).toEqual({ priceRange: { minValue: expectedMin, maxValue: expectedMax }, margins: { above: 4, below: 2 } })
  expect(getRange).toHaveBeenCalledOnce()
  expect(original.priceRange).toEqual({ minValue: min, maxValue: max })
})

it('does not invent a drawdown range when the visible interval has no data', async () => {
  await render(mkResult({ drawdown_curve: [] }))
  const drawdown = fake.calls.addSeries.find(call => call.def === 'baseline' && call.pane === 1)!
  const provider = drawdown.options.autoscaleInfoProvider as AutoscaleInfoProvider
  expect(provider).toBeTypeOf('function')
  expect(provider(() => null)).toBeNull()
  const noRange: AutoscaleInfo = { priceRange: null, margins: { above: 4, below: 2 } }
  expect(provider(() => noRange)).toEqual(noRange)
})

it('fits a different run instead of reusing the previous run viewport', async () => {
  await render(mkResult())
  await render(mkResult({ run_id: 'different-run' }))
  expect(fake.calls.fitContent).toBe(2)
  expect(fake.setVisibleLogicalRange).not.toHaveBeenCalled()
  expect(fake.createChart).toHaveBeenCalledOnce()
})

it('hover readout shows per-series values at the crosshair date', async () => {
  await render(mkResult())
  await act(async () => {
    for (const handler of fake.handlers.crosshair) handler({ time: '2026-01-06' })
  })
  const readout = host.textContent!
  expect(readout).toContain('2026-01-06')
  expect(readout).toContain('990,000')
  expect(readout).toContain('3250')
  expect(readout).toContain('49%')
  expect(readout).toContain('-5.0%')
})

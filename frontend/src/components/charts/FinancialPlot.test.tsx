// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import type { ISeriesApi, SeriesType } from 'lightweight-charts'
import type { MinuteKlineRow } from '@/lib/api'
import type { ChartMarker, DailyMarkerStyle } from '@/lib/chart-types'
import { FinancialPlot, updatePlotData } from './FinancialPlot'
import { LightweightDaily } from './LightweightDaily'
import { LightweightIntraday } from './LightweightIntraday'
import { minuteTime, type PlotSeries } from '@/lib/lightweight-model'
import { CHART_LAYOUT, dailyChartHeight } from '@/lib/chart-layout'
import { ChartAnnotations } from './ChartAnnotations'

const native = vi.hoisted(() => {
  const appearance = { theme: 'dark' }
  const handlers: Record<string, (event: any) => void> = {}
  const series: any[] = []
  const stretch = vi.fn()
  const timeScale = { width: () => 500, getVisibleLogicalRange: vi.fn(() => ({ from: 0, to: 10 })),
    setVisibleLogicalRange: vi.fn(), fitContent: vi.fn(),
    subscribeSizeChange: vi.fn((cb: any) => { handlers.resize = cb }), unsubscribeSizeChange: vi.fn(),
    subscribeVisibleLogicalRangeChange: vi.fn((cb: any) => { handlers.zoom = cb }), unsubscribeVisibleLogicalRangeChange: vi.fn() }
  const chart = {
    applyOptions: vi.fn(), remove: vi.fn(), removeSeries: vi.fn(), addCustomSeries: vi.fn(), timeScale: () => timeScale,
    panes: () => Array.from({ length: 5 }, (_, pane) => ({ setStretchFactor: (value: number) => stretch(pane, value) })),
    addSeries: vi.fn((_type: any, options: any, pane: number) => {
      const priceScale = { applyOptions: vi.fn() }
      const api = { setData: vi.fn(), update: vi.fn(), applyOptions: vi.fn(), attachPrimitive: vi.fn(),
        priceToCoordinate: vi.fn((price: number) => (price - 10) * 100),
        getPane: () => ({ getHeight: () => 276 }),
        coordinateToPrice: vi.fn(() => options.visible === false ? null : 12.5), priceScale: () => priceScale, pane }
      series.push(api)
      return api
    }),
    subscribeCrosshairMove: vi.fn((cb: any) => { handlers.hover = cb }), unsubscribeCrosshairMove: vi.fn(),
    subscribeClick: vi.fn((cb: any) => { handlers.click = cb }), unsubscribeClick: vi.fn(),
    subscribeDblClick: vi.fn((cb: any) => { handlers.double = cb }), unsubscribeDblClick: vi.fn(),
  }
  const markers = { setMarkers: vi.fn(), detach: vi.fn() }
  return { appearance, handlers, series, chart, timeScale, markers, stretch, createChart: vi.fn((_host: HTMLElement, _options: unknown) => chart) }
})
vi.mock('lightweight-charts', () => ({
  createChart: native.createChart, createSeriesMarkers: () => native.markers,
  CandlestickSeries: 'Candlestick', LineSeries: 'Line', AreaSeries: 'Area', BaselineSeries: 'Baseline', HistogramSeries: 'Histogram', customSeriesDefaultOptions: {},
  ColorType: { Solid: 'solid' }, CrosshairMode: { Normal: 0 },
}))
vi.mock('@/lib/theme', () => ({ useTheme: () => native.appearance.theme, useChartTheme: () => ({ text: '#888', border: '#333', grid: '#222', crosshair: '#777', crosshairLabelBg: '#111' }) }))

let host: HTMLDivElement, root: Root
beforeEach(() => {
  vi.clearAllMocks()
  native.series.length = 0
  native.chart.addCustomSeries.mockImplementation((_view, options, pane) => native.chart.addSeries('Volume', options, pane))
  native.appearance.theme = 'dark'
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  vi.stubGlobal('requestAnimationFrame', (cb: FrameRequestCallback) => { cb(0); return 1 })
  vi.stubGlobal('cancelAnimationFrame', vi.fn())
  host = document.createElement('div'); document.body.append(host); root = createRoot(host)
})
afterEach(async () => {
  await act(async () => root.unmount())
  host.remove(); vi.unstubAllGlobals()
})
const data = [{ time: '2026-11-04', value: 10 }, { time: '2026-11-05', value: 11 }]
const model: PlotSeries[] = [{ id: 'price', type: 'Line', pane: 0, data }]

it('preserves the replay time range when opening intraday or resizing the window', async () => {
  const render = (visibleBars?: 'all') => act(async () => root.render(<FinancialPlot
    scope="trade" series={model} height={400} paneHeights={[300]} visibleBars={visibleBars} />))
  await render('all')
  expect(native.createChart.mock.calls[0][1]).toMatchObject({ timeScale: { lockVisibleTimeRangeOnResize: true } })
  await render()
  expect(native.chart.applyOptions).toHaveBeenCalledWith(expect.objectContaining({
    timeScale: expect.objectContaining({ lockVisibleTimeRangeOnResize: false }),
  }))
  expect(native.createChart).toHaveBeenCalledOnce()
})

it('fits a newly selected trade of the same symbol without resetting the viewport for indicator changes', async () => {
  const rows = [
    { date: '2026-11-04', open: 10, high: 11, low: 9, close: 10, volume: 100 },
    { date: '2026-11-05', open: 10, high: 12, low: 10, close: 11, volume: 120 },
  ]
  const render = async (start: string, activeIndicators = ['vol']) => act(async () => root.render(
    <LightweightDaily symbol="600000.SH" data={rows} visibleBars="all" activeIndicators={activeIndicators}
      ranges={[{ start, end: '2026-11-05' }]} />))
  await render('2026-11-04')
  const initialFits = native.timeScale.fitContent.mock.calls.length
  await render('2026-11-04', ['vol', 'macd'])
  expect(native.timeScale.fitContent).toHaveBeenCalledTimes(initialFits)
  await render('2026-11-05', ['vol', 'macd'])
  expect(native.timeScale.fitContent).toHaveBeenCalledTimes(initialFits + 1)
  expect(native.createChart).toHaveBeenCalledOnce()
})

it('draws bounded replay prices across the selected minute session and omits them outside the holding period', async () => {
  const updates = vi.spyOn(ChartAnnotations.prototype, 'update')
  try {
    const render = async (date: string) => act(async () => root.render(
      <LightweightIntraday symbol="600000.SH" date={date} prevClose={10}
        data={[{ datetime: `${date} 09:31:00`, open: 10, high: 11, low: 10, close: 10.5, volume: 100, amount: 105000 }]}
        priceLines={[{ value: 10.2, label: '买入价', start: '2026-11-04', end: '2026-11-05' }]} />))
    await render('2026-11-05')
    const annotations = () => updates.mock.calls.map(([value]) => value).filter(value => value.dates.length === 1).at(-1)!
    expect(annotations().priceLines).toContainEqual(expect.objectContaining({ value: 10.2, label: '买入价' }))
    const fillLine = annotations().priceLines!.find(line => line.label === '买入价')!
    expect(fillLine.start).toBeUndefined()
    expect(fillLine.end).toBeUndefined()
    await render('2026-11-06')
    expect(annotations().priceLines?.some(line => line.label === '买入价')).toBe(false)
  } finally { updates.mockRestore() }
})

it('centers index extrema on yesterday close, hides invalid averages and clears limit controls on asset switches', async () => {
  const updates = vi.spyOn(ChartAnnotations.prototype, 'update')
  try {
    const render = async (assetType: 'stock' | 'index') => act(async () => root.render(<LightweightIntraday
      symbol="000985.SH" assetType={assetType} date="2026-11-06" prevClose={5211.79}
      priceLimit={{ rate: 0.1, limit_up: 5732.97, limit_down: 4690.61, source: 'rule' }}
      data={[{ datetime: '2026-11-06 09:30:00', open: 5059.65, high: 5160.75, low: 5059.65,
        close: 5128.90, volume: 567329187, amount: 906563120224 }]} />))
    await render('stock')
    await act(async () => [...host.querySelectorAll('button')].find(button => button.textContent === '涨跌停')!.click())
    await render('index')
    expect(host.textContent).not.toContain('涨跌停')
    expect(host.textContent).not.toContain('均价')
    expect(host.textContent).not.toContain('大幅波动')
    expect(native.chart.removeSeries).toHaveBeenCalled()
    const price = native.series[1]
    const range = price.applyOptions.mock.calls.at(-1)[0].autoscaleInfoProvider().priceRange
    expect(range.minValue).toBeGreaterThan(5050)
    expect(range.minValue).toBeLessThan(5059.65)
    expect(range.maxValue).toBeGreaterThan(5160.75)
    expect((range.minValue + range.maxValue) / 2).toBeCloseTo(5211.79)
    expect(range.maxValue).toBeLessThan(5400)
    const annotations = updates.mock.calls.map(([value]) => value).filter(value => value.dates.includes('2026-11-06')).at(-1)!
    expect(annotations.priceLines?.map(line => line.value)).toEqual([5211.79])
    expect(annotations.axisTicks).toContainEqual(expect.objectContaining({ value: 5211.79, priority: 3 }))
    expect(annotations.axisTicks?.some(tick => tick.priority === 2)).toBe(false)
  } finally { updates.mockRestore() }
})

it('ETF 日K价格、均线和轴刻度为三位, 切换股票后恢复两位', async () => {
  const rows = [{ date: '2026-11-05', open: 0.751, high: 0.828, low: 0.747, close: 0.827,
    ma5: 0.801, ma10: 0.789, ma20: 0.778, ma60: 0.765, boll_upper: 0.849, boll_lower: 0.707 }]
  await act(async () => root.render(<LightweightDaily assetType="etf" data={rows} activeIndicators={['boll']} />))
  expect(host.textContent).toContain('开 0.751')
  expect(host.textContent).toContain('收 0.827')
  expect(host.textContent).toContain('MA5 0.801')
  expect(host.textContent).toContain('上轨 0.849')
  expect(native.chart.addSeries.mock.calls[0][1].priceFormat).toMatchObject({ precision: 3, minMove: 0.001 })
  await act(async () => root.render(<LightweightDaily assetType="stock" data={rows} activeIndicators={['boll']} />))
  expect(host.textContent).toContain('收 0.83')
  expect(native.series[0].applyOptions.mock.calls.at(-1)![0].priceFormat).toMatchObject({ precision: 2, minMove: 0.01 })
})

it('updates the tail incrementally but replaces corrected history and removed bars', () => {
  const api = { setData: vi.fn(), update: vi.fn() }
  const series = api as unknown as ISeriesApi<SeriesType>
  updatePlotData(series, data, [{ ...data[0] }, { ...data[1], value: 12 }, { time: '2026-11-06', value: 13 }])
  expect(api.setData).not.toHaveBeenCalled()
  expect(api.update.mock.calls.map(call => call[0].value)).toEqual([12, 13])
  updatePlotData(series, data, [{ ...data[0], value: 9 }, data[1]])
  expect(api.setData).toHaveBeenLastCalledWith([{ ...data[0], value: 9 }, data[1]])
  updatePlotData(series, data, data.slice(0, 1))
  expect(api.setData).toHaveBeenLastCalledWith(data.slice(0, 1))
})

it('reuses one chart, preserves zoom on ticks, replaces markers on symbol changes and cleans up', async () => {
  const render = async (scope: string, text: string, series = model) => act(async () => root.render(<FinancialPlot
    scope={scope} series={series} height={400} paneHeights={[300]} markers={[{ time: data[0].time, position: 'aboveBar', color: 'red', shape: 'circle', text }]} />))
  await render('A', 'A-BUY')
  const setViewCalls = native.timeScale.setVisibleLogicalRange.mock.calls.length
  await render('A', 'A-BUY', [{ ...model[0], data: [data[0], { ...data[1], value: 12 }] }])
  expect(native.createChart).toHaveBeenCalledOnce()
  expect(native.series[0].update).toHaveBeenCalledWith({ ...data[1], value: 12 })
  expect(native.series[0].attachPrimitive).toHaveBeenCalledOnce()
  expect(native.timeScale.setVisibleLogicalRange.mock.calls.length).toBeGreaterThan(setViewCalls)
  await render('B', 'B-SELL')
  native.handlers.zoom({ from: 0, to: 100 })
  expect(JSON.stringify(native.markers.setMarkers.mock.calls.at(-1))).toContain('B-SELL')
  expect(JSON.stringify(native.markers.setMarkers.mock.calls.at(-1))).not.toContain('A-BUY')
  expect(native.chart.removeSeries).toHaveBeenCalledOnce()
  native.timeScale.getVisibleLogicalRange.mockReturnValueOnce({ from: 0, to: 100 })
  native.handlers.zoom({})
  expect(native.markers.setMarkers.mock.calls.at(-1)?.[0][0]).toMatchObject({ text: '', shape: 'circle' })
  await act(async () => root.unmount())
  expect(native.chart.remove).toHaveBeenCalledOnce()
  expect(native.chart.unsubscribeCrosshairMove).toHaveBeenCalledOnce()
  root = createRoot(host)
})

it('only uses the price pane for date selection and alerts, including an invisible intraday timeline', async () => {
  const onSelect = vi.fn(), onPriceDoubleClick = vi.fn()
  await act(async () => root.render(<FinancialPlot scope="ETF" series={[
    { id: 'timeline', type: 'Line', pane: 0, visible: false, data: [{ time: data[0].time }] }, ...model,
  ]} height={400} paneHeights={[300]} currentPrice={13} onSelect={onSelect} onPriceDoubleClick={onPriceDoubleClick} />))
  for (const paneIndex of [1, undefined]) {
    native.handlers.click({ paneIndex, time: data[0].time })
    native.handlers.double({ paneIndex, point: { x: 10, y: 10 } })
  }
  native.handlers.double({ paneIndex: 0, point: { x: 520, y: 10 } })
  expect(onSelect).not.toHaveBeenCalled(); expect(onPriceDoubleClick).not.toHaveBeenCalled()
  native.handlers.click({ paneIndex: 0, time: data[0].time })
  native.handlers.double({ paneIndex: 0, point: { x: 10, y: 10 } })
  expect(onSelect).toHaveBeenCalledWith('2026-11-04')
  expect(onPriceDoubleClick).toHaveBeenCalledWith(12.5, 13)
  expect(native.series[0].coordinateToPrice).not.toHaveBeenCalled()
})

it('daily hover uses the previous close for 至今, preserves indicator panes and resets on symbol changes', async () => {
  const rows = [10, 12, 15].map((close, i) => ({ date: `2026-11-0${i + 3}`, open: close, high: close + 1, low: close - 1, close, volume: 100, rsi_6: 52 }))
  const render = async (symbol: string) => act(async () => root.render(<LightweightDaily symbol={symbol} data={rows} activeIndicators={['vol', 'rsi']} />))
  await render('A')
  await act(async () => native.handlers.hover({ time: rows[1].date }))
  expect(host.textContent).toContain('至今 +50.00% · 周期 2')
  expect(native.chart.addSeries.mock.calls.some(call => call[2] === 2)).toBe(true)
  await render('B')
  expect(host.textContent).not.toContain('至今')
})

it('shows every indicator readout at the hovered date, including MACD histogram and both volume averages', async () => {
  const updates = vi.spyOn(ChartAnnotations.prototype, 'update')
  try {
    const rows = Array.from({ length: 12 }, (_, i) => ({ date: new Date(Date.UTC(2026, 9, i + 1)).toISOString().slice(0, 10),
      open: 10, high: 12, low: 9, close: 11, volume: (i + 1) * 10_000,
      macd_dif: 0.0014, macd_dea: 0, macd_hist: i === 11 ? -0.0024 : 0.0063,
      rsi_6: 56.74, rsi_14: 48.25, rsi_24: null, kdj_k: 70.35, kdj_d: 45.28, kdj_j: 120.31,
      ma20: 10.5, boll_upper: 12.2, boll_lower: 8.8,
    }))
    const render = async () => act(async () => root.render(<LightweightDaily symbol="A" data={rows} activeIndicators={['vol', 'macd', 'rsi', 'kdj', 'boll']} />))
    const readouts = (title: string) => [...updates.mock.calls].reverse().find(([d]) => d.title === title)![0].readouts!
    await render()
    expect(readouts('MACD').map(d => d.text)).toEqual(['DIF 0.001', 'DEA 0.000', 'MACD -0.002'])
    expect(readouts('成交量').map(d => d.text)).toEqual(['量 12.00万', 'VOL5 10.00万', 'VOL10 7.50万', '量比 1.09x'])
    expect(readouts('RSI').map(d => d.text)).toEqual(['RSI6 56.7', 'RSI14 48.3', 'RSI24 —'])
    expect(readouts('KDJ').map(d => d.text)).toEqual(['K 70.3', 'D 45.3', 'J 120.3'])
    expect(host.textContent).toContain('上轨 12.20')
    expect(host.textContent).toContain('中轨 10.50')
    expect(host.textContent).toContain('下轨 8.80')
    const dif = native.chart.addSeries.mock.calls.findIndex(call => call[2] === 2 && call[0] === 'Line')
    expect(native.chart.addSeries.mock.calls[dif][1].priceFormat).toMatchObject({ precision: 3, minMove: 0.001 })
    expect(readouts('MACD')[0].color).toBe(native.chart.addSeries.mock.calls[dif][1].color)
    expect(readouts('MACD')[2].color).toBe('#2D9B65')
    await act(async () => native.handlers.hover({ time: rows[3].date, paneIndex: 2 }))
    expect(readouts('MACD')[2].text).toBe('MACD 0.006')
    expect(readouts('成交量').map(d => d.text)).toEqual(['量 4.00万', 'VOL5 —', 'VOL10 —', '量比 1.33x'])
    native.appearance.theme = 'light'
    await render()
    expect(readouts('MACD')[0].color).toBe(native.series[dif].applyOptions.mock.calls.at(-1)![0].color)
    expect(readouts('MACD')[0].color).toBe('#A16207')
  } finally { updates.mockRestore() }
})

it('preserves missing and invalid indicator values instead of showing zero or an older date', async () => {
  const updates = vi.spyOn(ChartAnnotations.prototype, 'update')
  try {
    await act(async () => root.render(<LightweightDaily symbol="A" activeIndicators={['macd', 'rsi', 'kdj']}
      data={[{ date: '2026-11-04', open: 10, high: 11, low: 9, close: 10, macd_hist: 1, rsi_6: 50 },
        { date: '2026-11-05', open: 10, high: 11, low: 9, close: 10, macd_hist: NaN, rsi_6: null, kdj_j: NaN }]} />))
    const pane = (title: string) => [...updates.mock.calls].reverse().find(([d]) => d.title === title)![0]
    expect(pane('MACD').readouts?.map(d => d.text)).toEqual(['DIF —', 'DEA —', 'MACD —'])
    expect(pane('RSI').readouts?.map(d => d.text)).toEqual(['RSI6 —', 'RSI14 —', 'RSI24 —'])
    expect(pane('KDJ').readouts?.map(d => d.text)).toEqual(['K —', 'D —', 'J —'])
    const histogram = native.chart.addSeries.mock.calls.findIndex(call => call[0] === 'Histogram')
    expect(native.series[histogram].setData.mock.calls.at(-1)![0].at(-1)).toEqual({ time: '2026-11-05' })
  } finally { updates.mockRestore() }
})

it('intraday keeps missing open blank and honors authoritative and no-limit price ranges', async () => {
  const row = { datetime: '2026-11-05 09:30:00', open: null, high: 13, low: 12, close: 12.5, volume: 10, amount: null }
  const render = async (noLimit = false) => act(async () => root.render(<LightweightIntraday symbol="ETF" date="2026-11-05" data={[row]}
    prevClose={10} priceLimit={{ rate: 0.1, source: 'instrument', limit_up: 11, limit_down: 9, no_limit: noLimit }} />))
  await render()
  expect(host.textContent).toContain('开 —')
  expect(host.textContent).toContain('均价 —')
  const limit = [...host.querySelectorAll('button')].find(b => b.textContent === '涨跌停')!
  await act(async () => limit.click())
  const price = native.series[1]
  expect(price.applyOptions.mock.calls.at(-1)[0].autoscaleInfoProvider().priceRange).toEqual({ minValue: 7, maxValue: 13 })
  await render(true)
  expect(price.applyOptions.mock.calls.at(-1)[0].autoscaleInfoProvider().priceRange).toEqual({ minValue: 7, maxValue: 13 })
  expect(limit.disabled).toBe(true)
})

it('labels adjusted daily OHLC separately from the minute price and its change', async () => {
  await act(async () => root.render(<LightweightIntraday symbol="ETF" date="2026-11-05" prevClose={10}
    data={[{ datetime: '2026-11-05 09:30:00', open: 12, high: 13, low: 12, close: 12.5, volume: 10, amount: null }]}
    dailySummary={{ date: '2026-11-05', open: 14, high: 16, low: 14, close: 15 }} />))
  expect(host.textContent).toContain('日K（前复权）')
  expect(host.textContent).toContain('收 15.00')
  expect(host.textContent).toContain('分时价 12.50 +25.00%')
})

it('labels both axes at actual limit prices and percentages in adaptive and limit modes', async () => {
  const updates = vi.spyOn(ChartAnnotations.prototype, 'update')
  try {
    for (const [base, up, down, upText, downText] of [[100, 110.01, 90.01, '+10.01%', '-9.99%'],
      [100, 109.99, 89.99, '+9.99%', '-10.01%'], [10.01, 11.01, 9.01, '+9.99%', '-9.99%'],
      [100, 120.01, 80.01, '+20.01%', '-19.99%']] as const) {
      await act(async () => root.render(<LightweightIntraday symbol="A" date="2026-11-05" prevClose={base}
        priceLimit={{ rate: 0.1, limit_up: up, limit_down: down, source: 'instrument' }}
        data={[{ datetime: '2026-11-05 09:30:00', open: base, high: up, low: down, close: up, volume: 10, amount: null }]} />))
      const verify = () => {
        const all = updates.mock.calls.map(([d]) => d)
        const left = all.filter(d => d.axisTicks?.some(t => t.text.includes('%'))).at(-1)!
        const right = all.filter(d => d.dates.includes('2026-11-05')).at(-1)!
        expect(left.axisTicks).toEqual(expect.arrayContaining([
          expect.objectContaining({ text: upText, priority: 2 }), expect.objectContaining({ text: downText, priority: 2 }),
        ]))
        expect(left.axisTicks?.some(t => t.text === '+12.00%' || t.text === '-12.00%')).toBe(false)
        expect(right.axisTicks).toEqual(expect.arrayContaining([
          expect.objectContaining({ value: up, text: up.toFixed(2), priority: 2 }),
          expect.objectContaining({ value: down, text: down.toFixed(2), priority: 2 }),
        ]))
        const price = native.series[1]
        const options = price.applyOptions.mock.calls.at(-1)?.[0] ?? native.chart.addSeries.mock.calls[1][1]
        const range = options.autoscaleInfoProvider().priceRange
        expect(range.maxValue).toBeCloseTo(base + Math.max(up - base, base - down))
        expect((range.minValue + range.maxValue) / 2).toBeCloseTo(base)
      }
      verify()
      for (const mode of ['涨跌停', '自适应']) {
        await act(async () => [...host.querySelectorAll('button')].find(b => b.textContent === mode)!.click())
        verify()
      }
    }
  } finally { updates.mockRestore() }
})

it('ETF 涨跌停兜底及左右轴保留实际千分位和对应涨跌幅', async () => {
  const updates = vi.spyOn(ChartAnnotations.prototype, 'update')
  try {
    await act(async () => root.render(<LightweightIntraday symbol="159558.SZ" assetType="etf" date="2026-11-05"
      prevClose={0.755} priceLimit={{ rate: 0.1, limit_up: null, limit_down: null, source: 'rule' }}
      data={[{ datetime: '2026-11-05 15:00:00', open: 0.775, high: 0.831, low: 0.774, close: 0.827, volume: 10, amount: null }]} />))
    const all = updates.mock.calls.map(([d]) => d)
    const right = all.filter(d => d.dates.includes('2026-11-05')).at(-1)!
    expect(right.axisTicks).toEqual(expect.arrayContaining([
      expect.objectContaining({ value: 0.831, text: '0.831', priority: 2 }),
      expect.objectContaining({ value: 0.680, text: '0.680', priority: 2 }),
    ]))
    const left = all.filter(d => d.axisTicks?.some(t => t.text.includes('%'))).at(-1)!
    expect(left.axisTicks).toEqual(expect.arrayContaining([
      expect.objectContaining({ text: '+10.07%', priority: 2 }), expect.objectContaining({ text: '-9.93%', priority: 2 }),
    ]))
    expect(host.textContent).toContain('收 0.827')
  } finally { updates.mockRestore() }
})

it('uses single-session limit ticks without drawing disabled limit lines and clears them on no-limit days', async () => {
  const updates = vi.spyOn(ChartAnnotations.prototype, 'update')
  try {
    const render = async (noLimit = false) => act(async () => root.render(<LightweightIntraday symbol="A" showLimitLines={false}
      priceLimit={{ rate: 0.1, limit_up: 110.01, limit_down: 90.01, no_limit: noLimit, source: 'instrument' }}
      sessions={[{ date: '2026-11-05', prev_close: 100, rows: [{ datetime: '2026-11-05 09:30:00', open: 100, high: 110.01, low: 100, close: 110.01, volume: 10, amount: null }] }]} />))
    await render()
    const right = () => updates.mock.calls.map(([d]) => d).filter(d => d.dates.includes('2026-11-05')).at(-1)!
    expect(right().axisTicks?.some(t => t.value === 110.01 && t.priority === 2)).toBe(true)
    expect(right().priceLines?.some(line => line.value === 110.01 || line.value === 90.01)).toBe(false)
    await render(true)
    expect(right().axisTicks?.some(t => t.priority === 2)).toBe(false)
  } finally { updates.mockRestore() }
})

it('aligns daily and minute price/volume panes using the same geometry, with attribution outside the canvas', async () => {
  const height = dailyChartHeight(420, 1)
  await act(async () => root.render(<LightweightDaily symbol="ETF" height={height - CHART_LAYOUT.toolbar}
    activeIndicators={['vol']} data={[{ date: '2026-11-05', open: 10, high: 12, low: 9, close: 11, volume: 10 }]} />))
  const dailyPlotHeight = (host.querySelector('[data-chart-engine]') as HTMLElement).style.height
  const dailyPanes = native.stretch.mock.calls.slice()
  native.stretch.mockClear()
  await act(async () => root.render(<LightweightIntraday symbol="ETF" height={height} date="2026-11-05" prevClose={10}
    data={[{ datetime: '2026-11-05 09:30:00', open: 10, high: 12, low: 9, close: 11, volume: 10, amount: 11000 }]} />))
  expect((host.querySelector('[data-chart-engine]') as HTMLElement).style.height).toBe(dailyPlotHeight)
  expect(native.stretch.mock.calls).toEqual(dailyPanes)
  expect(native.createChart.mock.calls.at(-1)?.[1]).toMatchObject({
    layout: { attributionLogo: false }, timeScale: { minimumHeight: CHART_LAYOUT.timeAxis },
  })
})

it('bounds daily scrolling at the latest bar while keeping the intraday session timeline', async () => {
  const render = async (intraday: boolean) => act(async () => root.render(<FinancialPlot
    scope={intraday ? 'minute' : 'daily'} series={model} height={400} paneHeights={[300]} intraday={intraday} />))
  await render(false)
  expect(native.createChart.mock.calls[0]?.[1]).toMatchObject({ timeScale: { fixRightEdge: true, rightOffset: 0 } })
  expect(native.timeScale.setVisibleLogicalRange).toHaveBeenLastCalledWith({ from: 0, to: 1 })
  await render(true)
  expect(native.chart.applyOptions).toHaveBeenCalledWith(expect.objectContaining({ timeScale: expect.objectContaining({ timeVisible: true, fixRightEdge: false, rightOffset: 0 }) }))
  expect(native.timeScale.fitContent).toHaveBeenCalledOnce()
})

it('keeps intraday fitted on data and size changes while daily retains zoom and pan', async () => {
  const render = async (intraday: boolean, series = model) => act(async () => root.render(<FinancialPlot
    scope={intraday ? 'minute' : 'daily'} series={series} height={400} paneHeights={[300]} intraday={intraday} />))
  await render(true)
  expect(native.createChart.mock.calls[0]?.[1]).toMatchObject({
    handleScale: false, handleScroll: false,
    layout: { panes: { enableResize: false } },
    timeScale: { minBarSpacing: 0.01, rightOffset: 0, fixRightEdge: false },
  })
  const fits = native.timeScale.fitContent.mock.calls.length
  await act(async () => native.handlers.resize({ width: 300, height: 28 }))
  expect(native.timeScale.fitContent).toHaveBeenCalledTimes(fits + 1)
  await render(true, [{ ...model[0], data: [data[0], { ...data[1], value: 12 }] }])
  expect(native.timeScale.fitContent).toHaveBeenCalledTimes(fits + 2)
  await render(false)
  expect(native.chart.applyOptions).toHaveBeenCalledWith(expect.objectContaining({ handleScale: true, handleScroll: true }))
  const dailyFits = native.timeScale.fitContent.mock.calls.length
  await act(async () => native.handlers.resize({ width: 600, height: 28 }))
  expect(native.timeScale.fitContent).toHaveBeenCalledTimes(dailyFits)
})

it('identifies the shared lunch slot when hovering instead of presenting the combined volume as a single minute', async () => {
  const morning = { datetime: '2026-11-05 11:30:00', open: 9, high: 11, low: 9, close: 10, volume: 10, amount: 10000 }
  const afternoon = { ...morning, datetime: '2026-11-05 13:00:00', open: 11, high: 13, low: 11, close: 12, volume: 20, amount: 24000 }
  await act(async () => root.render(<LightweightIntraday symbol="ETF" date="2026-11-05" prevClose={10} data={[morning, afternoon]} />))
  await act(async () => native.handlers.hover({ time: minuteTime('2026-11-05 13:00') }))
  expect(host.textContent).toContain('2026-11-05 11:30 / 13:00')
  expect(host.textContent).toContain('午间衔接')
  expect(host.textContent).toContain('量 30')
  expect(host.textContent).toContain('均价 11.33')
  await act(async () => root.render(<LightweightIntraday symbol="ETF" sessions={[{ date: '2026-11-05', prev_close: 10, rows: [morning, afternoon] }]} />))
  await act(async () => native.handlers.hover({}))
  expect(host.textContent).toContain('2026-11-05 11:30 / 13:00')
  expect(host.textContent).toContain('午间衔接')
})

it('centers yesterday close and fits actual extrema without outlying averages or distant alerts distorting the range', async () => {
  const row = { datetime: '2026-11-05 09:30:00', open: 12, high: 13, low: 12, close: 12.5, volume: 10, amount: 14000 }
  const render = async (rows: MinuteKlineRow[] = [row], prevClose = 10, priceLines: { value: number; end?: string }[] = []) => act(async () => root.render(
    <LightweightIntraday symbol="ETF" date="2026-11-05" data={rows} prevClose={prevClose} priceLines={priceLines} />))
  await render()
  const priceRange = () => {
    const price = native.series[1]
    const options = price.applyOptions.mock.calls.at(-1)?.[0] ?? native.chart.addSeries.mock.calls[1][1]
    return options.autoscaleInfoProvider().priceRange
  }
  expect(priceRange()).toEqual({ minValue: 7, maxValue: 13 })
  await render([row], 10, [{ value: 15 }, { value: 50, end: '2026-11-04' }, { value: NaN }])
  expect(priceRange()).toEqual({ minValue: 7, maxValue: 13 })
  await render([{ ...row, amount: null }])
  expect(priceRange()).toEqual({ minValue: 7, maxValue: 13 })
  expect(host.textContent).toContain('高 13.00')
  await render([{ ...row, high: 12, low: 12, close: 12, amount: 12000 }], 12)
  expect(priceRange().minValue).toBeGreaterThan(0)
  expect(priceRange().minValue).toBeLessThan(12)
  expect(priceRange().maxValue).toBeGreaterThan(12)
})

it('uses compact intraday price margins and anchors volume at zero while preserving daily scale spacing', async () => {
  const series: PlotSeries[] = [...model, { id: 'volume', type: 'Histogram', pane: 1, priceFormat: 'volume', data }]
  const render = async (intraday: boolean) => act(async () => root.render(
    <FinancialPlot scope={intraday ? 'minute' : 'daily'} series={series} height={400} paneHeights={[276, 95]} intraday={intraday}
      annotations={{ dates: [], priceLines: [{ value: 10, axisLabel: true }] }} />))
  await render(true)
  expect(native.createChart.mock.calls[0][1]).toMatchObject({ rightPriceScale: { scaleMargins: { top: 0, bottom: 0 } } })
  expect(native.series[1].priceScale().applyOptions).toHaveBeenLastCalledWith({ entireTextOnly: true, scaleMargins: { top: 0.22, bottom: 0 } })
  const priceFormat = native.chart.addSeries.mock.calls[0][1].priceFormat
  expect(priceFormat.formatter(12.5)).toBe('12.50')
  expect(priceFormat.tickmarksFormatter([10, 10.05, 10.18, 11])).toEqual(['', '', '', '11.00'])
  await render(false)
  expect(native.chart.applyOptions).toHaveBeenCalledWith(expect.objectContaining({ rightPriceScale: expect.objectContaining({ scaleMargins: { top: 0.08, bottom: 0.06 } }) }))
  expect(native.series.at(-1).priceScale().applyOptions).toHaveBeenLastCalledWith({ entireTextOnly: false, scaleMargins: { top: 0.3, bottom: 0 } })
})

it('links ETF price and percentage scales, including the single-session tab, and removes stale axes on context changes', async () => {
  const row = { datetime: '2026-11-05 09:30:00', open: 0.761, high: 0.762, low: 0.76, close: 0.761, volume: 10, amount: null }
  const render = async (sessions = [{ date: '2026-11-05', prev_close: 0.76 as number | null, rows: [row] }]) => act(async () => root.render(
    <LightweightIntraday symbol="ETF" assetType="etf" showLimitLines={false} sessions={sessions} />))
  await render()
  const priceCall = native.chart.addSeries.mock.calls.find(call => call[0] === 'Baseline')!
  const leftCall = native.chart.addSeries.mock.calls.find(call => call[1].priceScaleId === 'left')!
  expect(priceCall[1].baseValue).toEqual({ type: 'price', price: 0.76 })
  expect(priceCall[1].relativeGradient).toBe(false)
  expect(priceCall[1].priceFormat.formatter(0.761)).toBe('0.761')
  expect(leftCall[1].lineVisible).toBe(false)
  expect(leftCall[1].priceFormat.formatter(1)).toBe('+1.00%')
  const right = priceCall[1].autoscaleInfoProvider().priceRange
  const left = leftCall[1].autoscaleInfoProvider().priceRange
  expect((right.minValue + right.maxValue) / 2).toBeCloseTo(0.76)
  expect(left.minValue).toBeCloseTo(-left.maxValue)
  expect(left.minValue).toBeCloseTo((right.minValue / 0.76 - 1) * 100)
  expect(left.maxValue).toBeCloseTo((right.maxValue / 0.76 - 1) * 100)
  expect(priceCall[1].priceFormat.tickmarksFormatter([0.76])).toEqual([''])
  expect(host.textContent).toContain('分时价 0.761')
  await render([{ date: '2026-11-05', prev_close: null, rows: [row] }])
  expect(native.chart.applyOptions.mock.calls.at(-1)![0]).toMatchObject({ leftPriceScale: { visible: false } })
  expect(host.textContent).toContain('昨收缺失 · 仅价格轴')
  await render([{ date: '2026-11-05', prev_close: 0.76, rows: [row] }, { date: '2026-11-04', prev_close: 0.7, rows: [{ ...row, datetime: '2026-11-04 09:30:00' }] }])
  expect(native.chart.applyOptions.mock.calls.at(-1)![0]).toMatchObject({ leftPriceScale: { visible: false } })
  expect(host.textContent).not.toContain('昨收缺失')
})

it('starts daily candles at a readable density and keeps the viewport when changing the volume pane', async () => {
  const rows = Array.from({ length: 100 }, (_, i) => ({ date: new Date(Date.UTC(2026, 6, i + 1)).toISOString().slice(0, 10), open: 10, high: 12, low: 9, close: 11, volume: 10 }))
  const render = async (activeIndicators = ['vol']) => act(async () => root.render(<LightweightDaily symbol="A" data={rows} activeIndicators={activeIndicators} />))
  await render()
  expect(native.timeScale.setVisibleLogicalRange).toHaveBeenLastCalledWith({ from: 58, to: 99 })
  expect(native.chart.addCustomSeries).toHaveBeenCalledOnce()
  await render([])
  expect(native.timeScale.setVisibleLogicalRange).toHaveBeenLastCalledWith({ from: 0, to: 10 })
  await render()
  expect(native.timeScale.setVisibleLogicalRange).toHaveBeenLastCalledWith({ from: 0, to: 10 })
  expect(native.chart.addCustomSeries).toHaveBeenCalledTimes(2)
})

it('switches text labels and candle-body colors while preserving strategy markers and source OHLC', async () => {
  const rows = [
    { date: '2026-11-03', open: 10, high: 11, low: 9, close: 11 },
    { date: '2026-11-04', open: 11, high: 12, low: 10, close: 10.5 },
    { date: '2026-11-05', open: 10.5, high: 11, low: 10, close: 10.8 },
  ]
  const events: ChartMarker[] = [
    { date: rows[0].date, kind: 'buy', above: true, event: 'limit_up', color: '#FACC15', label: '3' },
    { date: rows[1].date, kind: 'neutral', above: true, event: 'broken_limit_up', color: '#8B5CF6', label: '炸' },
    { date: rows[2].date, kind: 'buy', label: '策略买点' },
  ]
  const render = async (markerStyle: DailyMarkerStyle, showMarkers = true, markers = events) => act(async () => root.render(
    <LightweightDaily symbol="A" data={rows} markers={markers} markerStyle={markerStyle} showMarkers={showMarkers} />))
  const markers = () => native.markers.setMarkers.mock.calls.at(-1)![0]
  await render('label')
  expect(markers()[0]).toMatchObject({ text: '3', size: 0, shape: 'circle' })
  expect(markers()[2]).toMatchObject({ text: '策略买点', shape: 'arrowUp' })
  await render('candle')
  const candleData = () => native.series[0].setData.mock.calls.at(-1)![0]
  expect(candleData()[0]).toMatchObject({ color: '#FACC15', open: 10, close: 11 })
  expect(candleData()[1]).toMatchObject({ color: '#8B5CF6', open: 11, close: 10.5 })
  expect(candleData()[2]).not.toHaveProperty('color')
  expect(candleData()[0]).not.toHaveProperty('wickColor')
  expect(rows[0]).not.toHaveProperty('color')
  expect(markers()).toEqual([expect.objectContaining({ text: '策略买点' })])
  await act(async () => native.handlers.hover({ time: rows[1].date }))
  expect(host.textContent).toContain('炸板')
  await render('candle', false)
  expect(candleData().every((row: any) => row.color === undefined)).toBe(true)
  expect(markers()).toEqual([])
  await render('candle', true, events.filter(event => event.event !== 'broken_limit_up'))
  expect(candleData()[1]).not.toHaveProperty('color')
  native.appearance.theme = 'light'
  await render('candle')
  expect(candleData()[0].color).toBe('#CA8A04')
  expect(candleData()[1].color).toBe('#7C3AED')
  await render('label')
  expect(markers()[0].color).toBe('#A16207')
  expect(markers()[1].color).toBe('#7C3AED')
  await render('candle', true, [...events, { date: rows[2].date, kind: 'sell', event: 'custom_event', label: '外部标记' } as unknown as ChartMarker])
  expect(candleData()[2]).not.toHaveProperty('color')
  expect(markers()).toContainEqual(expect.objectContaining({ text: '外部标记' }))
})

it('colors bullish and bearish broken-limit candles by open/close, including gaps and dojis, in both themes', async () => {
  const rows = [
    { date: '2026-10-30', open: 20, high: 21, low: 19, close: 20 },
    { date: '2026-11-02', open: 10, high: 13, low: 9, close: 12 },
    { date: '2026-11-03', open: 15, high: 16, low: 13, close: 14 },
    { date: '2026-11-04', open: 14, high: 15, low: 13, close: 14 },
  ]
  const markers: ChartMarker[] = rows.slice(1).map(row => ({ date: row.date, event: 'broken_limit_up', kind: 'neutral', above: true, label: '炸' }))
  const render = async () => act(async () => root.render(<LightweightDaily symbol="A" data={rows} markers={markers} markerStyle="candle" />))
  const colors = () => native.series[0].setData.mock.calls.at(-1)![0].map((row: any) => row.color)
  await render()
  expect(colors()).toEqual([undefined, '#E879F9', '#8B5CF6', '#8B5CF6'])
  for (const [date, label] of [[rows[1].date, '炸板 · 收阳'], [rows[2].date, '炸板 · 收阴'], [rows[3].date, '炸板 · 开收同价']]) {
    await act(async () => native.handlers.hover({ time: date }))
    expect(host.textContent).toContain(label)
  }
  native.appearance.theme = 'light'
  await render()
  expect(colors()).toEqual([undefined, '#C026D3', '#7C3AED', '#7C3AED'])
  expect(rows.some(row => 'color' in row)).toBe(false)
})

it('distinguishes limit-down bodies from ordinary candles while preserving direction, labels and event controls', async () => {
  const rows = [
    { date: '2026-11-02', open: 11, high: 11, low: 10, close: 10 },
    { date: '2026-11-03', open: 10, high: 10, low: 9, close: 9 },
    { date: '2026-11-04', open: 8, high: 8.1, low: 7.8, close: 8.1 },
    { date: '2026-11-05', open: 7.29, high: 7.29, low: 7.29, close: 7.29 },
  ]
  const markers: ChartMarker[] = rows.slice(1).map(row => ({ date: row.date, event: 'limit_down', kind: 'sell', label: '跌', above: false }))
  // 同日曾触及涨停、最终跌停时，以收盘跌停的实体色为准，且不受标记顺序影响。
  markers.push({ date: rows[1].date, event: 'broken_limit_up', kind: 'neutral', label: '炸' })
  const render = async (markerStyle: DailyMarkerStyle = 'candle', showMarkers = true, events = markers) => act(async () => root.render(
    <LightweightDaily symbol="A" data={rows} markers={events} markerStyle={markerStyle} showMarkers={showMarkers} />))
  const candleData = () => native.series[0].setData.mock.calls.at(-1)![0]
  await render()
  expect(candleData().map((row: any) => row.color)).toEqual([undefined, '#22D3EE', '#22D3EE', '#22D3EE'])
  expect(candleData().every((row: any) => row.wickColor === undefined)).toBe(true)
  expect(host.textContent).toContain('跌停')
  expect(host.textContent).not.toContain('连板')
  for (const row of rows.slice(1)) {
    await act(async () => native.handlers.hover({ time: row.date }))
    expect(host.textContent).toContain('跌停')
  }
  await render('candle', true, markers.slice().reverse())
  expect(candleData()[1].color).toBe('#22D3EE')
  native.appearance.theme = 'light'
  await render()
  expect(candleData().slice(1).every((row: any) => row.color === '#0891B2')).toBe(true)
  await render('label', true, markers.slice(0, 3))
  expect(candleData().every((row: any) => row.color === undefined)).toBe(true)
  expect(native.markers.setMarkers.mock.calls.at(-1)![0]).toEqual(rows.slice(1).map(row => expect.objectContaining({
    time: row.date, text: '跌', position: 'belowBar', shape: 'circle', size: 0, color: '#0E7490',
  })))
  await render('candle', false)
  expect(candleData().every((row: any) => row.color === undefined)).toBe(true)
  expect(native.markers.setMarkers.mock.calls.at(-1)![0]).toEqual([])
  await render('candle', true, [])
  expect(candleData().every((row: any) => row.color === undefined)).toBe(true)
  expect(rows.some(row => 'color' in row)).toBe(false)
})

it('colors recovered limit-down candles by open/close in both themes and restores ordinary candles on toggle', async () => {
  const rows = [
    { date: '2026-10-30', open: 30, high: 31, low: 29, close: 30 },
    { date: '2026-11-02', open: 10, high: 13, low: 9, close: 12 },
    { date: '2026-11-03', open: 15, high: 16, low: 13, close: 14 },
    { date: '2026-11-04', open: 14, high: 15, low: 13, close: 14 },
  ]
  const markers: ChartMarker[] = rows.slice(1).map(row => ({ date: row.date, event: 'limit_down_recovery', kind: 'neutral', label: '翘', above: false }))
  const render = async (markerStyle: DailyMarkerStyle = 'candle', showMarkers = true, events = markers) => act(async () => root.render(
    <LightweightDaily symbol="A" data={rows} markers={events} markerStyle={markerStyle} showMarkers={showMarkers} />))
  const candles = () => native.series[0].setData.mock.calls.at(-1)![0]
  await render()
  expect(candles().map((row: any) => row.color)).toEqual([undefined, '#2DD4BF', '#60A5FA', '#60A5FA'])
  for (const [date, label] of [[rows[1].date, '翘板 · 收阳'], [rows[2].date, '翘板 · 收阴'], [rows[3].date, '翘板 · 开收同价']]) {
    await act(async () => native.handlers.hover({ time: date }))
    expect(host.textContent).toContain(label)
  }
  expect(candles().every((row: any) => row.wickColor === undefined)).toBe(true)
  native.appearance.theme = 'light'
  await render()
  expect(candles().map((row: any) => row.color)).toEqual([undefined, '#0D9488', '#2563EB', '#2563EB'])
  await render('label')
  expect(candles().every((row: any) => row.color === undefined)).toBe(true)
  expect(native.markers.setMarkers.mock.calls.at(-1)![0]).toEqual(rows.slice(1).map(row => expect.objectContaining({
    time: row.date, text: '翘', position: 'belowBar', shape: 'circle', size: 0,
  })))
  await render('candle', false)
  expect(candles().every((row: any) => row.color === undefined)).toBe(true)
  expect(host.textContent).not.toContain('翘板')
  await render('candle', true, [])
  expect(candles().every((row: any) => row.color === undefined)).toBe(true)
  expect(rows.some(row => 'color' in row)).toBe(false)
})

it('keeps compact event labels visible as small dots when zooming out', async () => {
  await act(async () => root.render(<LightweightDaily symbol="A" markerStyle="label"
    data={[{ date: '2026-11-05', open: 10, high: 11, low: 9, close: 11 }]}
    markers={[{ date: '2026-11-05', kind: 'buy', above: true, event: 'limit_up', label: '板' }]} />))
  native.timeScale.getVisibleLogicalRange.mockReturnValueOnce({ from: 0, to: 100 })
  native.handlers.zoom({})
  expect(native.markers.setMarkers.mock.calls.at(-1)![0][0]).toMatchObject({ text: '', shape: 'circle', size: 0.5 })
})

it('keeps trade directions and prices readable when fitting a long holding period', async () => {
  await act(async () => root.render(<LightweightDaily symbol="A" visibleBars="all"
    data={[{ date: '2026-11-05', open: 10, high: 11, low: 9, close: 11 }]}
    markers={[{ date: '2026-11-05', kind: 'buy', label: '买 10.00' }, { date: '2026-11-05', kind: 'sell', label: '卖 11.00' }]} />))
  native.timeScale.getVisibleLogicalRange.mockReturnValueOnce({ from: 0, to: 100 })
  native.handlers.zoom({})
  expect(native.markers.setMarkers.mock.calls.at(-1)![0]).toMatchObject([
    { text: '买 10.00', shape: 'arrowUp' }, { text: '卖 11.00', shape: 'arrowDown' },
  ])
})

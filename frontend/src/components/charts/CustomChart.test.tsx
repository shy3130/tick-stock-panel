// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { CustomDaily, CustomIntraday } from './CustomChart'
import { EMPTY_INDICATOR_LIBRARY, type ChartIndicatorLibrary, type IndicatorEvaluationRequest, type IndicatorEvaluation } from '@/lib/custom-chart-indicators'
import { legacyChartIndicator as newChartIndicator } from '@/lib/chart-indicator-fixtures'
import { CHART_LAYOUT, plotPaneHeights } from '@/lib/chart-layout'

const calls = vi.hoisted(() => ({ library: vi.fn(), evaluate: vi.fn(), save: vi.fn() }))
vi.mock('@/lib/api', async original => {
  const module = await original<typeof import('@/lib/api')>()
  return { ...module, api: { ...module.api, chartIndicatorLibrary: calls.library, evaluateChartIndicators: calls.evaluate, saveChartIndicatorLibrary: calls.save } }
})
vi.mock('./LightweightDaily', () => ({ LightweightDaily: (props: any) => <output data-testid="daily">{JSON.stringify(props)}</output> }))
vi.mock('./LightweightIntraday', () => ({ LightweightIntraday: ({ indicatorControls, ...props }: any) => <><output data-testid="minute">{JSON.stringify(props)}</output>{indicatorControls}</> }))
vi.mock('./IndicatorManager', () => ({ IndicatorManager: () => <div>指标编辑器</div> }))

let host: HTMLDivElement, root: Root, client: QueryClient, library: ChartIndicatorLibrary
const rows = [1, 2, 3, 4].map(n => ({ date: `2026-01-0${n}`, open: n, high: n + 1, low: n, close: n }))
function response(body: IndicatorEvaluationRequest): IndicatorEvaluation {
  return { times: body.rows.map(r => r.time), results: body.definitions.map(d => ({ ...d, outputs: d.outputs.map(o => ({ ...o, values: body.rows.map(r => r.close) })) })),
    errors: [], warnings: [], cache_hit: false, elapsed_ms: 1 }
}
const props = (testId = 'daily') => JSON.parse(host.querySelector(`[data-testid="${testId}"]`)!.textContent!)
async function settle(condition: () => void) {
  let failure: unknown
  for (let attempt = 0; attempt < 100; attempt++) {
    await act(async () => { await new Promise(resolve => setTimeout(resolve, 25)) })
    try { condition(); return } catch (cause) { failure = cause }
  }
  throw failure
}
async function render(symbol = '000001.SZ', source = rows, minute = false, revision = 1) {
  await act(async () => root.render(<QueryClientProvider client={client}>
    <CustomDaily symbol={symbol} data={source} indicatorRows={source} indicatorRevision={revision} />
    {minute && <CustomIntraday symbol={symbol} data={source.map(r => ({ ...r, datetime: `${r.date} 09:30:00`, volume: 10, amount: 100 }))} />}
  </QueryClientProvider>))
}
beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  localStorage.clear()
  library = JSON.parse(JSON.stringify(EMPTY_INDICATOR_LIBRARY))
  calls.library.mockReset().mockImplementation(async () => ({ library, revision: JSON.stringify(library) }))
  calls.evaluate.mockReset().mockImplementation(async body => response(body))
  calls.save.mockReset().mockImplementation(async next => {
    library = JSON.parse(JSON.stringify(next))
    return { library, revision: JSON.stringify(library) }
  })
  host = document.createElement('div'); document.body.append(host); root = createRoot(host)
  client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } })
})
afterEach(async () => { await act(async () => root.unmount()); client.clear(); host.remove() })

it('does no formula evaluation when the current view has no enabled indicators', async () => {
  await render()
  await settle(() => expect(calls.library).toHaveBeenCalledTimes(1))
  await act(async () => { await new Promise(r => setTimeout(r, 200)) })
  expect(calls.evaluate).not.toHaveBeenCalled()
  expect(props().data).toEqual(rows)
  expect(props().showMA).toBe(false)
  expect(props().activeIndicators).toEqual([])
  expect(host.textContent).toContain('选择指标或新建公式')
})

it('shares one library query and binds independent daily/minute parameters', async () => {
  const d = newChartIndicator()
  library.definitions = [d]
  library.views = { daily: [{ id: d.id, parameters: { N: 3 } }], minute: [{ id: d.id, parameters: { N: 8 } }] }
  await render('000001.SZ', rows, true)
  await settle(() => expect(calls.evaluate).toHaveBeenCalledTimes(2))
  const payloads = calls.evaluate.mock.calls.map(([body]) => body as IndicatorEvaluationRequest)
  expect(payloads.find(b => b.timeframe === 'daily')!.definitions[0].parameters[0].value).toBe(3)
  expect(payloads.find(b => b.timeframe === 'minute')!.definitions[0].parameters[0].value).toBe(8)
  expect(calls.library).toHaveBeenCalledTimes(1)
  expect(props('minute').showVolume).toBe(false)
  expect(library.definitions[0].parameters[0].value).toBe(20)
})

it('toggles only chart visibility, retains its toolbar button and independent parameters without opening the editor or recalculating', async () => {
  const d = newChartIndicator()
  library.definitions = [d]
  library.views = { daily: [{ id: d.id, parameters: { N: 3 } }], minute: [{ id: d.id, parameters: { N: 8 } }] }
  await render('000001.SZ', rows, true)
  await settle(() => expect(props().customIndicatorValues?.results).toHaveLength(1))
  await settle(() => expect(props('minute').customIndicatorValues?.results).toHaveLength(1))
  const buttons = () => [...host.querySelectorAll<HTMLButtonElement>('button')].filter(b => b.textContent === d.name)
  expect(buttons()[0].getAttribute('aria-pressed')).toBe('true')
  await act(async () => buttons()[0].click())
  await settle(() => expect(calls.save).toHaveBeenCalledTimes(1))
  await settle(() => expect(props().customIndicatorValues).toBeUndefined())
  expect(buttons()).toHaveLength(2)
  expect(buttons()[0].getAttribute('aria-pressed')).toBe('false')
  expect(buttons()[1].getAttribute('aria-pressed')).toBe('true')
  expect(props('minute').customIndicatorValues.results).toHaveLength(1)
  expect(calls.save.mock.calls[0][0].views).toEqual({
    daily: [{ id: d.id, parameters: { N: 3 }, visible: false }], minute: [{ id: d.id, parameters: { N: 8 } }],
  })
  expect(host.textContent).not.toContain('指标编辑器')
  await settle(() => expect(buttons()[0].disabled).toBe(false))
  await act(async () => buttons()[0].click())
  await settle(() => expect(props().customIndicatorValues?.results).toHaveLength(1))
  expect(calls.save.mock.calls[1][0].views.daily[0]).toMatchObject({ id: d.id, visible: true, parameters: { N: 3 } })
  expect(calls.evaluate).toHaveBeenCalledTimes(2)
})

it('lists hidden indicators without evaluation and evaluates them when first shown', async () => {
  const d = newChartIndicator()
  library.definitions = [d]
  library.views.daily = [{ id: d.id, parameters: { N: 3 }, visible: false }]
  await render()
  await settle(() => expect(host.querySelector(`button[title="显示${d.name}"]`)).not.toBeNull())
  await act(async () => { await new Promise(resolve => setTimeout(resolve, 200)) })
  expect(calls.evaluate).not.toHaveBeenCalled()
  await act(async () => host.querySelector<HTMLButtonElement>(`button[title="显示${d.name}"]`)!.click())
  await settle(() => expect(props().customIndicatorValues?.results).toHaveLength(1))
  expect(calls.evaluate).toHaveBeenCalledOnce()
})

it('rolls back failed visibility saves, keeps the other indicators and permits a retry', async () => {
  const ma = newChartIndicator(), macd = newChartIndicator('macd')
  library.definitions = [ma, macd]
  library.views.daily = [ma, macd].map(d => ({ id: d.id, parameters: {} }))
  let reject!: (error: Error) => void
  calls.save.mockImplementationOnce(() => new Promise((_resolve, fail) => { reject = fail }))
  await render()
  await settle(() => expect(props().customIndicatorValues?.results).toHaveLength(2))
  const toggle = () => [...host.querySelectorAll<HTMLButtonElement>('button')].find(b => b.textContent === macd.name)!
  await act(async () => toggle().click())
  await settle(() => expect(props().customIndicatorValues?.results.map((r: any) => r.id)).toEqual([ma.id]))
  expect(toggle().disabled).toBe(true)
  await act(async () => reject(new Error('保存失败，请重试')))
  await settle(() => expect(props().customIndicatorValues?.results).toHaveLength(2))
  expect(host.textContent).toContain('保存失败')
  await settle(() => expect(toggle().disabled).toBe(false))
  await act(async () => toggle().click())
  await settle(() => expect(toggle().getAttribute('aria-pressed')).toBe('false'))
  expect(calls.evaluate).toHaveBeenCalledOnce()
})

it('cancels a superseded symbol and never paints its late response on the new stock', async () => {
  const d = newChartIndicator(); library.definitions = [d]; library.views.daily = [{ id: d.id, parameters: {} }]
  const pending: { body: IndicatorEvaluationRequest; signal: AbortSignal; resolve: (result: IndicatorEvaluation) => void }[] = []
  calls.evaluate.mockImplementation((body, signal) => new Promise(resolve => pending.push({ body, signal, resolve })))
  await render('000001.SZ')
  await settle(() => expect(pending).toHaveLength(1))
  await render('600000.SH')
  await settle(() => expect(pending).toHaveLength(2))
  expect(pending[0].signal.aborted).toBe(true)
  await act(async () => pending[0].resolve(response(pending[0].body)))
  expect(props().symbol).toBe('600000.SH')
  expect(props().customIndicatorValues).toBeUndefined()
  await act(async () => pending[1].resolve(response(pending[1].body)))
  await settle(() => expect(props().customIndicatorValues.results).toHaveLength(1))
})

it('re-evaluates a corrected earlier bar even when the tail did not change', async () => {
  const d = newChartIndicator(); library.definitions = [d]; library.views.daily = [{ id: d.id, parameters: {} }]
  await render()
  await settle(() => expect(calls.evaluate).toHaveBeenCalledTimes(1))
  const corrected = [{ ...rows[0], close: 1.5 }, ...rows.slice(1)]
  await render('000001.SZ', corrected)
  await settle(() => expect(calls.evaluate).toHaveBeenCalledTimes(2))
  expect(calls.evaluate.mock.calls[1][0].rows[0].close).toBe(1.5)
  expect(calls.evaluate.mock.calls[1][0].rows.at(-1)).toEqual(calls.evaluate.mock.calls[0][0].rows.at(-1))
})

it('checks repaired offscreen warmup after a quote refresh with identical visible bars', async () => {
  const d = newChartIndicator(); library.definitions = [d]; library.views.daily = [{ id: d.id, parameters: {} }]
  await render()
  await settle(() => expect(calls.evaluate).toHaveBeenCalledTimes(1))
  await render('000001.SZ', rows, false, 2)
  await settle(() => expect(calls.evaluate).toHaveBeenCalledTimes(2))
  expect(calls.evaluate.mock.calls[1][0]).toEqual(calls.evaluate.mock.calls[0][0])
})

it('keeps chart geometry equal with asymmetric panes and respects the saved indicator order including volume', async () => {
  const volume = { ...newChartIndicator('blank'), id: 'volume', format: 'volume' as const,
    outputs: [{ key: 'VOLBAR', label: '量', formula: 'V', style: 'volume' as const, color: '#C74040' }] }
  const macd = newChartIndicator('macd'), rsi = newChartIndicator('rsi')
  library.definitions = [volume, macd, rsi]
  library.views = { daily: [macd, rsi, volume].map(d => ({ id: d.id, parameters: {} })), minute: [{ id: volume.id, parameters: {} }] }
  await act(async () => root.render(<QueryClientProvider client={client}>
    <CustomDaily symbol="A" data={rows} indicatorRows={rows} height={380} />
    <CustomIntraday symbol="A" height={420} data={rows.map(r => ({ ...r, datetime: `${r.date} 09:30:00`, volume: 10, amount: null }))} />
  </QueryClientProvider>))
  await settle(() => expect(calls.evaluate).toHaveBeenCalledTimes(2))
  await settle(() => expect(props().customIndicatorValues?.results).toHaveLength(3))
  expect(props().height).toBe(380 + 3 * CHART_LAYOUT.subPaneSlot)
  expect(props('minute').height).toBe(420 + CHART_LAYOUT.subPaneSlot)
  expect(props().customIndicatorValues.results.map((d: { id: string }) => d.id)).toEqual([macd.id, rsi.id, volume.id])
  expect([...host.querySelectorAll('button[aria-pressed]')].map(button => button.textContent)).toEqual([macd.name, rsi.name, volume.name, volume.name])
  const dailyPanes = plotPaneHeights(props().height - CHART_LAYOUT.readout, 3)
  const minutePanes = plotPaneHeights(props('minute').height - CHART_LAYOUT.toolbar - CHART_LAYOUT.readout, 1)
  expect(dailyPanes.slice(0, 2)).toEqual(minutePanes)
  expect(library.views.daily.map(d => d.id)).toEqual([macd.id, rsi.id, volume.id])
})

it('migrates the legacy volume switch and period once without changing minute settings, formulas or parameter choices', async () => {
  const volume = { ...newChartIndicator('blank'), id: 'my_volume', format: 'volume' as const,
    outputs: [{ key: 'BARS', label: '量', formula: 'V', style: 'volume' as const, color: '#C74040' }] }
  library.definitions = [volume]
  library.views = { daily: [{ id: volume.id, parameters: { N: 5 } }], minute: [{ id: volume.id, parameters: { N: 8 } }] }
  localStorage.setItem('stock_volume_compare', JSON.stringify({ enabled: false, days: 7 }))
  await render('000001.SZ', rows, true)
  await settle(() => expect(calls.save).toHaveBeenCalledOnce())
  await settle(() => expect(props().customVolumeCompare).toEqual({ my_volume: { days: 7, show_labels: false, show_readout: false } }))
  expect(calls.save.mock.calls[0][0].views.daily[0]).toEqual({ id: volume.id, parameters: { N: 5 },
    volume_compare: { days: 7, show_labels: false, show_readout: false } })
  expect(library.views.minute).toEqual([{ id: volume.id, parameters: { N: 8 } }])
  expect(props().volumeCompare).toBeUndefined()
  expect(library.definitions[0]).toEqual(volume)
  // A newly mounted chart must prefer the saved indicator setting over a stale local preference.
  localStorage.setItem('stock_volume_compare', JSON.stringify({ enabled: true, days: 1 }))
  await act(async () => root.unmount())
  client.clear(); root = createRoot(host)
  await render()
  await settle(() => expect(props().customVolumeCompare.my_volume.days).toBe(7))
  expect(calls.save).toHaveBeenCalledOnce()
})

it('keeps the chart and editor available when migration cannot be saved and clears the warning after retry', async () => {
  const volume = { ...newChartIndicator('blank'), id: 'volume', outputs: [
    { key: 'BARS', label: '量', formula: 'V', style: 'volume' as const, color: '#C74040' },
  ] }
  library.definitions = [volume]; library.views.daily = [{ id: volume.id, parameters: {} }]
  calls.save.mockRejectedValueOnce(new Error('配置暂时无法写入'))
  await render()
  await settle(() => expect(props().customIndicatorValues?.results).toHaveLength(1))
  expect(host.textContent).toContain('旧量比设置尚未保存')
  expect(host.querySelector<HTMLButtonElement>('[aria-label="日K自定义指标"]')!.disabled).toBe(false)
  await act(async () => [...host.querySelectorAll<HTMLButtonElement>('button')].find(button => button.textContent === '重试')!.click())
  await settle(() => expect(host.textContent).not.toContain('旧量比设置尚未保存'))
  expect(calls.save).toHaveBeenCalledTimes(2)
  expect(calls.evaluate).toHaveBeenCalledOnce()
})

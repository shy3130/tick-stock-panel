// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { IndicatorManager } from './IndicatorManager'
import { ApiError } from '@/lib/api'
import { EMPTY_INDICATOR_LIBRARY, type IndicatorLibraryResponse, type IndicatorSelection, type IndicatorView } from '@/lib/custom-chart-indicators'
import { legacyChartIndicator as newChartIndicator } from '@/lib/chart-indicator-fixtures'

const calls = vi.hoisted(() => ({ save: vi.fn(), evaluate: vi.fn(), compile: vi.fn(), export: vi.fn(), download: vi.fn(), wide: true, sidebar: true }))
vi.mock('@/lib/useMediaQuery', () => ({ useMediaQuery: (query: string) => query.includes('1024') ? calls.wide : calls.sidebar }))
vi.mock('@/lib/api', async original => {
  const module = await original<typeof import('@/lib/api')>()
  return { ...module, api: { ...module.api, compileChartIndicator: calls.compile, saveChartIndicatorLibrary: calls.save, evaluateChartIndicators: calls.evaluate, exportChartIndicators: calls.export } }
})
vi.mock('@/lib/resourceTransfer', () => ({ downloadBlob: calls.download }))
vi.mock('./FinancialPlot', () => ({ FinancialPlot: (props: any) => <output>{JSON.stringify(props)}</output> }))
let host: HTMLDivElement, root: Root, client: QueryClient, initial: IndicatorLibraryResponse
const close = vi.fn()
const presets = [
  { ...newChartIndicator(), id: 'builtin_ma', name: 'MA' },
  { ...newChartIndicator('macd'), id: 'builtin_macd', name: 'MACD' },
  { ...newChartIndicator('blank'), id: 'builtin_volume', name: '成交量', syntax: 'tdx' as const, source: '量:VOL,VOLSTICK;' },
]
const rows = [1, 2, 3, 4].map(n => ({ date: `2026-01-0${n}`, open: n, high: n + 1, low: n, close: n }))
const click = async (text: string) => act(async () => { [...document.querySelectorAll('button')].find(b => b.textContent === text)!.click() })
async function input(label: string, value: string) {
  const node = document.querySelector<HTMLInputElement | HTMLTextAreaElement>(`[aria-label="${label}"]`)!
  const prototype = node instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype
  await act(async () => { Object.getOwnPropertyDescriptor(prototype, 'value')!.set!.call(node, value); node.dispatchEvent(new Event('input', { bubbles: true })) })
}
async function mount(sourceRows: object[] = rows, view: IndicatorView = 'daily') {
  await act(async () => root.render(<QueryClientProvider client={client}>
    <IndicatorManager initial={initial} symbol="000001.SZ" view={view} sourceRows={sourceRows} onClose={close} />
  </QueryClientProvider>))
}
beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  close.mockReset()
  calls.save.mockReset().mockImplementation(async library => ({ library, revision: 'b'.repeat(64) }))
  calls.compile.mockReset().mockResolvedValue({ valid: true, warmup: 0, dependencies: [] })
  calls.evaluate.mockReset()
  calls.export.mockReset().mockResolvedValue(new Blob(['{}']))
  calls.download.mockReset()
  calls.wide = true; calls.sidebar = true
  vi.spyOn(window, 'confirm').mockReturnValue(true)
  initial = { library: JSON.parse(JSON.stringify(EMPTY_INDICATOR_LIBRARY)), revision: 'a'.repeat(64), presets }
  host = document.createElement('div'); document.body.append(host); root = createRoot(host)
  client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } })
})
afterEach(async () => { await act(async () => root.unmount()); client.clear(); host.remove(); vi.restoreAllMocks() })

it('creates an empty source indicator and saves pasted code without seeding presets', async () => {
  await mount()
  expect(document.body.textContent).toContain('把你的公式放到图上')
  await click('创建空白指标')
  expect(document.querySelector<HTMLTextAreaElement>('[aria-label="公式源码"]')!.value).toBe('')
  expect(document.querySelector('[aria-label="指标模板"]')).toBeNull()
  await input('指标名称', '我的均线')
  await input('公式源码', '均线:MA(C,5),COLORYELLOW;')
  await click('保存方案')
  const [library, revision] = calls.save.mock.calls[0]
  expect(revision).toBe('a'.repeat(64))
  expect(library.definitions).toHaveLength(1)
  expect(library.definitions[0]).toMatchObject({ name: '我的均线', syntax: 'tdx', source: '均线:MA(C,5),COLORYELLOW;', outputs: [], parameters: [] })
  expect(library.views.daily[0]).toMatchObject({ parameters: {}, visible: false })
  expect(initial.library.definitions).toEqual([])
  expect(close).toHaveBeenCalledOnce()
})

it('adds chosen built-ins to the draft and shows them after saving alongside existing formulas', async () => {
  const custom = newChartIndicator()
  initial.library.definitions = [custom]
  initial.library.views.daily = [{ id: custom.id, parameters: {}, visible: true }]
  await mount()
  const add = async (name: string) => act(async () => {
    document.querySelector<HTMLButtonElement>(`[aria-label="添加${name}到日K指标栏"]`)!.click()
  })
  await add('MA')
  await add('MACD')
  await add('成交量')
  expect(calls.save).not.toHaveBeenCalled()
  expect(initial.library.definitions).toEqual([custom])
  expect(document.querySelector('[aria-label="添加MA到日K指标栏"]')).toBeNull()
  expect(document.querySelector<HTMLTextAreaElement>('[aria-label="公式源码"]')!.value).toContain('VOLSTICK')
  await click('保存方案')
  const [library] = calls.save.mock.calls[0]
  expect(library.definitions.map((d: { name: string }) => d.name)).toEqual([custom.name, 'MA', 'MACD', '成交量'])
  expect(library.views.daily.map((s: IndicatorSelection) => s.visible)).toEqual([true, true, true, true])
  expect(library.views.daily.at(-1).volume_compare).toEqual({ days: 1, show_labels: true, show_readout: true })
  expect(library.views.minute).toEqual([])
})

it('reuses a saved built-in in the other view and keeps the saved edited formula', async () => {
  const edited = { ...newChartIndicator(), id: 'builtin_ma', name: '已调整的 MA' }
  initial.library.definitions = [edited]
  initial.library.views.daily = [{ id: edited.id, parameters: { N: 10 }, visible: false }]
  await mount([], 'minute')
  expect(document.querySelector('[aria-label="添加MA到分时指标栏"]')).toBeNull()
  await act(async () => document.querySelector<HTMLInputElement>('[aria-label="在分时指标栏显示已调整的 MA"]')!.click())
  await click('保存方案')
  const [library] = calls.save.mock.calls[0]
  expect(library.definitions).toEqual([edited])
  expect(library.views.daily).toEqual(initial.library.views.daily)
  expect(library.views.minute).toEqual([{ id: edited.id, parameters: { N: 20 }, visible: true }])
})

it('preserves the draft and existing saved settings when validation rejects a formula', async () => {
  const d = newChartIndicator(); initial.library.definitions = [d]
  calls.save.mockRejectedValue(new ApiError('REF 的负周期属于未来函数', 400))
  await mount()
  await input('输出1公式', 'REF(C,-1)')
  await click('保存方案')
  expect(document.querySelector('[role="alert"]')!.textContent).toContain('未来函数')
  expect(document.querySelector<HTMLTextAreaElement>('[aria-label="输出1公式"]')!.value).toBe('REF(C,-1)')
  expect(initial.library.definitions[0].outputs[0].formula).toBe('MA(C,N)')
  expect(close).not.toHaveBeenCalled()
  vi.mocked(window.confirm).mockReturnValue(false)
  await click('取消')
  expect(close).not.toHaveBeenCalled()
})

it('changes only the current view parameter overrides', async () => {
  const d = newChartIndicator(); initial.library.definitions = [d]
  initial.library.views = { daily: [{ id: d.id, parameters: { N: 10 } }], minute: [{ id: d.id, parameters: { N: 30 } }] }
  await mount()
  await click('参数')
  await input('参数N本图取值', '50')
  await click('保存方案')
  const library = calls.save.mock.calls[0][0]
  expect(library.views.daily[0].parameters.N).toBe(50)
  expect(library.views.minute[0].parameters.N).toBe(30)
})

it('uses checkboxes for toolbar membership independently of chart visibility', async () => {
  const ma = newChartIndicator(), macd = newChartIndicator('macd')
  initial.library.definitions = [ma, macd]
  initial.library.views.daily = [{ id: ma.id, parameters: { N: 10 }, visible: false }]
  await mount()
  expect(document.querySelector<HTMLInputElement>(`[aria-label="在日K指标栏显示${ma.name}"]`)!.checked).toBe(true)
  expect(document.querySelector<HTMLInputElement>(`[aria-label="在日K指标栏显示${macd.name}"]`)!.checked).toBe(false)
  await act(async () => document.querySelector<HTMLInputElement>(`[aria-label="在日K指标栏显示${macd.name}"]`)!.click())
  await click('保存方案')
  expect(calls.save.mock.calls[0][0].views.daily).toEqual([
    { id: ma.id, parameters: { N: 10 }, visible: false },
    { id: macd.id, parameters: { SHORT: 12, LONG: 26, SIGNAL: 9 }, visible: false },
  ])
  expect(calls.evaluate).not.toHaveBeenCalled()
})

const indicatorNames = () => [...document.querySelectorAll('[aria-label="指标列表"] li button[aria-pressed]')]
  .map(button => button.firstElementChild?.textContent)
const orderButton = (direction: string, name: string) => document.querySelector<HTMLButtonElement>(`button[aria-label="${direction}${name}"]`)!

it.each(['daily', 'minute'] as const)('reorders %s toolbar members independently and restores their saved order', async view => {
  const ma = newChartIndicator(), macd = newChartIndicator('macd'), rsi = newChartIndicator('rsi'), unlisted = newChartIndicator('blank')
  const selections: IndicatorSelection[] = [{ id: rsi.id, parameters: { N: 6 }, visible: false },
    { id: ma.id, parameters: { N: 10 }, visible: true }, { id: macd.id, parameters: { SIGNAL: 3 }, visible: true }]
  const otherView = view === 'daily' ? 'minute' : 'daily'
  initial.library.definitions = [ma, unlisted, macd, rsi]
  initial.library.views[view] = selections
  initial.library.views[otherView] = [{ id: ma.id, parameters: { N: 30 }, visible: false }]
  const original = structuredClone(initial.library)
  await mount(rows, view)
  expect(indicatorNames()).toEqual([rsi.name, ma.name, macd.name, unlisted.name])
  expect(orderButton('上移', rsi.name).disabled).toBe(true)
  expect(orderButton('下移', macd.name).disabled).toBe(true)
  expect(orderButton('上移', unlisted.name)).toBeNull()
  expect(document.querySelector(`[aria-label="拖动${macd.name}排序"]`)).not.toBeNull()
  expect(document.querySelector(`[aria-label="拖动${unlisted.name}排序"]`)).toBeNull()
  await act(async () => orderButton('上移', macd.name).click())
  await act(async () => orderButton('下移', rsi.name).click())
  expect(indicatorNames()).toEqual([macd.name, rsi.name, ma.name, unlisted.name])
  expect(document.querySelector<HTMLInputElement>('[aria-label="指标名称"]')!.value).toBe(rsi.name)
  expect(orderButton('上移', macd.name).disabled).toBe(true)
  expect(orderButton('下移', ma.name).disabled).toBe(true)
  await click('保存方案')
  const [library, revision] = calls.save.mock.calls[0]
  expect(library.views[view]).toEqual([selections[2], selections[0], selections[1]])
  expect(library.views[otherView]).toEqual(original.views[otherView])
  expect(library.definitions).toEqual(original.definitions)
  expect(initial.library).toEqual(original)
  expect(revision).toBe(initial.revision)
  expect(calls.evaluate).not.toHaveBeenCalled()
  expect(close).toHaveBeenCalledOnce()
  initial = await calls.save.mock.results[0].value
  await act(async () => root.render(null))
  await mount(rows, view)
  expect(indicatorNames()).toEqual([macd.name, rsi.name, ma.name, unlisted.name])
})

it('keeps the reordered draft after a save conflict and disables sorting while saving', async () => {
  const ma = newChartIndicator(), macd = newChartIndicator('macd')
  initial.library.definitions = [ma, macd]
  initial.library.views.daily = [ma, macd].map(d => ({ id: d.id, parameters: {} }))
  let reject!: (error: Error) => void
  calls.save.mockImplementationOnce(() => new Promise((_resolve, fail) => { reject = fail }))
  await mount()
  await act(async () => orderButton('上移', macd.name).click())
  await click('保存方案')
  expect(document.querySelector<HTMLFieldSetElement>('[aria-label="指标列表"]')!.disabled).toBe(true)
  expect(document.querySelector<HTMLButtonElement>(`[aria-label="拖动${macd.name}排序"]`)!.disabled).toBe(true)
  await act(async () => reject(new ApiError('指标配置已在其他窗口更新', 409)))
  expect(document.querySelector('[role="alert"]')!.textContent).toContain('其他窗口更新')
  expect(indicatorNames()).toEqual([macd.name, ma.name])
  expect(close).not.toHaveBeenCalled()
  expect(initial.library.views.daily.map(s => s.id)).toEqual([ma.id, macd.id])
  await click('保存方案')
  expect(calls.save.mock.calls[1][0].views.daily.map((s: { id: string }) => s.id)).toEqual([macd.id, ma.id])
  expect(close).toHaveBeenCalledOnce()
})

it('previews an unsaved formula using current view parameters and the original market rows', async () => {
  const d = newChartIndicator(); initial.library.definitions = [d]; initial.library.views.daily = [{ id: d.id, parameters: { N: 3 } }]
  calls.evaluate.mockImplementation(async body => ({ times: body.rows.map((r: any) => r.time), results: body.definitions.map((definition: any) => ({ ...definition, outputs: definition.outputs.map((o: any) => ({ ...o, values: [null, null, 2, 3] })) })), errors: [], warnings: [], elapsed_ms: 1, cache_hit: false }))
  await mount()
  await click('解析并预览')
  const body = calls.evaluate.mock.calls[0][0]
  expect(body.definitions[0].parameters[0].value).toBe(3)
  expect(body.rows).toHaveLength(4)
  expect(document.querySelector('output')).not.toBeNull()
  expect(calls.save).not.toHaveBeenCalled()
})

it('exports the saved selected definition with its revision and blocks unsaved changes', async () => {
  const d = newChartIndicator(); initial.library.definitions = [d]
  await mount()
  await click('导出当前指标')
  expect(calls.export).toHaveBeenCalledWith([d.id], initial.revision)
  expect(calls.download).toHaveBeenCalledWith(expect.any(Blob), 'chart-indicators.json')
  expect(calls.save).not.toHaveBeenCalled()
  await input('指标名称', '未保存名称')
  const exportButton = [...document.querySelectorAll('button')].find(b => b.textContent === '导出当前指标')!
  expect(exportButton.disabled).toBe(true)
  expect(document.body.textContent).toContain('保存修改后可导出')
})

it('edits volume comparison labels and readouts independently without changing minute settings or volume formulas', async () => {
  const volume = { ...newChartIndicator('blank'), id: 'volume', format: 'volume' as const,
    outputs: [{ key: 'BARS', label: '量', formula: 'V', style: 'volume' as const, color: '#C74040' }] }
  initial.library.definitions = [volume]
  initial.library.views = { daily: [{ id: volume.id, parameters: {}, volume_compare: { days: 7, show_labels: false, show_readout: false } }],
    minute: [{ id: volume.id, parameters: { N: 8 } }] }
  await mount()
  await click('显示')
  expect(document.querySelector<HTMLInputElement>('[aria-label="量比对比周期"]')!.value).toBe('7')
  await input('量比对比周期', '5')
  await act(async () => document.querySelector<HTMLInputElement>('[aria-label="显示量比倍数标注"]')!.click())
  await click('保存方案')
  const library = calls.save.mock.calls[0][0]
  expect(library.views.daily[0].volume_compare).toEqual({ days: 5, show_labels: true, show_readout: false })
  expect(library.views.minute).toEqual(initial.library.views.minute)
  expect(library.definitions).toEqual(initial.library.definitions)
})

it('keeps source errors and the draft, then previews the corrected pasted source', async () => {
  await mount()
  await click('创建空白指标')
  const source = '均线:MA(C,3);\n买入:CROSS(C,均线),NODRAW;'
  await input('公式源码', source)
  calls.compile.mockRejectedValueOnce(new ApiError('第2行：不支持的函数 UNKNOWN', 400))
  await click('解析并预览')
  expect(document.querySelector('[role="alert"]')?.textContent).toContain('第2行')
  expect(document.querySelector<HTMLTextAreaElement>('[aria-label="公式源码"]')!.value).toBe(source)
  expect(calls.evaluate).not.toHaveBeenCalled()
  await click('定位到公式')
  await act(async () => { await new Promise(resolve => requestAnimationFrame(resolve)) })
  const sourceInput = document.querySelector<HTMLTextAreaElement>('[aria-label="公式源码"]')!
  expect(document.activeElement).toBe(sourceInput)
  expect(sourceInput.selectionStart).toBe(source.indexOf('\n') + 1)
  calls.evaluate.mockImplementation(async body => ({ times: body.rows.map((r: any) => r.time), results: [{ ...body.definitions[0], outputs: [{ key: '均线', label: '均线', style: 'line', color: '#FF0000', values: [1, 1.5, 2, 3] }], drawings: [] }], errors: [], warnings: [], elapsed_ms: 1, cache_hit: false }))
  await click('解析并预览')
  expect(calls.evaluate.mock.calls[0][0].definitions[0].source).toBe(source)
  expect(document.querySelector('output')).not.toBeNull()
  await input('公式源码', source+'\n价格:C;')
  expect(document.querySelector('output')).toBeNull()
  expect(document.body.textContent).toContain('请重新解析预览')
})


it('parses and saves source without a market-data snapshot', async () => {
  await mount([])
  await click('创建空白指标')
  await input('公式源码', '均线:MA(C,5);')
  await click('解析并预览')
  expect(calls.compile).toHaveBeenCalledOnce()
  expect(calls.evaluate).not.toHaveBeenCalled()
  expect(document.body.textContent).toContain('解析通过。当前没有行情')
  await click('保存方案')
  expect(calls.save.mock.calls[0][0].definitions[0].source).toBe('均线:MA(C,5);')
})

it('keeps formula and parameter edits when switching sections and saves them together', async () => {
  const d = newChartIndicator()
  initial.library.definitions = [d]
  initial.library.views.daily = [{ id: d.id, parameters: { N: 10 } }]
  await mount()
  await input('输出1公式', 'MA(C,N)+1')
  await click('参数')
  await input('参数N本图取值', '15')
  await click('显示')
  await input('小数位', '3')
  await click('公式')
  expect(document.querySelector<HTMLTextAreaElement>('[aria-label="输出1公式"]')!.value).toBe('MA(C,N)+1')
  await click('保存方案')
  const library = calls.save.mock.calls[0][0]
  expect(library.definitions[0]).toMatchObject({ precision: 3, outputs: [{ formula: 'MA(C,N)+1' }] })
  expect(library.views.daily[0].parameters.N).toBe(15)
})

it('adjusts current-view parameters alongside the formula and uses them in the next preview', async () => {
  const d = newChartIndicator()
  initial.library.definitions = [d]
  initial.library.views = { daily: [{ id: d.id, parameters: { N: 10 } }], minute: [{ id: d.id, parameters: { N: 30 } }] }
  calls.evaluate.mockImplementation(async body => ({ times: body.rows.map((r: any) => r.time),
    results: body.definitions.map((d: any) => ({ ...d, outputs: d.outputs.map((o: any) => ({ ...o, values: [1, 2, 3, 4] })) })),
    errors: [], warnings: [], elapsed_ms: 1, cache_hit: false }))
  await mount()
  await input('快速调整参数N', '25')
  expect(document.querySelector('[role="tab"][aria-selected="true"]')!.textContent).toBe('公式')
  expect(document.querySelector('[aria-label="输出1公式"]')).not.toBeNull()
  await click('解析并预览')
  expect(calls.evaluate.mock.calls[0][0].definitions[0].parameters[0].value).toBe(25)
  await click('保存方案')
  expect(calls.save.mock.calls[0][0].views.daily[0].parameters.N).toBe(25)
  expect(calls.save.mock.calls[0][0].views.minute[0].parameters.N).toBe(30)
})

it('keeps outputs independently expanded across section changes and preserves edits when manually collapsed', async () => {
  const d = newChartIndicator('macd')
  initial.library.definitions = [d]
  await mount()
  await input('输出1公式', 'EMA(C,SHORT)+1')
  await act(async () => document.querySelector<HTMLButtonElement>('[aria-label="编辑输出2"]')!.click())
  expect(document.querySelector<HTMLTextAreaElement>('[aria-label="输出1公式"]')!.value).toBe('EMA(C,SHORT)+1')
  await input('输出2公式', 'EMA(DIF,SIGNAL)+2')
  await click('参数')
  await click('公式')
  expect(document.querySelector<HTMLTextAreaElement>('[aria-label="输出1公式"]')!.value).toBe('EMA(C,SHORT)+1')
  expect(document.querySelector<HTMLTextAreaElement>('[aria-label="输出2公式"]')!.value).toBe('EMA(DIF,SIGNAL)+2')
  await act(async () => document.querySelector<HTMLButtonElement>('[aria-label="编辑输出1"]')!.click())
  expect(document.querySelector('[aria-label="输出1公式"]')).toBeNull()
  expect(document.querySelector('[aria-label="输出2公式"]')).not.toBeNull()
  await act(async () => document.querySelector<HTMLButtonElement>('[aria-label="编辑输出1"]')!.click())
  expect(document.querySelector<HTMLTextAreaElement>('[aria-label="输出1公式"]')!.value).toBe('EMA(C,SHORT)+1')
  await click('保存方案')
  expect(calls.save.mock.calls[0][0].definitions[0].outputs.map((o: { formula: string }) => o.formula)).toEqual(['EMA(C,SHORT)+1', 'EMA(DIF,SIGNAL)+2', d.outputs[2].formula])
})

it('remembers each indicator\'s expanded outputs when switching indicators without changing the saved library', async () => {
  const macd = newChartIndicator('macd'), ma = newChartIndicator()
  initial.library.definitions = [macd, ma]
  await mount()
  const toggle = (n: number) => document.querySelector<HTMLButtonElement>(`[aria-label="编辑输出${n}"]`)!
  await act(async () => toggle(2).click())
  await act(async () => toggle(1).click())
  const select = (name: string) => [...document.querySelectorAll<HTMLButtonElement>('[aria-label="指标列表"] button[aria-pressed]')]
    .find(button => button.firstElementChild?.textContent === name)!
  await act(async () => select(ma.name).click())
  expect(document.querySelector('[aria-label="输出1公式"]')).not.toBeNull()
  await act(async () => select(macd.name).click())
  expect(toggle(1).getAttribute('aria-expanded')).toBe('false')
  expect(toggle(2).getAttribute('aria-expanded')).toBe('true')
  expect(document.querySelector('[aria-label="输出2公式"]')).not.toBeNull()
  expect([...document.querySelectorAll<HTMLButtonElement>('button')].find(button => button.textContent === '保存方案')!.disabled).toBe(true)
  expect(calls.save).not.toHaveBeenCalled()
})

it('preserves other expanded outputs when appending an output or deleting an earlier one', async () => {
  const d = newChartIndicator('macd')
  initial.library.definitions = [d]
  await mount()
  await act(async () => document.querySelector<HTMLButtonElement>('[aria-label="编辑输出3"]')!.click())
  await click('添加输出')
  expect(document.querySelector('[aria-label="输出1公式"]')).not.toBeNull()
  expect(document.querySelector('[aria-label="输出3公式"]')).not.toBeNull()
  expect(document.querySelector<HTMLTextAreaElement>('[aria-label="输出4公式"]')!.value).toBe('C')
  await act(async () => document.querySelector<HTMLButtonElement>('[aria-label="删除输出1"]')!.click())
  expect(document.querySelector('[aria-label="输出1公式"]')).toBeNull()
  expect(document.querySelector<HTMLTextAreaElement>('[aria-label="输出2公式"]')!.value).toBe(d.outputs[2].formula)
  expect(document.querySelector<HTMLTextAreaElement>('[aria-label="输出3公式"]')!.value).toBe('C')
  await click('保存方案')
  expect(calls.save.mock.calls[0][0].definitions[0].outputs.map((o: { key: string }) => o.key)).toEqual(['DEA', 'HIST', 'LINE1'])
})

it('uses a separate indicator picker on narrow screens and shows the preview after parsing', async () => {
  calls.wide = false; calls.sidebar = false
  const ma = newChartIndicator(), macd = newChartIndicator('macd')
  initial.library.definitions = [ma, macd]
  initial.library.views.daily = [ma, macd].map(d => ({ id: d.id, parameters: {} }))
  calls.evaluate.mockImplementation(async body => ({ times: body.rows.map((r: any) => r.time),
    results: body.definitions.map((d: any) => ({ ...d, outputs: d.outputs.map((o: any) => ({ ...o, values: [1, 2, 3, 4] })) })),
    errors: [], warnings: [], elapsed_ms: 1, cache_hit: false }))
  await mount()
  await act(async () => document.querySelector<HTMLButtonElement>('[aria-label="打开指标列表"]')!.click())
  expect(document.querySelector<HTMLElement>('[aria-label="指标库"]')!.hidden).toBe(false)
  await act(async () => [...document.querySelectorAll<HTMLButtonElement>('[aria-label="指标列表"] button[aria-pressed]')]
    .find(button => button.firstElementChild?.textContent === macd.name)!.click())
  expect(document.querySelector<HTMLElement>('[aria-label="指标库"]')!.hidden).toBe(true)
  expect(document.querySelector<HTMLInputElement>('[aria-label="指标名称"]')!.value).toBe(macd.name)
  await click('解析并预览')
  expect(document.querySelector<HTMLElement>('[aria-label="指标预览与帮助"]')!.hidden).toBe(false)
  expect(document.querySelector<HTMLElement>('[aria-label="指标编辑区"]')!.hidden).toBe(true)
  await click('公式')
  expect(document.querySelector<HTMLElement>('[aria-label="指标编辑区"]')!.hidden).toBe(false)
})

it('requires confirmation before removing an indicator from both views', async () => {
  const ma = newChartIndicator(), macd = newChartIndicator('macd')
  initial.library.definitions = [ma, macd]
  initial.library.views = { daily: [{ id: ma.id, parameters: {} }], minute: [{ id: ma.id, parameters: {} }] }
  await mount()
  await act(async () => document.querySelector<HTMLButtonElement>('[aria-label="删除指标"]')!.click())
  expect(document.querySelector<HTMLInputElement>('[aria-label="指标名称"]')!.value).toBe(ma.name)
  expect(calls.save).not.toHaveBeenCalled()
  await click('确认删除')
  await click('保存方案')
  expect(calls.save.mock.calls[0][0].definitions).toEqual([macd])
  expect(calls.save.mock.calls[0][0].views).toEqual({ daily: [], minute: [] })
})

it('supports preview and save shortcuts without leaking them to the chart', async () => {
  await mount([])
  await click('创建空白指标')
  await input('公式源码', '均线:MA(C,5);')
  const source = document.querySelector<HTMLTextAreaElement>('[aria-label="公式源码"]')!
  const parentKeydown = vi.fn()
  document.addEventListener('keydown', parentKeydown)
  try {
    await act(async () => source.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', ctrlKey: true, bubbles: true, cancelable: true })))
    expect(calls.compile).toHaveBeenCalledOnce()
    await act(async () => source.dispatchEvent(new KeyboardEvent('keydown', { key: 's', metaKey: true, bubbles: true, cancelable: true })))
    expect(calls.save).toHaveBeenCalledOnce()
    expect(parentKeydown).not.toHaveBeenCalled()
  } finally { document.removeEventListener('keydown', parentKeydown) }
})

it('retains source after a network save failure without presenting it as a formula error', async () => {
  calls.save.mockRejectedValue(new ApiError('保存暂时不可用，请重试', 503))
  await mount([])
  await click('创建空白指标')
  await input('公式源码', '均线:MA(C,5);')
  await click('保存方案')
  expect(document.querySelector('[role="alert"]')!.textContent).toContain('保存暂时不可用')
  expect([...document.querySelectorAll('button')].some(button => button.textContent === '定位到公式')).toBe(false)
  expect(document.querySelector<HTMLTextAreaElement>('[aria-label="公式源码"]')!.value).toBe('均线:MA(C,5);')
  expect(close).not.toHaveBeenCalled()
})

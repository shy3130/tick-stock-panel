// @vitest-environment jsdom
import { act, type ReactNode } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import { api, ApiError } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import type {
  EngineInputCatalog,
  EngineJobPlan,
  EngineJobRecord,
  EngineJobRequest,
  EngineJobStatus,
  EnginePriceChoice,
  EngineResearchReport,
  EngineScheduleRecord,
} from '@/lib/engineJobTypes'
import { EngineResearch } from './Page'
import { JobForm } from './JobForm'
import { Report } from './Report'
import { decimalLabel, orderedStages } from './viewModel'
import * as viewModel from './viewModel'

vi.mock('@/components/Toast', () => ({ toast: vi.fn() }))

const request = {
  idempotency_key: 'original',
  nodes: [{ node_id: 'daily', run: 'run', snapshot: 'snapshot' }],
} satisfies EngineJobRequest
const plan: EngineJobPlan = {
  plan_version: '1.1',
  request,
  inputs: [],
  effective_budget: { max_attempts: 3, timeout_seconds: 60, rss_limit_bytes: 1024 ** 3 },
}
const captureRequest: EngineJobRequest = {
  idempotency_key: 'capture',
  nodes: [{ node_id: 'prices', capture: { datasets: [{
    kind: 'tsp_daily', expected_provider: 'desk', calendar_version: '2026.1.0',
    request: { dataset_id: 'daily-raw', record_type: 'bar', market_id: 'cn.equity',
      asset_class: 'equity', timeframe: '1d', price_basis: 'raw', instrument_ids: ['stock-1'],
      start: '2025-06-30T16:00:00Z', end: '2025-07-02T16:00:00Z' },
    symbols: [{ instrument_id: 'stock-1', symbol: '600000.SH', venue_id: 'XSHG' }],
  }] } }],
}
const catalog: EngineInputCatalog = {
  catalog_version: '1.0',
  truncated: false,
  issues: [],
  runs: [
    {
      path: 'run',
      archive_id: 'archive',
      lock_id: 'lock',
      snapshot_id: 'snapshot-a',
      strategy_id: 'test.strategy',
      start: '2025-01-02',
      end: '2025-01-08',
      universe_count: 2,
      price_view: { provider_id: 'builtin', mode: 'qfq' },
      accounting_model: 'event_ledger',
      semantics_version: '1.0',
      initial_capital: '5000',
      currency: 'CNY',
    },
  ],
  snapshots: [
    { path: 'matching', snapshot_id: 'snapshot-a', dataset_count: 4 },
    { path: 'unrelated', snapshot_id: 'snapshot-b', dataset_count: 9 },
  ],
}
function job(id: string, status: EngineJobStatus = 'succeeded'): EngineJobRecord {
  return {
    job_id: id,
    plan_id: id,
    status,
    created_at: '2026-11-18T00:00:00Z',
    updated_at: '2026-11-18T00:01:00Z',
    cancel_requested: false,
    artifact: null,
    nodes: Object.fromEntries(
      ['validate', 'execute', 'report'].map((stage) => [
        `daily.${stage}`,
        { status, attempts: [], artifact: null },
      ]),
    ),
  }
}
const metrics = {
  metrics_version: '1.0',
  calculation: 'daily_equity_decimal12_half_even',
  currency: 'CNY',
  initial_capital: '5000',
  final_equity: '5237.58',
  total_return: '0.047516',
  maximum_drawdown: '0.001004',
  filled_orders: 2,
}
function report(id: string, node = 'daily'): EngineResearchReport {
  return {
    format: 'tsp-engine-report',
    version: '1.0',
    plan_id: id,
    node_id: node,
    execution: { kind: 'result', identity: 'result', path: 'result' },
    summary: {
      metrics,
      semantics: {
        start: '2025-01-02',
        end: '2025-01-08',
        price_view: { mode: 'qfq' },
        accounting_model: 'event_ledger',
        execution: { fill_at: 'next_open', end_policy: 'mark_to_market' },
        knowledge_requirement: 'historical_versions',
        lock_id: `${id}-lock`,
      },
    },
  }
}
function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>((done) => {
    resolve = done
  })
  return { promise, resolve }
}

let host: HTMLDivElement
let root: Root
let client: QueryClient
beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  host = document.createElement('div')
  document.body.append(host)
  root = createRoot(host)
  client = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: Infinity }, mutations: { retry: false } },
  })
  vi.spyOn(api, 'engineInputs').mockResolvedValue(catalog)
  vi.spyOn(api, 'engineJobs').mockResolvedValue({ items: [job('a')] })
  vi.spyOn(api, 'engineJob').mockImplementation(async (id) => job(id))
  vi.spyOn(api, 'engineJobPlan').mockResolvedValue(plan)
  vi.spyOn(api, 'engineJobReport').mockImplementation(async (id, node) => report(id, node))
  vi.spyOn(api, 'previewEngineJob').mockImplementation(async (body) => ({
    plan_id: 'checked-id',
    plan: { ...plan, request: body },
  }))
  vi.spyOn(api, 'submitEngineJob').mockResolvedValue(job('new', 'queued'))
  vi.spyOn(api, 'cancelEngineJob').mockResolvedValue(job('a', 'cancelling'))
  vi.spyOn(api, 'retryEngineJob').mockResolvedValue(job('a', 'queued'))
})
afterEach(async () => {
  await act(async () => root.unmount())
  client.clear()
  host.remove()
  vi.restoreAllMocks()
  vi.useRealTimers()
})
async function flush() {
  await act(async () => {
    await new Promise((done) => setTimeout(done, 15))
  })
}
async function render(element: ReactNode = <EngineResearch />, url = '/research/engines') {
  await act(async () =>
    root.render(
      <MemoryRouter initialEntries={[url]} future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
        <QueryClientProvider client={client}>{element}</QueryClientProvider>
      </MemoryRouter>,
    ),
  )
  await flush()
  await flush()
  await flush()
}
const button = (label: string) => {
  const found = [...host.querySelectorAll('button')].find((element) => element.textContent?.trim() === label)
  expect(found, `button: ${label}`).toBeTruthy()
  return found as HTMLButtonElement
}
async function click(label: string) {
  await act(async () => button(label).click())
  await flush()
}
async function select(selector: string, value: string) {
  await act(async () => {
    const element = host.querySelector(selector) as HTMLSelectElement
    element.value = value
    element.dispatchEvent(new Event('change', { bubbles: true }))
  })
  await flush()
}
async function input(selector: string, value: string) {
  await act(async () => {
    const element = host.querySelector(selector) as HTMLInputElement
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!.call(element, value)
    element.dispatchEvent(new Event('input', { bubbles: true }))
  })
  await flush()
}
const form = (onCreated = vi.fn(), initial?: EngineJobRequest) => (
  <JobForm onCreated={onCreated} onClose={vi.fn()} initial={initial} />
)

test.each(['routed', 'signed'])('a checked %s daily source can create a paused schedule and open the delivered job', async kind => {
  const template: EngineJobRequest = kind === 'routed' ? captureRequest : {
    ...captureRequest,
    nodes: [{ node_id: 'prices', capture: { datasets: [{ kind: 'engine_data_provider', provider_id: 'desk.daily',
      request: captureRequest.nodes[0].capture!.datasets[0].request }] } }],
  }
  const preset = { version: 'test-calendar', preset_sha256: 'd'.repeat(64), venues: { XSHG: [2026] } }
  let saved: EngineScheduleRecord | undefined
  vi.spyOn(api, 'engineCalendarPresets').mockResolvedValue({ items: [preset] })
  vi.spyOn(api, 'engineSchedules').mockImplementation(async () => ({ items: saved ? [saved] : [] }))
  vi.spyOn(api, 'createEngineSchedule').mockImplementation(async body => (saved = {
    schedule_id: 'schedule-id', request: body, enabled: false, created_at: '2026-09-01T00:00:00Z',
    updated_at: '2026-09-01T00:00:00Z', next_due_at: '2026-09-30T07:30:00Z', last_error: null, last_delivery_id: null,
    calendar: { calendar_id: 'calendar', preset_sha256: preset.preset_sha256, calendar_sha256: 'c'.repeat(64), trading_days: 2, last_due_at: '2026-10-08T07:30:00Z' },
  }))
  vi.spyOn(api, 'engineSchedule').mockImplementation(async () => saved!)
  vi.spyOn(api, 'setEngineScheduleEnabled').mockImplementation(async (_id, enabled) => (saved = { ...saved!, enabled }))
  vi.spyOn(api, 'engineScheduleCalendar').mockResolvedValue({
    calendar_id: 'calendar', calendar_sha256: 'c'.repeat(64), preset_sha256: preset.preset_sha256,
    occurrences: [{ trading_date: '2026-09-30', due_at: '2026-09-30T07:30:00Z' }, { trading_date: '2026-10-08', due_at: '2026-10-08T07:30:00Z' }],
  })
  vi.spyOn(api, 'engineScheduleDeliveries').mockImplementation(async () => ({ items: saved?.enabled ? [{
    delivery_id: 'dispatch', schedule_id: 'schedule-id', due_at: '2026-09-30T07:30:00Z',
    trading_date: '2026-09-30', missed_count: 0, status: 'submitted', request_key: 'dispatch',
    plan_id: 'a', job_id: 'a', error: null, created_at: '2026-09-30T07:31:00Z', updated_at: '2026-09-30T07:31:00Z',
  }] : [] }))
  vi.mocked(api.previewEngineJob).mockResolvedValue({ plan_id: 'checked', plan: { ...plan, request: template }, trading_day_capture_nodes: ['prices'] })
  await render()
  await click('触发计划')
  await click('选择任务模板')
  await select('#engine-run', 'run')
  await click('检查输入与预算')
  await click('设为触发计划')
  await input('[name=schedule_key]', 'desk-close')
  await input('[name=start_date]', '2026-09-30')
  await input('[name=end_date]', '2026-10-08')
  await act(async () => host.querySelector<HTMLInputElement>('[type=checkbox]')!.click())
  await click('保存为暂停计划')
  expect(vi.mocked(api.createEngineSchedule).mock.lastCall?.[0]).toMatchObject({
    schedule_key: 'desk-close', schedule_version: '1.1',
    trigger: expect.objectContaining({ calendar_version: preset.version, preset_sha256: preset.preset_sha256, daily_capture_nodes: ['prices'] }),
    template: { nodes: template.nodes, budget: undefined },
  })
  expect(api.submitEngineJob).not.toHaveBeenCalled()
  expect(api.setEngineScheduleEnabled).not.toHaveBeenCalled()
  await click('查看派发日历')
  expect(host.querySelector('[aria-label="交易日期与派发时点"]')?.children).toHaveLength(2)
  await click('启用计划')
  expect(api.setEngineScheduleEnabled).toHaveBeenCalledWith('schedule-id', true)
  expect(host.textContent).toContain('已提交任务')
  await click('查看任务')
  expect(host.querySelector('[aria-label="研究详情"]')).toBeTruthy()
})

test('publication subscriptions resume from the saved position and link each version to its task', async () => {
  const triggerDataset = 'daily-raw'
  const version = 'c'.repeat(64)
  let saved: EngineScheduleRecord
  vi.spyOn(api, 'engineCalendarPresets').mockResolvedValue({ items: [] })
  vi.spyOn(api, 'engineSchedules').mockImplementation(async () => ({ items: saved ? [saved] : [] }))
  vi.spyOn(api, 'createEngineSchedule').mockImplementation(async body => (saved = {
    schedule_id: 'subscription', request: body, enabled: false, next_due_at: null,
    created_at: '2026-09-01T00:00:00Z', updated_at: '2026-09-01T00:00:00Z', last_delivery_id: null, last_error: null,
    publication_cursor: { format: 'tivelo-publication-cursor-v1', dataset_id: triggerDataset, stream_id: 'stream', event_id: null, sequence: 0 },
  }))
  vi.spyOn(api, 'engineSchedule').mockImplementation(async () => saved)
  vi.spyOn(api, 'setEngineScheduleEnabled').mockImplementation(async (_, enabled) => (saved = { ...saved, enabled, last_delivery_id: 'delivery' }))
  vi.spyOn(api, 'engineScheduleDeliveries').mockImplementation(async () => ({ items: saved?.last_delivery_id ? [{
    delivery_id: 'delivery', schedule_id: 'subscription', status: 'submitted', request_key: 'schedule.receipt', missed_count: 0,
    due_at: '2026-09-01T05:00:00Z', created_at: '2026-09-01T05:00:01Z', updated_at: '2026-09-01T05:00:02Z',
    plan_id: 'a', job_id: 'a', error: null,
    publication: { sequence: 1, event_id: 'receipt', dataset_id: triggerDataset, version_id: version, recorded_at: '2026-09-01T05:00:00Z' },
  }] : [] }))
  vi.mocked(api.previewEngineJob).mockResolvedValue({
    plan_id: 'checked', plan: { ...plan, request: captureRequest }, publication_datasets: [triggerDataset],
  })
  await render()
  await click('触发计划')
  await click('选择任务模板')
  await select('#engine-run', 'run')
  await click('检查输入与预算')
  await click('设为触发计划')
  await select('[name=trigger_kind]', 'data_publication')
  await input('[name=schedule_key]', 'bar-published')
  expect(host.textContent).toContain('暂停期间的版本')
  await click('保存为暂停计划')
  expect(api.createEngineSchedule).toHaveBeenCalledWith(expect.objectContaining({
    schedule_key: 'bar-published', schedule_version: '1.2', trigger: { kind: 'data_publication', dataset_id: triggerDataset },
  }), expect.anything())
  expect(host.textContent).toContain('已确认的发布序号')
  expect(button('启用计划').disabled).toBe(false)
  await click('启用计划')
  expect(host.textContent).toContain(version)
  await click('暂停计划')
  expect(button('启用计划').disabled).toBe(false)
  await click('查看任务')
  expect(host.querySelector('[aria-label="研究详情"]')).toBeTruthy()
})

function mockPriceEditor(state: EnginePriceChoice['state'] = 'available') {
  const source = { ...catalog.runs[0], semantics_version: 'strict.daily.v1',
    controlled_semantics: 'anchor_cancelled_float64_v1' as const,
    price_view: { provider_id: 'tivelo.qfq', mode: 'qfq' as const, anchor_at: '2025-01-07T00:00:00Z',
      factor_definition_id: 'fixture.cumulative', factor_baseline_id: 'fixture.listing' } }
  vi.mocked(api.engineInputs).mockResolvedValue({ ...catalog, runs: [source] })
  vi.spyOn(api, 'enginePriceViews').mockResolvedValue([
    { provider_id: 'tivelo.qfq', mode: 'qfq', state: 'available' },
    { provider_id: 'tivelo.hfq', mode: 'hfq', state: 'available' },
    { provider_id: 'tivelo.pro.dynamic', mode: 'extension', state },
  ])
  vi.spyOn(api, 'saveEnginePriceVariant').mockImplementation(async body => ({
    input: { ...source, path: 'price-variants/derived', lock_id: 'derived', price_view: body.price_view },
    snapshot: body.snapshot,
  }))
}

test('a dynamic price change requires explicit strategy consent and selects the saved archive', async () => {
  mockPriceEditor()
  await render(form())
  await select('#engine-run', 'run')
  await click('检查输入与预算')
  expect(host.textContent).toContain('提交研究任务')
  await click('更换价格口径')
  expect(button('检查输入与预算').disabled).toBe(true)
  expect(host.textContent).not.toContain('提交研究任务')
  await select('select[id$="-view"]', 'tivelo.pro.dynamic')
  expect(host.querySelector('input[id$="-anchor"]')).toBeNull()
  await click('另存回测输入')
  expect(api.saveEnginePriceVariant).not.toHaveBeenCalled()
  expect(host.querySelector('input[id$="-adapt"]')?.getAttribute('aria-invalid')).toBe('true')
  await act(async () => host.querySelector<HTMLInputElement>('input[id$="-adapt"]')!.click())
  await click('另存回测输入')
  expect(api.saveEnginePriceVariant).toHaveBeenCalledWith({ run: 'run', snapshot: 'matching', source_lock_id: 'lock',
    price_view: { mode: 'extension', provider_id: 'tivelo.pro.dynamic', factor_definition_id: 'fixture.cumulative',
      factor_baseline_id: 'fixture.listing' }, adapt_controlled: true })
  expect(host.textContent).toContain('已另存动态复权回测输入')
  expect((host.querySelector('#engine-run') as HTMLSelectElement).value).toBe('price-variants/derived')
  await click('检查输入与预算')
  expect(vi.mocked(api.previewEngineJob).mock.lastCall?.[0].nodes[0].run).toBe('price-variants/derived')
  expect(api.submitEngineJob).not.toHaveBeenCalled()
})

test('a copied plan keeps its steps and accepts a free fixed basis with a valid UTC anchor', async () => {
  mockPriceEditor('needs_license')
  await render(form(vi.fn(), request))
  await click('更换价格口径')
  await select('select[id$="-view"]', 'tivelo.pro.dynamic')
  expect(button('另存回测输入').disabled).toBe(true)
  expect(host.querySelector('a[href*="tab=import"]')).toBeTruthy()
  await select('select[id$="-view"]', 'tivelo.hfq')
  await input('input[id$="-anchor"]', '2025-02-31T01:00:00Z')
  await click('另存回测输入')
  expect(host.querySelector('input[id$="-anchor"]')?.getAttribute('aria-invalid')).toBe('true')
  expect(api.saveEnginePriceVariant).not.toHaveBeenCalled()
  await input('input[id$="-anchor"]', '2025-01-02T07:00:00Z')
  await click('另存回测输入')
  expect(api.saveEnginePriceVariant).toHaveBeenCalledWith({ run: 'run', snapshot: 'snapshot', source_lock_id: 'lock',
    price_view: { mode: 'hfq', provider_id: 'tivelo.hfq', anchor_at: '2025-01-02T07:00:00Z',
      factor_definition_id: 'fixture.cumulative', factor_baseline_id: 'fixture.listing' }, adapt_controlled: false })
  await click('检查输入与预算')
  expect(vi.mocked(api.previewEngineJob).mock.lastCall?.[0].nodes).toEqual([
    { node_id: 'daily', run: 'price-variants/derived', snapshot: 'snapshot' },
  ])
  expect(request.nodes[0].run).toBe('run')
})

test('failure to read installed extensions still allows saving a free raw input', async () => {
  mockPriceEditor()
  vi.mocked(api.enginePriceViews).mockRejectedValue(new Error('unavailable'))
  await render(form())
  await select('#engine-run', 'run')
  await click('更换价格口径')
  expect(host.textContent).toContain('扩展状态读取失败')
  await select('select[id$="-view"]', 'tivelo.raw')
  await click('另存回测输入')
  expect(vi.mocked(api.saveEnginePriceVariant).mock.lastCall?.[0].price_view).toEqual({ mode: 'raw', provider_id: 'tivelo.raw' })
})

test('loading, empty tasks and missing inputs offer a reachable create flow', async () => {
  const pending = deferred<{ items: EngineJobRecord[] }>()
  vi.mocked(api.engineJobs).mockReturnValue(pending.promise)
  vi.mocked(api.engineInputs).mockResolvedValue({ ...catalog, runs: [], snapshots: [] })
  await render()
  expect(host.textContent).toContain('正在读取任务列表')
  await act(async () => pending.resolve({ items: [] }))
  await flush()
  expect(host.textContent).toContain('还没有研究任务')
  await click('新建任务')
  expect(host.textContent).toContain('尚无可选回测归档')
  await click('检查输入与预算')
  expect(host.querySelector('#engine-run')?.getAttribute('aria-invalid')).toBe('true')
  expect(api.previewEngineJob).not.toHaveBeenCalled()
})

test('only matching snapshots are selectable; checking does not submit and changes invalidate the check', async () => {
  await render(form())
  await select('#engine-run', 'run')
  expect((host.querySelector('#engine-snapshot') as HTMLSelectElement).value).toBe('matching')
  expect(host.querySelector('#engine-snapshot')?.textContent).not.toContain('unrelated')
  await click('检查输入与预算')
  expect(api.submitEngineJob).not.toHaveBeenCalled()
  expect(button('提交研究任务')).toBeTruthy()
  const key = vi.mocked(api.previewEngineJob).mock.calls[0][0].idempotency_key
  await input('#engine-memory', '512')
  expect(host.textContent).not.toContain('提交研究任务')
  await click('检查输入与预算')
  const checked = vi.mocked(api.previewEngineJob).mock.calls[1][0]
  expect(checked.idempotency_key).not.toBe(key)
  expect(checked.budget?.rss_limit_bytes).toBe(512 * 1024 ** 2)
})

test('network retry retains the checked request; conflict requires a new check', async () => {
  vi.mocked(api.submitEngineJob)
    .mockRejectedValueOnce(new Error('连接断开'))
    .mockRejectedValueOnce(new ApiError('输入已变更', 409))
  await render(form())
  await select('#engine-run', 'run')
  await click('检查输入与预算')
  await click('提交研究任务')
  expect(host.textContent).toContain('连接断开')
  await click('提交研究任务')
  const calls = vi.mocked(api.submitEngineJob).mock.calls
  expect(calls[1]).toEqual(calls[0])
  expect(calls[0][1]).toBe('checked-id')
  expect(host.textContent).toContain('输入已变更')
  expect(host.textContent).not.toContain('提交研究任务')
})

test('in-flight submission disables duplicates and forwards the acknowledged job once', async () => {
  const pending = deferred<EngineJobRecord>()
  const created = vi.fn()
  vi.mocked(api.submitEngineJob).mockReturnValue(pending.promise)
  await render(form(created))
  await select('#engine-run', 'run')
  await click('检查输入与预算')
  await click('提交研究任务')
  expect(button('正在提交…').disabled).toBe(true)
  expect(button('返回任务').disabled).toBe(true)
  await click('正在提交…')
  expect(api.submitEngineJob).toHaveBeenCalledTimes(1)
  await act(async () => pending.resolve(job('new', 'queued')))
  await flush()
  expect(created).toHaveBeenCalledExactlyOnceWith(job('new', 'queued'))
})

test('copied plans keep all research parameters but use a fresh idempotency key', async () => {
  const copied = {
    ...request,
    nodes: [
      {
        ...request.nodes[0],
        study: {
          mode: 'grid' as const,
          search: {
            target: 'controlled_program' as const,
            axes: [{ name: 'weight', values: ['0.5', '0.75'] }],
          },
        },
      },
    ],
  }
  await render(form(vi.fn(), copied))
  await click('检查输入与预算')
  const checked = vi.mocked(api.previewEngineJob).mock.calls[0][0]
  expect(checked.nodes).toEqual(copied.nodes)
  expect(checked.idempotency_key).not.toBe('original')
  expect(host.textContent).toContain('参数搜索')
  expect(host.textContent).toContain('查看完整检查计划')
})

test('capture preview identifies the source and does not advertise a strategy run', async () => {
  await render(form(vi.fn(), captureRequest))
  await click('检查输入与预算')
  const checked = host.querySelector('[aria-label="已检查的研究计划"]')
  expect(checked?.textContent).toContain('采集数据快照')
  expect(checked?.textContent).toContain('daily-raw · desk')
  expect(checked?.textContent).not.toContain('单次回测')
})

test('legacy repository plans display the declared local source and retain the price basis', async () => {
  const source = captureRequest.nodes[0].capture!.datasets[0]
  if (source.kind !== 'tsp_daily') throw new Error('expected daily fixture')
  const archive: EngineJobRequest = {
    idempotency_key: 'legacy',
    nodes: [{ node_id: 'prices', capture: { datasets: [{
      ...source, kind: 'tsp_repository_daily', layout: 'enriched_v1', expected_provider: 'local_archive',
      request: { ...source.request, dataset_id: 'daily-qfq', price_basis: 'qfq' },
    }] } }],
  }
  await render(form(vi.fn(), archive))
  await click('检查输入与预算')
  expect(host.querySelector('[aria-label="已检查的研究计划"]')?.textContent).toContain('daily-qfq · local_archive')
  expect(vi.mocked(api.previewEngineJob).mock.calls[0][0].nodes).toEqual(archive.nodes)
})

test('repository minute previews retain the label and the selected price evidence', async () => {
  const archive: EngineJobRequest = {
    idempotency_key: 'archive-minute',
    nodes: [{ node_id: 'prices', capture: { datasets: [{
      kind: 'tsp_repository_minute', layout: 'minute_v1', expected_provider: 'local_archive',
      minute_label: 'end', price_evidence: 'legacy_close_anchor', calendar_version: '2026.1.0',
      symbols: [{ instrument_id: 'security-A', symbol: '600000.SH', venue_id: 'XSHG' }],
      request: {
        dataset_id: 'stock-minute', record_type: 'bar', market_id: 'cn.equity', asset_class: 'equity',
        instrument_ids: ['security-A'], timeframe: '1m', price_basis: 'raw',
        start: '2025-07-01T09:30:00+08:00', end: '2025-07-01T15:00:00+08:00',
      },
    }] } }],
  }
  await render(form(vi.fn(), archive))
  await click('检查输入与预算')
  expect(host.querySelector('[aria-label="已检查的研究计划"]')?.textContent).toContain('stock-minute · local_archive')
  expect(vi.mocked(api.previewEngineJob).mock.calls[0][0].nodes).toEqual(archive.nodes)
})

test.each([
  { kind: 'tsp_repository_factors', layout: 'event_factors_v1', expected_provider: 'local_archive' } as const,
  { kind: 'tsp_factors', expected_provider: 'dividends' } as const,
])('factor imports retain the fixed baseline in the $kind preview', async (source) => {
  const archive: EngineJobRequest = {
    idempotency_key: 'archive-factors',
    nodes: [{ node_id: 'prices', capture: { datasets: [{
      ...source,
      representation: 'cumulative_forward' as const, baseline_date: '2025-07-01',
      symbols: [{ instrument_id: 'security-A', symbol: '600000.SH', venue_id: 'XSHG' as const }],
      request: {
        dataset_id: 'stock-factors', record_type: 'adjustment_factor', market_id: 'cn.equity' as const,
        asset_class: 'equity', instrument_ids: ['security-A'],
        start: '2025-07-01T00:00:00+08:00', end: '2025-07-04T00:00:00+08:00',
      },
    }] } }],
  }
  await render(form(vi.fn(), archive))
  await click('检查输入与预算')
  expect(host.querySelector('[aria-label="已检查的研究计划"]')?.textContent).toContain(`stock-factors · ${source.expected_provider}`)
  expect(vi.mocked(api.previewEngineJob).mock.calls[0][0].nodes).toEqual(archive.nodes)
})

test('previews an imported signed data source using its provider id', async () => {
  const source = captureRequest.nodes[0].capture!.datasets[0]
  const archive: EngineJobRequest = { ...captureRequest, nodes: [{ node_id: 'prices', capture: { datasets: [{
    kind: 'engine_data_provider', provider_id: 'desk.raw-bars', profile: 'default', request: source.request,
  }] } }] }
  await render(form(vi.fn(), archive))
  await click('检查输入与预算')
  expect(host.querySelector('[aria-label="已检查的研究计划"]')?.textContent).toContain(source.request.dataset_id + ' · desk.raw-bars')
  expect(vi.mocked(api.previewEngineJob).mock.calls[0][0].nodes).toEqual(archive.nodes)
})

test('capture results show snapshot identity and coverage without querying for a report', async () => {
  vi.mocked(api.engineJobPlan).mockResolvedValue({ ...plan, plan_version: '1.2', request: captureRequest })
  const completed = { ...job('a'), nodes: { 'prices.snapshot': {
    status: 'succeeded' as const, attempts: [], artifact: null,
  } } }
  vi.mocked(api.engineJob).mockResolvedValue(completed)
  vi.mocked(api.engineJobs).mockResolvedValue({ items: [completed] })
  const getSnapshot = vi.spyOn(api, 'engineJobSnapshot').mockResolvedValue({
    snapshot_id: 'frozen-id', bundle_id: 'export-id', path: 'jobs/a/output',
    datasets: [{ dataset_id: 'daily-raw', version_id: 'v1', record_type: 'bar', scope: {},
      knowledge_basis: 'latest_reconstruction', price_basis: 'raw', source_ids: ['desk'],
      coverage: { status: 'partial', requested_units: 2, completed_units: 1, row_count: 1 } }],
  })
  await render(<EngineResearch />, '/research/engines?job=a&node=prices')
  expect(getSnapshot).toHaveBeenCalledWith('a', 'prices')
  expect(api.engineJobReport).not.toHaveBeenCalled()
  expect(host.textContent).toContain('快照标识 frozen-id')
  expect(host.textContent).toContain('1/2 个覆盖单位')
  expect(host.textContent).toContain('按采集时点重建')
})

test.each([401, 403, 503])(
  'input service rejection %s disables form actions with a readable error',
  async (status) => {
    vi.mocked(api.engineInputs).mockRejectedValue(new ApiError('refused', status))
    await render(form())
    expect(host.querySelector('[role=alert]')).toBeTruthy()
    expect(button('检查输入与预算').disabled).toBe(true)
    expect(api.previewEngineJob).not.toHaveBeenCalled()
  },
)

test('oversized integer budgets are rejected before serialization loses precision', async () => {
  await render(form())
  await select('#engine-run', 'run')
  await input('#engine-memory', '9007199254740991')
  await click('检查输入与预算')
  expect(host.querySelector('#engine-memory')?.getAttribute('aria-invalid')).toBe('true')
  expect(api.previewEngineJob).not.toHaveBeenCalled()
})

test('switching jobs cannot display a late report for the previous job', async () => {
  const pending = deferred<EngineResearchReport>()
  vi.mocked(api.engineJobs).mockResolvedValue({ items: [job('a'), job('b')] })
  vi.mocked(api.engineJobReport).mockImplementation((id, node) =>
    id === 'a'
      ? pending.promise
      : Promise.resolve({
          ...report(id, node),
          summary: { ...report(id, node).summary, metrics: { ...metrics, final_equity: '7777.77' } },
        }),
  )
  await render()
  expect(host.textContent).toContain('正在校验并读取报告')
  await act(async () =>
    [...host.querySelectorAll('aside ul button')]
      .find((element) => element.querySelector('code')?.textContent === 'b')!
      .dispatchEvent(new MouseEvent('click', { bubbles: true })),
  )
  await flush()
  await flush()
  expect(host.textContent).toContain('7,777.77')
  await act(async () => pending.resolve(report('a')))
  await flush()
  expect(host.textContent).not.toContain('5,237.58')
  expect(host.textContent).toContain('7,777.77')
  expect(client.getQueryData(QK.engineJobReport('a', 'daily'))).toBeTruthy()
})

test('result download keeps the report visible and permits retry after a rejected archive', async () => {
  const pending = deferred<Blob>()
  const transfer = vi.spyOn(api, 'engineJobResultZip')
    .mockRejectedValueOnce(new ApiError('执行制品与清单摘要不一致', 409))
    .mockReturnValueOnce(pending.promise)
  const save = vi.spyOn(viewModel, 'downloadFile').mockImplementation(() => undefined)
  await render(<Report report={report('a', 'daily.report')} />)
  await click('导出结果 ZIP')
  expect(host.querySelector('[role=alert]')?.textContent).toContain('执行制品与清单摘要不一致')
  expect(host.textContent).toContain('5,237.58')
  await click('导出结果 ZIP')
  expect(button('正在校验并打包').disabled).toBe(true)
  expect(transfer.mock.calls).toEqual([['a', 'daily'], ['a', 'daily']])
  const archive = new Blob(['zip'], { type: 'application/zip' })
  await act(async () => pending.resolve(archive))
  await flush()
  expect(save).toHaveBeenCalledExactlyOnceWith(archive, 'tivelo-result-result.zip')
  expect(host.querySelector('[role=alert]')).toBeNull()
})

test('background list changes retain the selected job; background errors retain its last good data', async () => {
  await render()
  expect(host.textContent).toContain('5,237.58')
  vi.mocked(api.engineJobs).mockResolvedValue({ items: [job('newer'), job('a')] })
  await click('刷新')
  await flush()
  expect(api.engineJob).not.toHaveBeenCalledWith('newer')
  vi.mocked(api.engineJobs).mockRejectedValue(new Error('列表断网'))
  vi.mocked(api.engineJob).mockRejectedValue(new Error('详情断网'))
  await click('刷新')
  await flush()
  expect(host.textContent).toContain('保留上次读取的列表')
  expect(host.textContent).toContain('当前显示上次读取的状态')
  expect(host.textContent).toContain('5,237.58')
})

test('an invalid deep-link node never silently displays another report and can be corrected', async () => {
  await render(<EngineResearch />, '/research/engines?job=a&node=missing')
  expect(api.engineJobReport).not.toHaveBeenCalled()
  expect(host.textContent).toContain('链接中的研究步骤不存在')
  await select('#research-node', 'daily')
  await flush()
  expect(api.engineJobReport).toHaveBeenCalledWith('a', 'daily')
})

test('100 percent worker progress is not a published report; cancellation waits for confirmed state', async () => {
  const running = job('a', 'running')
  running.nodes['daily.execute'].attempts = [
    {
      number: 1,
      token: 'token',
      status: 'running',
      started_at: running.created_at,
      finished_at: null,
      progress: { done: 3, total: 3, phase: 'compute' },
      artifact: null,
      error: null,
    },
  ]
  vi.mocked(api.engineJob).mockResolvedValue(running)
  await render()
  expect(host.textContent).toContain('compute · 3 / 3')
  expect(api.engineJobReport).not.toHaveBeenCalled()
  vi.mocked(api.engineJob).mockResolvedValue(job('a', 'cancelling'))
  await click('取消任务')
  await flush()
  expect(api.cancelEngineJob).toHaveBeenCalledExactlyOnceWith('a')
  expect(button('正在取消…').disabled).toBe(true)
  expect(host.textContent).toContain('正在等待执行进程停止')
})

test('retry keeps the original job and attempt history, and does not create a new plan', async () => {
  const failed = job('a', 'failed')
  failed.nodes['daily.execute'].attempts = [
    {
      number: 1,
      token: 'old',
      status: 'failed',
      started_at: failed.created_at,
      finished_at: failed.updated_at,
      progress: null,
      artifact: null,
      error: { code: 'RUNTIME_MISMATCH' },
    },
  ]
  vi.mocked(api.engineJob).mockResolvedValue(failed)
  await render()
  expect(host.textContent).toContain('RUNTIME_MISMATCH')
  expect(host.textContent).toContain('运行环境与锁定版本不同')
  vi.mocked(api.engineJob).mockResolvedValue(job('a', 'queued'))
  await click('按原输入重试')
  expect(api.retryEngineJob).toHaveBeenCalledExactlyOnceWith('a')
  expect(api.submitEngineJob).not.toHaveBeenCalled()
  expect(api.previewEngineJob).not.toHaveBeenCalled()
})

test('a failed source shows the physical row and fields with instructions to capture corrected data', async () => {
  const failed = job('a', 'failed')
  const source = captureRequest.nodes[0].capture!.datasets[0]
  if (source.kind !== 'tsp_daily') throw new Error('expected daily fixture')
  const archive: EngineJobRequest = { ...captureRequest, nodes: [{ node_id: 'prices', capture: { datasets: [{
    ...source, kind: 'tsp_repository_daily', layout: 'enriched_v1', expected_provider: 'local_archive',
  }] } }] }
  const location = {
    layout: 'enriched_v1' as const, trading_date: '2025-07-01', symbol: '600000.SH',
    row_number: 2051, file_sha256: 'ab'.repeat(32),
  }
  failed.nodes = { 'prices.sync_00': { status: 'failed', artifact: null, attempts: [{
    number: 1, token: 'old', status: 'failed', started_at: failed.created_at,
    finished_at: failed.updated_at, progress: null, artifact: null,
    error: { code: 'CONTRACT_INVALID', source_issues: [
      { ...location, field: 'raw_close', reason: 'fractional_precision' },
      { ...location, field: 'amount', reason: 'integral_range' },
    ] },
  }] } }
  vi.mocked(api.engineJobPlan).mockResolvedValue({ ...plan, plan_version: '1.2', request: archive })
  vi.mocked(api.engineJob).mockResolvedValue(failed)
  await render()
  expect(button('按原输入重试').disabled).toBe(true)
  expect(button('复制为新任务').disabled).toBe(false)
  expect(host.textContent).toContain('2025-07-01 的 600000.SH')
  expect(host.textContent).toContain('修正来源后请重新预检并提交新计划')
  const list = host.querySelector('[aria-label="源数据问题"]')!
  expect(list.children).toHaveLength(1)
  expect(list.textContent).toContain('第 2051 行')
  expect(list.textContent).toContain('raw_close，有效小数超过 12 位')
  expect(list.textContent).toContain('amount，整数部分超过 26 位')
  expect(list.textContent).toContain('kline_daily_enriched 分区 SHA-256 ' + location.file_sha256)
  const stage = failed.nodes['prices.sync_00']
  vi.mocked(api.engineJob).mockResolvedValue({
    ...failed, nodes: { 'prices.sync_00': { ...stage, attempts: [{
      ...stage.attempts[0], error: { code: 'DATA_COVERAGE_INSUFFICIENT', source_issues: [
        { ...location, field: 'quote_ts', reason: 'quote_before_finalization' },
      ] },
    }] } },
  })
  await click('刷新')
  await flush()
  expect(host.textContent).toContain('缺少已定版日线')
  expect(host.textContent).toContain('quote_ts，报价早于北京时间 15:30 定版线')
  expect(host.textContent).toContain('补采后请重新预检并提交新任务')
  vi.mocked(api.engineJob).mockResolvedValue({
    ...failed, nodes: { 'prices.sync_00': { ...stage, attempts: [{
      ...stage.attempts[0], error: { code: 'CONTRACT_INVALID', source_issues: [
        { ...location, layout: 'event_factors_v1', field: 'ex_factor', reason: 'fractional_precision' },
      ] },
    }] } },
  })
  await click('刷新')
  await flush()
  expect(host.textContent).toContain('原始除权比率超出可保存精度')
  expect(host.textContent).toContain('ex_factor，有效小数超过 12 位')
  expect(host.textContent).toContain('adj_factor/all.parquet SHA-256 ' + location.file_sha256)
})

test('exhausted attempts disable retries but allow a reviewed new task', async () => {
  const failed = job('a', 'failed')
  failed.nodes['daily.execute'].attempts = Array.from({ length: 3 }, (_, index) => ({
    number: index + 1,
    token: String(index),
    status: 'failed',
    started_at: failed.created_at,
    finished_at: failed.updated_at,
    progress: null,
    artifact: null,
    error: { code: 'RESOURCE_LIMIT' },
  }))
  vi.mocked(api.engineJob).mockResolvedValue(failed)
  await render()
  expect(button('按原输入重试').disabled).toBe(true)
  await click('复制为新任务')
  expect(host.textContent).toContain('从已有任务复制的研究计划')
  expect(api.submitEngineJob).not.toHaveBeenCalled()
})

test('unknown report versions never present financial numbers as supported results', async () => {
  await render(<Report report={{ ...report('a'), version: '2.0' } as unknown as EngineResearchReport} />)
  expect(host.textContent).toContain('报告版本不受当前页面支持')
  expect(host.textContent).not.toContain('5,237.58')
})

test('walk-forward reports explain account resets and expose winner across candidate pages', async () => {
  const candidates = Array.from({ length: 26 }, (_, index) => ({
    candidate_id: `candidate-${index}`,
    parameters: { weight: String(index) },
  }))
  const value = report('study')
  value.summary = {
    ...value.summary,
    metrics: undefined,
    study: {
      plan_version: '1.0',
      request: { mode: 'walk_forward' },
      candidates,
      windows: [
        {
          train_start: '2025-01-02',
          train_end: '2025-01-03',
          test_start: '2025-01-06',
          test_end: '2025-01-06',
        },
      ],
    },
    report: {
      report_version: '1.0',
      aggregate_method: 'independent_fold_return_product',
      oos_compounded_return: '0.047516',
      windows: [
        {
          winner_candidate_id: 'candidate-25',
          training: candidates.map((candidate) => ({ candidate_id: candidate.candidate_id, metrics })),
          test: { metrics },
        },
      ],
    },
  }
  await render(<Report report={value} />)
  expect(host.textContent).toContain('各窗口重置账户')
  expect(host.textContent).toContain('不代表连续持仓账户的收益')
  expect(host.textContent).toContain('4.7516%')
  expect(host.querySelectorAll('tbody tr')).toHaveLength(25)
  await click('查看选中候选')
  expect(host.querySelectorAll('tbody tr')).toHaveLength(1)
  expect(host.textContent).toContain('训练选中')
})

test.each([
  ['9007199254740993.01', false, '9,007,199,254,740,993.01'],
  ['0.047516', true, '4.7516%'],
  ['1e-8', true, '0.000001%'],
  ['-0E-12', false, '0'],
  ['-2.5000e3', false, '-2,500'],
  ['1e999', false, '未提供'],
  [0.047516, true, '未提供'],
])('decimal presentation preserves exact strings: %s', (value, percentage, expected) =>
  expect(decimalLabel(value, percentage)).toBe(expected),
)

test('dataset stages stay in dependency order rather than alphabetical order', () => {
  const value = job('ordered')
  value.nodes = Object.fromEntries(
    [
      'report',
      'publish_01',
      'sync_00',
      'prepare',
      'sync_01',
      'publish_00',
      'execute',
      'snapshot',
      'validate',
    ].map((stage) => [`daily.${stage}`, { status: 'queued', attempts: [], artifact: null }]),
  )
  expect(orderedStages(value).map(([id]) => id)).toEqual(
    [
      'sync_00',
      'publish_00',
      'sync_01',
      'publish_01',
      'snapshot',
      'prepare',
      'validate',
      'execute',
      'report',
    ].map((stage) => `daily.${stage}`),
  )
})

test('imported files use the reviewed path; malformed files never reach the API', async () => {
  await render(form())
  const picker = host.querySelector('#engine-plan-file') as HTMLInputElement
  async function upload(contents: string) {
    const file = new File([contents], 'research.json', { type: 'application/json' })
    Object.defineProperty(file, 'text', { value: async () => contents })
    await act(async () => {
      Object.defineProperty(picker, 'files', { configurable: true, value: [file] })
      picker.dispatchEvent(new Event('change', { bubbles: true }))
    })
    await flush()
  }
  await upload('{"nodes":[]}')
  expect(host.textContent).toContain('文件不是可识别的研究计划')
  expect(api.previewEngineJob).not.toHaveBeenCalled()
  await upload(JSON.stringify(request))
  await click('检查输入与预算')
  expect(vi.mocked(api.previewEngineJob).mock.calls[0][0].nodes).toEqual(request.nodes)
  expect(vi.mocked(api.previewEngineJob).mock.calls[0][0].idempotency_key).not.toBe(request.idempotency_key)
  expect(host.textContent).toContain('research.json')
})

test('losing report access hides a previously cached financial result', async () => {
  await render()
  expect(host.textContent).toContain('5,237.58')
  vi.mocked(api.engineJobReport).mockRejectedValue(new ApiError('denied', 403))
  await click('刷新')
  await flush()
  expect(host.textContent).toContain('当前账号没有此操作的权限')
  expect(host.textContent).not.toContain('5,237.58')
})

test('a late preflight response after leaving the form cannot replace the selected task', async () => {
  const pending = deferred<Awaited<ReturnType<typeof api.previewEngineJob>>>()
  vi.mocked(api.previewEngineJob).mockReturnValue(pending.promise)
  await render()
  await click('新建任务')
  await select('#engine-run', 'run')
  await click('检查输入与预算')
  await act(async () => (host.querySelector('aside ul button') as HTMLButtonElement).click())
  await flush()
  await act(async () => pending.resolve({ plan_id: 'late', plan }))
  await flush()
  expect(host.textContent).toContain('研究报告')
  expect(host.textContent).not.toContain('提交研究任务')
  expect(api.submitEngineJob).not.toHaveBeenCalled()
})

test('list and detail share the newest record when independent polls arrive out of order', async () => {
  const old = { ...job('a', 'queued'), updated_at: '2026-11-18T00:00:00Z' }
  vi.mocked(api.engineJobs).mockResolvedValue({ items: [old] })
  await render()
  expect(host.querySelector('aside')?.textContent).toContain('3 / 3 个阶段已完成')
  expect(host.querySelector('aside')?.textContent).not.toContain('排队中')
  const latest = { ...job('a', 'failed'), updated_at: '2026-11-18T00:02:00Z' }
  vi.mocked(api.engineJobs).mockResolvedValue({ items: [latest] })
  await click('刷新')
  await flush()
  expect(host.querySelector('article [role=status]')?.textContent).toBe('失败')
  expect(host.textContent).not.toContain('5,237.58')
})

test('a rejected action can recover after a successful service and permission recheck', async () => {
  vi.mocked(api.engineJob).mockResolvedValue(job('a', 'failed'))
  vi.mocked(api.retryEngineJob).mockRejectedValueOnce(new ApiError('unavailable', 503))
  await render()
  await click('按原输入重试')
  expect(button('按原输入重试').disabled).toBe(true)
  await click('重新检查服务与权限')
  expect(button('按原输入重试').disabled).toBe(false)
  vi.mocked(api.engineJob).mockResolvedValue(job('a', 'queued'))
  await click('按原输入重试')
  expect(api.retryEngineJob).toHaveBeenCalledTimes(2)
  expect(api.submitEngineJob).not.toHaveBeenCalled()
})

test('old report metadata remains readable without inventing missing run dates', async () => {
  const legacy = report('legacy')
  legacy.summary = { metrics }
  await render(<Report report={legacy} />)
  expect(host.textContent).toContain('5,237.58')
  expect(host.textContent).toContain('未提供 至 未提供')
  expect(host.textContent).toContain('未知账户模型')
})

test('continuous reports display witnessed fills and checkpoint metrics', async () => {
  const continuous = report('tape')
  continuous.summary = {
    ...continuous.summary,
    semantics: { execution: { fill_at: 'witnessed_trade', end_policy: 'mark_to_market' } },
    metrics: { ...metrics, calculation: 'checkpoint_equity_decimal12_half_even' },
  }
  await render(<Report report={continuous} />)
  expect(host.textContent).toContain('按逐笔报告确认')
  expect(host.textContent).toContain('成交委托数')
  expect(host.textContent).toContain('5,237.58')
})

test('unknown metric calculations are not rendered as the current financial convention', async () => {
  const unknown = report('unknown')
  unknown.summary = { metrics: { ...metrics, calculation: 'unrecognized' } }
  await render(<Report report={unknown} />)
  expect(host.textContent).toContain('此指标版本暂不支持展示')
  expect(host.textContent).not.toContain('5,237.58')
})

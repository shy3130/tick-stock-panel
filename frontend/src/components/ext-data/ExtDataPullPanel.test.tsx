// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { api, type ExtDataConfig } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { ExtDataPullPanel } from './ExtDataPullPanel'
import { formatIntervalSummary } from './dialogShared'

vi.mock('@/lib/api', () => ({ api: {
  extDataPullRun: vi.fn(), extDataPullConfig: vi.fn(), extDataApiKey: vi.fn(),
  extDataApiKeySet: vi.fn(), extDataPullTest: vi.fn(),
} }))

const config: ExtDataConfig = {
  id: 'sparse', label: '稀疏数据', mode: 'timeseries', fields: [],
  created_at: '', updated_at: '',
  pull: {
    url: 'https://example.test/sparse', method: 'GET', response_path: 'items',
    schedule_minutes: 1440, timeout_seconds: 30, enabled: false,
  },
}
const auctionKey = QK.extSeries('auction:intraday:sparse:price', '2026-11-06', '600000.SH')
const otherKeys = [QK.klineMinute('600000.SH', '2026-11-06'), QK.preferences]
let host: HTMLDivElement
let root: Root
let client: QueryClient

beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  vi.clearAllMocks()
  vi.mocked(api.extDataApiKey).mockResolvedValue({ key_set: false, masked_key: '' })
  vi.mocked(api.extDataPullConfig).mockResolvedValue({ status: 'ok', pull: config.pull! })
  vi.mocked(api.extDataApiKeySet).mockResolvedValue({ status: 'ok', key_set: true, masked_key: 'test...key' })
  host = document.createElement('div')
  document.body.append(host)
  root = createRoot(host)
  client = new QueryClient({ defaultOptions: { queries: { staleTime: Infinity, retry: false } } })
  for (const key of [auctionKey, QK.extDataSchemaAll, QK.extSchemaAll,
    QK.extDataRows('sparse'), QK.watchlistEnriched('sparse.price'),
    QK.screenerCached('2026-11-06', 'sparse.price'),
    QK.kline('600000.SH', '2026-01-01', '2026-11-06', 'sparse.price'), ...otherKeys]) {
    client.setQueryData(key, {})
  }
})

afterEach(async () => {
  await act(async () => root.unmount())
  client.clear()
  host.remove()
})

async function run(onSaved = vi.fn()) {
  await act(async () => root.render(<QueryClientProvider client={client}>
    <ExtDataPullPanel config={config} onSaved={onSaved} />
  </QueryClientProvider>))
  const button = [...host.querySelectorAll('button')].find(b => b.textContent?.includes('立即执行'))!
  await act(async () => button.click())
  return onSaved
}

it('refreshes cached empty auction series, schemas and list values after a successful pull', async () => {
  vi.mocked(api.extDataPullRun).mockResolvedValue({ status: 'ok', rows: 601, date: '2026-11-06' })
  const onSaved = await run()

  expect(onSaved).toHaveBeenCalledOnce()
  expect(client.getQueryState(auctionKey)?.isInvalidated).toBe(true)
  expect(client.getQueryState(QK.extDataSchemaAll)?.isInvalidated).toBe(true)
  expect(client.getQueryState(QK.extSchemaAll)?.isInvalidated).toBe(true)
  expect(client.getQueryState(QK.watchlistEnriched('sparse.price'))?.isInvalidated).toBe(true)
  expect(client.getQueryState(QK.screenerCached('2026-11-06', 'sparse.price'))?.isInvalidated).toBe(true)
  expect(client.getQueryState(QK.kline('600000.SH', '2026-01-01', '2026-11-06', 'sparse.price'))?.isInvalidated).toBe(true)
  for (const key of otherKeys) expect(client.getQueryState(key)?.isInvalidated).toBe(false)
})

it('keeps cached data intact when pulling fails', async () => {
  vi.mocked(api.extDataPullRun).mockRejectedValue(new Error('拉取失败'))
  const onSaved = await run()

  expect(host.textContent).toContain('拉取失败')
  expect(onSaved).not.toHaveBeenCalled()
  expect(client.getQueryState(auctionKey)?.isInvalidated).toBe(false)
})

describe('formatIntervalSummary (与后端 _interval_seconds 同口径)', () => {
  it('秒级且非整分 → 显示秒', () => {
    expect(formatIntervalSummary(30, 1)).toBe('每 30 秒')
    expect(formatIntervalSummary(90, 2)).toBe('每 90 秒')
    expect(formatIntervalSummary(10, 1)).toBe('每 10 秒')
  })

  it('整分 → 显示分钟', () => {
    expect(formatIntervalSummary(60, 1)).toBe('每 1 分钟')
    expect(formatIntervalSummary(300, 5)).toBe('每 5 分钟')
  })

  it('schedule_seconds 缺失 → 回退分钟; 两者皆缺 → 默认 1440 分钟', () => {
    expect(formatIntervalSummary(null, 5)).toBe('每 5 分钟')
    expect(formatIntervalSummary(undefined, undefined)).toBe('每 1440 分钟')
  })
})

it('卡片摘要显示秒级间隔 (schedule_seconds 优先于 schedule_minutes)', async () => {
  const secondsConfig: ExtDataConfig = {
    ...config,
    pull: { ...config.pull!, enabled: true, schedule_minutes: 1, schedule_seconds: 30 },
  }
  await act(async () => root.render(<QueryClientProvider client={client}>
    <ExtDataPullPanel config={secondsConfig} onSaved={vi.fn()} />
  </QueryClientProvider>))

  expect(host.textContent).toContain('每 30 秒')
  expect(host.textContent).not.toContain('每 1 分钟')
})

async function clickConfigButton(label: string) {
  const button = [...host.querySelectorAll('button')].find(el => el.textContent?.trim() === label)!
  expect(button).toBeTruthy()
  await act(async () => button.click())
}

async function editSeconds(value: number) {
  const input = host.querySelector<HTMLInputElement>('input[type=number][min="10"]')!
  expect(input).toBeTruthy()
  await act(async () => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!.call(input, String(value))
    input.dispatchEvent(new Event('input', { bubbles: true }))
  })
}

it.each([3, 10.5])('shows an invalid draft interval of %s seconds without clamping or rounding it', async value => {
  await act(async () => root.render(<QueryClientProvider client={client}>
    <ExtDataPullPanel config={{ ...config, pull: { ...config.pull!, schedule_seconds: 30 } }} onSaved={vi.fn()} />
  </QueryClientProvider>))
  await clickConfigButton('拉取设置…')
  await editSeconds(value)

  expect(host.textContent).toContain(`每 ${value} 秒`)
  await clickConfigButton('保存配置')
  expect(host.querySelector('[role=alert]')?.textContent).toContain('10~2592000')
  expect(api.extDataPullConfig).not.toHaveBeenCalled()
})

it.each(['保存配置', '测试'])('rejects excessive seconds before %s can write keys or configuration', async action => {
  const onSaved = vi.fn()
  await act(async () => root.render(<QueryClientProvider client={client}>
    <ExtDataPullPanel config={{ ...config, pull: {
      ...config.pull!, schedule_seconds: 30, auth: { type: 'bearer' },
    } }} onSaved={onSaved} />
  </QueryClientProvider>))
  await clickConfigButton('拉取设置…')
  const keyInput = host.querySelector<HTMLInputElement>('input[type=password]')!
  await act(async () => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!.call(keyInput, 'test-key')
    keyInput.dispatchEvent(new Event('input', { bubbles: true }))
  })
  await editSeconds(2592060)
  await clickConfigButton(action)

  expect(host.textContent).toContain('10~2592000')
  expect(api.extDataPullConfig).not.toHaveBeenCalled()
  expect(api.extDataApiKeySet).not.toHaveBeenCalled()
  expect(api.extDataPullTest).not.toHaveBeenCalled()
  expect(onSaved).not.toHaveBeenCalled()

  await editSeconds(30)
  await clickConfigButton('保存配置')
  expect(api.extDataPullConfig).toHaveBeenCalledWith(config.id, expect.objectContaining({
    schedule_seconds: 30, schedule_minutes: 1,
  }))
  expect(api.extDataApiKeySet).toHaveBeenCalledWith(config.id, 'test-key')
  expect(onSaved).toHaveBeenCalledOnce()
})

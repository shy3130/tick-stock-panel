// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { api } from '@/lib/api'
import { CreateExtDialog } from './CreateExtDialog'

vi.mock('@/lib/api', () => ({ api: {
  extDataCreate: vi.fn(),
  extDataPullConfig: vi.fn(),
  extDataPullRun: vi.fn(),
  extDataDetectUrl: vi.fn(),
} }))

let host: HTMLDivElement
let root: Root
let client: QueryClient
let onClose: ReturnType<typeof vi.fn>

beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  vi.resetAllMocks()
  host = document.createElement('div')
  document.body.append(host)
  root = createRoot(host)
  client = new QueryClient({ defaultOptions: { mutations: { retry: false } } })
  onClose = vi.fn()
  vi.mocked(api.extDataCreate).mockResolvedValue({
    id: 'auction', label: '竞价', mode: 'snapshot', fields: [], created_at: '', updated_at: '',
  })
  vi.mocked(api.extDataPullConfig).mockResolvedValue({ status: 'ok', pull: {
    url: 'https://example.test', method: 'GET', response_path: '', schedule_minutes: 1, enabled: false,
  } })
  vi.mocked(api.extDataPullRun).mockResolvedValue({ status: 'ok', rows: 1, date: '2026-11-11' })
  vi.mocked(api.extDataDetectUrl).mockResolvedValue({
    status: 'ok', total_rows: 1, response_path: '', response_path_candidates: [],
    fields: [{ name: 'symbol', dtype: 'string', label: '' }, { name: 'price', dtype: 'float', label: '' }],
    symbol_candidates: ['symbol'], code_candidates: [], preview: [],
  })
})

afterEach(async () => {
  await act(async () => root.unmount())
  client.clear()
  host.remove()
})

async function click(label: string) {
  const button = [...host.querySelectorAll('button')].find(el => el.textContent?.trim() === label)!
  expect(button).toBeTruthy()
  await act(async () => {
    button.click()
    await new Promise(resolve => setTimeout(resolve, 10))
  })
}

async function fill(selector: string, value: string) {
  const input = host.querySelector<HTMLInputElement>(selector)!
  expect(input).toBeTruthy()
  await act(async () => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!.call(input, value)
    input.dispatchEvent(new Event('input', { bubbles: true }))
  })
}

async function setup(unit: 'seconds' | 'minutes' = 'seconds') {
  await act(async () => root.render(<QueryClientProvider client={client}>
    <CreateExtDialog onClose={onClose} />
  </QueryClientProvider>))
  await fill('input[placeholder="例: 概念"]', '竞价')
  await fill('input[placeholder="例: concept"]', 'auction')
  await fill('input[placeholder="https://api.example.com/data"]', 'https://example.test')
  await click('测试识别')
  if (unit === 'seconds') {
    const select = host.querySelector<HTMLSelectElement>('select[title^="间隔单位"]')!
    await act(async () => {
      select.value = unit
      select.dispatchEvent(new Event('change', { bubbles: true }))
    })
  }
}

it('rejects an excessive seconds interval before creating a table and allows a corrected retry', async () => {
  await setup()
  await fill('input[type=number][min="10"]', '2592060')
  await click('创建')

  expect(host.textContent).toContain('10~2592000')
  expect(api.extDataCreate).not.toHaveBeenCalled()
  expect(api.extDataPullConfig).not.toHaveBeenCalled()
  expect(api.extDataPullRun).not.toHaveBeenCalled()
  expect(onClose).not.toHaveBeenCalled()

  await fill('input[type=number][min="10"]', '30')
  await click('创建')

  expect(api.extDataCreate).toHaveBeenCalledOnce()
  expect(api.extDataPullConfig).toHaveBeenCalledWith('auction', expect.objectContaining({
    schedule_seconds: 30, schedule_minutes: 1,
  }))
  expect(onClose).toHaveBeenCalledOnce()
})

it.each([3, 9, 10.5])('shows the entered seconds interval %s and rejects it before any creation request', async value => {
  await setup()
  await fill('input[type=number][min="10"]', String(value))
  expect(host.textContent).toContain(`每 ${value} 秒`)
  await click('创建')

  expect(host.textContent).toContain('10~2592000')
  expect(api.extDataCreate).not.toHaveBeenCalled()
})

it.each([10, 2592000])('accepts the seconds boundary %s', async value => {
  await setup()
  await fill('input[type=number][min="10"]', String(value))
  await click('创建')

  expect(api.extDataPullConfig).toHaveBeenCalledWith('auction', expect.objectContaining({
    schedule_seconds: value, schedule_minutes: Math.ceil(value / 60),
  }))
  expect(onClose).toHaveBeenCalledOnce()
})

it('preserves minute intervals longer than 30 days', async () => {
  await setup('minutes')
  await fill('input[type=number][min="1"]', '43201')
  await click('创建')

  expect(api.extDataPullConfig).toHaveBeenCalledWith('auction', expect.objectContaining({
    schedule_seconds: null, schedule_minutes: 43201,
  }))
  expect(onClose).toHaveBeenCalledOnce()
})

it('ignores the unused interval when creating without a pull configuration', async () => {
  await setup()
  await fill('input[type=number][min="10"]', '2592060')
  const savePull = [...host.querySelectorAll('label')]
    .find(el => el.textContent?.includes('保存为拉取配置'))!.querySelector<HTMLInputElement>('input')!
  await act(async () => savePull.click())
  await click('创建')

  expect(api.extDataCreate).toHaveBeenCalledOnce()
  expect(api.extDataPullConfig).not.toHaveBeenCalled()
  expect(api.extDataPullRun).not.toHaveBeenCalled()
  expect(onClose).toHaveBeenCalledOnce()
})

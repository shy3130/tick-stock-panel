// @vitest-environment jsdom
import { act } from 'react'
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { expect, test, vi } from 'vitest'
import type { CustomSourceConfig } from '@/lib/api'
import { DataSourceEditor } from './DataSourceEditor'

const api = vi.hoisted(() => ({ saveDataSource: vi.fn(), dataSource: vi.fn() }))
vi.mock('@/lib/api', () => ({ api }))
vi.mock('@/components/Toast', () => ({ toast: vi.fn() }))

test('selects and saves the factor profile independently from daily bars', async () => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  const factorProfile = 'cn.equity.ex-factor.events.v1'
  const initial: CustomSourceConfig = {
    name: 'dividends', display_name: '事件源', auth: { type: 'none' },
    datasets: {
      daily: { url: '/bars', method: 'GET', response_path: 'rows', field_map: {},
        research_profile: 'cn.equity.regular.raw.lot100.v1' },
      adj_factor: { url: '/events', method: 'GET', response_path: 'rows',
        field_map: { code: 'symbol', date: 'trade_date', ratio: 'ex_factor', status: 'event_status' } },
    },
  }
  api.saveDataSource.mockResolvedValue({})
  api.dataSource.mockImplementation(async () => api.saveDataSource.mock.lastCall?.[0] ?? initial)
  const node = document.createElement('div')
  const root = createRoot(node)
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const onSaved = vi.fn()
  try {
    await act(async () => root.render(<QueryClientProvider client={client}>
      <DataSourceEditor existingName={initial.name} initial={initial} activeName=""
        onActivate={vi.fn()} onCancel={vi.fn()} onSaved={onSaved} />
    </QueryClientProvider>))
    const buttons = () => [...node.querySelectorAll('button')]
    await act(async () => buttons().find(b => b.textContent?.startsWith('除权因子'))!.click())
    const select = await vi.waitFor(() => {
      const field = node.querySelector<HTMLSelectElement>('select[aria-label="除权因子研究口径"]')
      expect(field).toBeTruthy()
      return field!
    })
    expect(select.value).toBe('')
    await act(async () => {
      select.value = factorProfile
      select.dispatchEvent(new Event('change', { bubbles: true }))
    })
    await act(async () => buttons().find(b => b.textContent === '保存')!.click())
    await vi.waitFor(() => expect(onSaved).toHaveBeenCalledOnce())
    const saved: CustomSourceConfig = api.saveDataSource.mock.lastCall![0]
    expect(saved.datasets.daily).toEqual(initial.datasets.daily)
    expect(saved.datasets.adj_factor).toEqual({ ...initial.datasets.adj_factor, research_profile: factorProfile })
  } finally {
    await act(async () => root.unmount())
    client.clear()
  }
})

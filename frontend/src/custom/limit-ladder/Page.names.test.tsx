// @vitest-environment jsdom
import { act } from 'react'
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { expect, it, vi } from 'vitest'
import { QK } from '@/lib/queryKeys'
import { LimitUpLadder } from './Page'

vi.mock('@/components/StockPreviewDialog', () => ({
  StockPreviewDialog: ({ symbol, name }: { symbol: string | null; name: string }) =>
    symbol && <div data-preview={symbol}>{name}</div>,
}))

it('labels missing names while preserving codes, names and stock navigation', async () => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  localStorage.clear()
  const client = new QueryClient({ defaultOptions: { queries: { staleTime: Infinity, retry: false } } })
  client.setQueryData(QK.capabilities, {})
  client.setQueryData(QK.preferences, {})
  client.setQueryData(QK.monitorRules, { rules: [] })
  client.setQueryData([...QK.limitLadder(undefined), undefined, 'up'], {
    as_of: '2026-11-10',
    tiers: [{ boards: 3, count: 4, stocks: [
      { symbol: '002623.SZ', name: null, status: 'limit_up', consecutive_limit_ups: 3, is_one_word: true },
      { symbol: '002708.SZ', name: '', status: 'limit_up', consecutive_limit_ups: 3 },
      { symbol: '600000.SH', name: '   ', status: 'limit_up', consecutive_limit_ups: 3 },
      { symbol: '603706.SH', name: '东方环宇', status: 'limit_up', consecutive_limit_ups: 3 },
    ] }],
  })
  const host = document.createElement('div')
  document.body.append(host)
  const root = createRoot(host)
  const scrollTo = vi.spyOn(window, 'scrollTo').mockImplementation(() => {})
  try {
    await act(async () => root.render(<QueryClientProvider client={client}>
      <MemoryRouter future={{ v7_startTransition: true, v7_relativeSplatPath: true }}><LimitUpLadder /></MemoryRouter>
    </QueryClientProvider>))
    expect(host.querySelectorAll('[title="名称数据缺失，请同步标的信息"]')).toHaveLength(3)
    expect(host.textContent).toContain('东方环宇')
    for (const code of ['002623', '002708', '600000', '603706']) expect(host.textContent).toContain(code)
    const card = [...host.querySelectorAll<HTMLElement>('[role=button]')].find(el => el.textContent?.includes('002623'))!
    await act(async () => card.click())
    expect(host.querySelector('[data-preview]')?.getAttribute('data-preview')).toBe('002623.SZ')
    expect(host.querySelector('[data-preview]')?.textContent).toBe('')
  } finally {
    await act(async () => root.unmount())
    client.clear()
    scrollTo.mockRestore()
    host.remove()
  }
})

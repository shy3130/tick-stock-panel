// @vitest-environment jsdom
import { act } from 'react'
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { api, type WatchlistEntry, type WatchlistGroup } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { WatchlistGroupTransfer } from './WatchlistGroupTransfer'

const groups: WatchlistGroup[] = [
  { id: 'a', name: '观察', color: 'sky' }, { id: 'b', name: '跟踪', color: 'teal' },
]
const entries: WatchlistEntry[] = [
  { symbol: '600000.SH', name: '浦发银行', group_ids: ['a'], added_at: '' },
  { symbol: '000001.SZ', name: '平安银行', group_ids: ['a', 'b'], added_at: '' },
  { symbol: '510300.SH', name: '沪深 ETF', group_ids: [], added_at: '' },
]
let client: QueryClient
let host: HTMLDivElement
let root: ReturnType<typeof createRoot>
const buttons = () => [...document.querySelectorAll<HTMLButtonElement>('button')]
const findButton = (name: string) => buttons().find(el => el.textContent === name)!
const click = async (el: HTMLElement) => { await act(async () => el.click()) }
const checkboxes = () => [...document.querySelectorAll<HTMLInputElement>('input[type=checkbox]')]
async function open(source: string) {
  await act(async () => root.render(<QueryClientProvider client={client}>
    <WatchlistGroupTransfer key={source} source={source} entries={entries} groups={groups} />
  </QueryClientProvider>))
  await click(findButton('批量分组'))
}
async function chooseTarget() {
  const select = document.querySelector('select')!
  await act(async () => { select.value = 'b'; select.dispatchEvent(new Event('change', { bubbles: true })) })
}

beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  host = document.createElement('div')
  document.body.append(host)
  root = createRoot(host)
  client = new QueryClient({ defaultOptions: { mutations: { retry: false } } })
})
afterEach(async () => {
  await act(async () => root.unmount())
  client.clear()
  host.remove()
  vi.restoreAllMocks()
})

it.each(['move', 'copy'] as const)('submits one %s request for selected group members and publishes the returned list', async action => {
  const result = { changed: 1, symbols: [{ ...entries[0], group_ids: ['b'] }] }
  const call = vi.spyOn(api, 'watchlistGroupTransfer').mockResolvedValue(result)
  await open('a')
  const submit = findButton(action === 'move' ? '移动到目标组' : '复制到目标组')
  expect(submit.disabled).toBe(true)
  expect(checkboxes()).toHaveLength(3) // 全选 + 当前组两只
  expect([...document.querySelectorAll('option')].map(o => o.value)).toEqual(['', 'b'])
  await click(checkboxes()[1])
  expect(checkboxes()[0].indeterminate).toBe(true)
  await chooseTarget()
  await click(submit)
  expect(call.mock.calls).toEqual([['a', 'b', ['600000.SH'], action]])
  expect(client.getQueryData(QK.watchlist)).toEqual({ symbols: result.symbols })
  expect(document.querySelector('[role=dialog]')).toBeNull()
  await click(findButton('批量分组'))
  expect(checkboxes().every(el => !el.checked)).toBe(true)
})

it('keeps the selection after an error and scopes select-all to the search results', async () => {
  const call = vi.spyOn(api, 'watchlistGroupTransfer').mockRejectedValue(new Error('分组成员已变化'))
  await open('all')
  expect(findButton('移动到目标组')).toBeUndefined()
  const input = document.querySelector<HTMLInputElement>('input[type=search]')!
  await act(async () => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!.call(input, '银行')
    input.dispatchEvent(new Event('input', { bubbles: true }))
  })
  await click(checkboxes()[0])
  await chooseTarget()
  await click(findButton('复制到目标组'))
  await act(async () => { await new Promise(resolve => setTimeout(resolve, 0)) })
  expect(document.querySelector('[role=alert]')?.textContent).toBe('分组成员已变化')
  expect(call).toHaveBeenCalledWith('all', 'b', ['600000.SH', '000001.SZ'], 'copy')
  expect(checkboxes().every(el => el.checked)).toBe(true)
  await open('ungrouped')
  expect(findButton('复制到目标组')).toBeUndefined()
  expect(checkboxes()).toHaveLength(2)
  expect(findButton('移动到目标组').disabled).toBe(true)
})

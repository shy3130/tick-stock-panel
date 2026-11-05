// @vitest-environment jsdom
import { act } from 'react'
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, expect, it, vi } from 'vitest'
import { api } from './api'
import { QK } from './queryKeys'
import { useDataVersions } from './useDataVersions'

vi.mock('./api', () => ({ api: { dataVersions: vi.fn() } }))
Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
const roots: ReturnType<typeof createRoot>[] = []
const clients: QueryClient[] = []
const hosts: HTMLDivElement[] = []
afterEach(async () => {
  await act(async () => roots.splice(0).forEach(root => root.unmount()))
  clients.splice(0).forEach(client => client.clear())
  hosts.splice(0).forEach(host => host.remove())
  vi.clearAllMocks()
})
function Versions() { useDataVersions(); return null }
async function tick() { await act(async () => { await new Promise(resolve => setTimeout(resolve, 15)) }) }

it('independent windows refresh only changed datasets, including when quote polling is off', async () => {
  let versions = { daily: 'server:1', minute: 'server:1' }
  vi.mocked(api.dataVersions).mockImplementation(async () => ({ versions }))
  for (let i = 0; i < 2; i++) {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    clients.push(client)
    const host = document.createElement('div')
    document.body.append(host); hosts.push(host)
    const root = createRoot(host); roots.push(root)
    await act(async () => root.render(<QueryClientProvider client={client}><Versions /></QueryClientProvider>))
  }
  await tick()
  for (const client of clients) {
    client.setQueryData(['minute-batch'], { data: {} })
    client.setQueryData(['watchlist-enriched', ''], { rows: [] })
    client.setQueryData(['financials'], {})
  }
  versions = { ...versions, minute: 'server:2' }
  await act(async () => { await Promise.all(clients.map(client => client.invalidateQueries({ queryKey: QK.dataVersions }))) })
  await tick()
  for (const client of clients) {
    expect(client.getQueryState(['minute-batch'])?.isInvalidated).toBe(true)
    expect(client.getQueryState(['watchlist-enriched', ''])?.isInvalidated).toBe(false)
    expect(client.getQueryState(['financials'])?.isInvalidated).toBe(false)
  }
  versions = { ...versions, daily: 'server:2' }
  await act(async () => { await Promise.all(clients.map(client => client.invalidateQueries({ queryKey: QK.dataVersions }))) })
  await tick()
  expect(clients.every(client => client.getQueryState(['watchlist-enriched', ''])?.isInvalidated)).toBe(true)
})

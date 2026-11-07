// @vitest-environment jsdom
import { act, useState } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { Indices } from './Indices'

type MinuteResponse = {
  symbol: string
  date: string
  rows: { datetime: string; close: number }[]
}
const fixtures = vi.hoisted(() => ({
  pending: [] as { symbol: string; day: string; resolve: (value: MinuteResponse) => void }[],
  mounts: 0,
}))

vi.mock('@/lib/api', () => ({
  api: {
    indexQuotes: async () => ({ rows: [] }),
    indexDaily: async (symbol: string) => ({
      symbol,
      rows: ['2026-09-09', '2026-09-10', '2026-09-11'].map(date => ({
        date, open: 10, high: 12, low: 9, close: 11, volume: 1,
      })),
    }),
    indexMinute: (symbol: string, day: string) => new Promise<MinuteResponse>(resolve => {
      fixtures.pending.push({ symbol, day, resolve })
    }),
  },
}))
vi.mock('@/lib/useSharedQueries', () => ({
  useCapabilities: () => ({ data: { capabilities: { 'kline.minute.batch': true } } }),
}))
vi.mock('@/components/charts/LightweightDaily', () => ({
  LightweightDaily: ({ data, onDateClick }: {
    data: { date: string }[]; onDateClick: (date: string) => void
  }) => <div>{data.map(row => (
    <button key={row.date} data-day={row.date} onClick={() => onDateClick(row.date)}>{row.date}</button>
  ))}</div>,
}))
vi.mock('@/components/charts/LightweightIntraday', () => ({
  LightweightIntraday: ({ data, date }: { data: MinuteResponse['rows']; date: string }) => {
    const [mount] = useState(() => ++fixtures.mounts)
    return <div data-chart data-mount={mount}>{`${date}|${data[0].datetime}|${data[0].close}`}</div>
  },
}))

let host: HTMLDivElement
let root: Root
let client: QueryClient

beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  fixtures.pending.length = 0
  fixtures.mounts = 0
  host = document.createElement('div')
  document.body.append(host)
  root = createRoot(host)
  client = new QueryClient({ defaultOptions: { queries: {
    retry: false, staleTime: Infinity, gcTime: Infinity,
  } } })
})

afterEach(async () => {
  await act(async () => root.unmount())
  client.clear()
  host.remove()
})

// React Query batches observer notifications on the next timer tick.
async function settle() {
  for (let i = 0; i < 5; i++) {
    await act(async () => { await new Promise(resolve => setTimeout(resolve, 0)) })
  }
}

async function resolveDay(day: string, close: number) {
  const index = fixtures.pending.findIndex(call => call.day === day)
  expect(index).toBeGreaterThanOrEqual(0)
  const [call] = fixtures.pending.splice(index, 1)
  await act(async () => call.resolve({
    symbol: call.symbol, date: day, rows: [{ datetime: `${day}T09:35:00`, close }],
  }))
  await settle()
}

async function clickDay(day: string) {
  const button = host.querySelector<HTMLButtonElement>(`[data-day="${day}"]`)
  expect(button).not.toBeNull()
  await act(async () => button!.click())
  await settle()
}

it('keeps selected dates isolated across delayed, out-of-order and cached responses', async () => {
  await act(async () => root.render(
    <MemoryRouter><QueryClientProvider client={client}><Indices /></QueryClientProvider></MemoryRouter>,
  ))
  await settle()
  const chart = () => host.querySelector<HTMLElement>('[data-chart]')
  await resolveDay('2026-09-11', 11)
  expect(chart()?.textContent).toBe('2026-09-11|2026-09-11T09:35:00|11')

  await clickDay('2026-09-10')
  expect(chart()).toBeNull()
  await clickDay('2026-09-09')
  await resolveDay('2026-09-10', 10)
  expect(chart()).toBeNull()
  await resolveDay('2026-09-09', 9)
  expect(chart()?.textContent).toBe('2026-09-09|2026-09-09T09:35:00|9')

  const mount9 = chart()?.dataset.mount
  await clickDay('2026-09-11')
  expect(chart()?.textContent).toBe('2026-09-11|2026-09-11T09:35:00|11')
  expect(chart()?.dataset.mount).not.toBe(mount9)

  const mount11 = chart()?.dataset.mount
  await act(async () => { void client.invalidateQueries({ queryKey: ['index-minute'] }) })
  await settle()
  expect(chart()?.textContent).toBe('2026-09-11|2026-09-11T09:35:00|11')
  await resolveDay('2026-09-11', 12)
  expect(chart()?.textContent).toBe('2026-09-11|2026-09-11T09:35:00|12')
  expect(chart()?.dataset.mount).toBe(mount11)
})

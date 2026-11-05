// @vitest-environment jsdom
import { afterEach, expect, it, vi } from 'vitest'
import { api } from './api'

afterEach(() => vi.unstubAllGlobals())

it('列级显示周期随序列请求发送, 不指定时保留整表默认行为', async () => {
  const fetchMock = vi.fn(async (_url: string, _init?: RequestInit) => Response.json({ items: {} }))
  vi.stubGlobal('fetch', fetchMock)
  await api.extDataSeries('flow', '主力净流入', '2026-11-05', ['600000.SH'], 5, 'daily')
  const daily = new URL(fetchMock.mock.calls[0][0] as string, 'http://test')
  expect(daily.searchParams.get('granularity')).toBe('daily')
  expect(daily.searchParams.get('field')).toBe('主力净流入')
  expect(daily.searchParams.get('days')).toBe('5')
  await api.extDataSeries('flow', 'net', undefined, ['600000.SH'], 10, 'intraday')
  expect(new URL(fetchMock.mock.calls[1][0] as string, 'http://test').searchParams.get('granularity')).toBe('intraday')
  await api.extDataSeries('flow', 'net')
  expect(new URL(fetchMock.mock.calls[2][0] as string, 'http://test').searchParams.has('granularity')).toBe(false)
})

import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import {
  closeStockPreview, openStockPreview,
} from './stockPreview'
import { getStockPreviewRequest, navigateStockPreview, stockPreviewUi, subscribeStockPreview } from '@/lib/stockPreviewStore'

beforeEach(() => { vi.stubGlobal('window', { __TSP_EXT__: { ui: stockPreviewUi } }) })
afterEach(() => { stockPreviewUi.closeStockPreview(); vi.unstubAllGlobals() })

it('returns false on an older host and remains safe before browser bootstrap', () => {
  vi.stubGlobal('window', { __TSP_EXT__: { api: {} } })
  expect(openStockPreview({ symbol: '159558.SZ' })).toBe(false)
  expect(() => closeStockPreview()).not.toThrow()
  vi.stubGlobal('window', undefined)
  expect(openStockPreview({ symbol: '159558.SZ' })).toBe(false)
})

it.each(['605365.SH', '159558.SZ', '000001.SH', '920071.BJ'])('accepts the existing full symbol contract: %s', symbol => {
  expect(openStockPreview({ symbol })).toBe(true)
  expect(getStockPreviewRequest()?.symbol).toBe(symbol)
})

it('normalizes and copies inputs without guessing an exchange or retaining caller-owned objects', () => {
  const navList = [
    { symbol: ' 159558.sz ', name: ' ETF ' },
    { symbol: '159558.SZ', name: '重复' },
    { symbol: '605365.SH', name: null },
    { symbol: '605365' },
  ]
  expect(openStockPreview({ symbol: ' 159558.sz ', navList })).toBe(true)
  expect(getStockPreviewRequest()).toMatchObject({
    symbol: '159558.SZ', name: 'ETF', navList: [
      { symbol: '159558.SZ', name: 'ETF' }, { symbol: '605365.SH' },
    ],
  })
  navList[0].symbol = '600519.SH'
  expect(getStockPreviewRequest()?.navList?.[0].symbol).toBe('159558.SZ')
})

it.each([null, undefined, {}, { symbol: 123 }, { symbol: '600519' }, { symbol: '../settings' },
  { symbol: '600519.US' }, { symbol: '600519.SH', navList: {} }])('rejects malformed runtime inputs without replacing an open preview: %j', input => {
  openStockPreview({ symbol: '159558.SZ' })
  const previous = getStockPreviewRequest()
  const changed = vi.fn()
  const unsubscribe = subscribeStockPreview(changed)
  try {
    expect(openStockPreview(input as Parameters<typeof openStockPreview>[0])).toBe(false)
    expect(getStockPreviewRequest()).toBe(previous)
    expect(changed).not.toHaveBeenCalled()
  } finally { unsubscribe() }
})

it('replaces the current request, navigates with the same list and closes idempotently', () => {
  openStockPreview({ symbol: '159558.SZ', navList: [{ symbol: '159558.SZ' }, { symbol: '605365.SH', name: '立达信' }] })
  const list = getStockPreviewRequest()!.navList
  navigateStockPreview('605365.SH', '立达信')
  expect(getStockPreviewRequest()).toMatchObject({ symbol: '605365.SH', name: '立达信', navList: list })
  openStockPreview({ symbol: '600519.SH' })
  expect(getStockPreviewRequest()?.navList).toBeUndefined()
  const changed = vi.fn()
  const unsubscribe = subscribeStockPreview(changed)
  closeStockPreview()
  closeStockPreview()
  navigateStockPreview('159558.SZ')
  expect(getStockPreviewRequest()).toBeNull()
  expect(changed).toHaveBeenCalledOnce()
  unsubscribe()
})

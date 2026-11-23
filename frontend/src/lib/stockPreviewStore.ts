import type { OpenStockPreviewOptions, StockPreviewItem, StockPreviewUi } from '@/extensions/stockPreview'
import { uniqueBy, navItemKey, type NavItem } from './listNav'

interface StockPreviewRequest {
  symbol: string
  name?: string
  navList?: NavItem[]
}

let request: StockPreviewRequest | null = null
const listeners = new Set<() => void>()

function normalizeItem(value: unknown): NavItem | null {
  if (!value || typeof value !== 'object') return null
  const item = value as Partial<StockPreviewItem>
  if (typeof item.symbol !== 'string') return null
  const symbol = item.symbol.trim().toUpperCase()
  if (!/^\d{6}\.(SH|SZ|BJ)$/.test(symbol)) return null
  return { symbol, name: typeof item.name === 'string' ? item.name.trim() || undefined : undefined }
}

function open(options: OpenStockPreviewOptions): boolean {
  const item = normalizeItem(options)
  if (!item || (options.navList != null && !Array.isArray(options.navList))) return false
  const navList = options.navList ? uniqueBy(options.navList.flatMap(value => {
    const normalized = normalizeItem(value)
    return normalized ? [normalized] : []
  }), navItemKey) : undefined
  request = { ...item, name: item.name ?? navList?.find(row => row.symbol === item.symbol)?.name, navList }
  listeners.forEach(listener => listener())
  return true
}

function close(): void {
  if (!request) return
  request = null
  listeners.forEach(listener => listener())
}

/** 仅由宿主安装到能力桥，不随源码扩展的独立编译产物复制状态。 */
export const stockPreviewUi: StockPreviewUi = { openStockPreview: open, closeStockPreview: close }

export function getStockPreviewRequest() { return request }

export function subscribeStockPreview(listener: () => void) {
  listeners.add(listener)
  return () => { listeners.delete(listener) }
}

export function navigateStockPreview(symbol: string, name?: string): void {
  const item = normalizeItem({ symbol, name })
  if (!request || !item) return
  request = { ...request, ...item }
  listeners.forEach(listener => listener())
}

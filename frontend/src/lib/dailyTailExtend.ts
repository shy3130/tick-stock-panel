import type { KlineRow, QuoteFreshness } from './api'
import { advanceQuoteFreshness } from './quote-freshness'
import type { TailExtendQuoteDates } from './minuteTailExtend'

interface DailyLiveRow {
  symbol: string
  asset_type?: string | null
  date?: string | null
  open?: number | null
  high?: number | null
  low?: number | null
  close?: number | null
  quote_freshness?: QuoteFreshness | null
}

/** 自选日K尾部修补；只生成展示序列，不修改查询缓存。 */
export function extendDailyTail(
  base: Record<string, KlineRow[]>,
  liveRows: DailyLiveRow[] | undefined,
  quoteDates: TailExtendQuoteDates | null | undefined,
): Record<string, KlineRow[]> {
  if (!liveRows?.length) return base
  const liveBySymbol = new Map(liveRows.map(row => [row.symbol, row]))
  const patched: Record<string, KlineRow[]> = {}
  for (const [symbol, rows] of Object.entries(base)) {
    const live = liveBySymbol.get(symbol)
    if (live?.quote_freshness && ['stale', 'delayed', 'unknown', 'missing'].includes(
      advanceQuoteFreshness(live.quote_freshness, Date.now() - live.quote_freshness.checked_at).state,
    )) { patched[symbol] = rows; continue }
    // 新后端给出逐行日期；旧后端仅按资产日期回退，不能使用混合列表的全局最旧日。
    const asset = (live?.asset_type ?? 'stock') as keyof TailExtendQuoteDates
    const asOf = live?.date ?? quoteDates?.[asset]
    const { open, high, low, close } = live ?? {}
    if (!rows.length || !asOf || !/^\d{4}-\d{2}-\d{2}$/.test(asOf)
      || [open, high, low, close].some(v => typeof v !== 'number' || !Number.isFinite(v) || v <= 0)) {
      patched[symbol] = rows
      continue
    }
    const candle = { date: asOf, open: open!, high: high!, low: low!, close: close! }
    const last = rows[rows.length - 1]
    patched[symbol] = last.date === asOf
      ? [...rows.slice(0, -1), { ...last, ...candle }]
      : last.date < asOf ? [...rows, candle] : rows
  }
  return patched
}

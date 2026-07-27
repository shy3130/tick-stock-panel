import { request } from '@/lib/api'

export type PortfolioTradeSide = 'buy' | 'sell'

export interface PortfolioTradeInput {
  symbol: string
  name: string
  side: PortfolioTradeSide
  quantity: number
  price: number
  fees: number
  trade_date: string
  note: string
}

export interface PortfolioTrade extends PortfolioTradeInput {
  id: string
  created_at: string
  updated_at: string
}

export interface PortfolioPosition {
  symbol: string
  name: string
  quantity: number
  average_cost: number
  cost_value: number
  realized_pnl: number
  first_trade_date: string
  last_trade_date: string
}

export interface PortfolioSummary {
  position_count: number
  trade_count: number
  cost_value: number
  realized_pnl: number
}

export interface Portfolio {
  trades: PortfolioTrade[]
  positions: PortfolioPosition[]
  summary: PortfolioSummary
}

export interface PositionQuote {
  symbol: string
  name: string
  price: number
  change_pct: number | null
  date: string
  is_live: boolean
}

interface DailyRow {
  date: string
  close: number
  change_pct?: number | null
  is_live?: boolean
}

interface DailyResponse {
  symbol: string
  name?: string
  stock_info?: { name?: string }
  rows: DailyRow[]
}

export interface InstrumentSearchResult {
  symbol: string
  name: string
  code: string
  asset_type?: string
}

export const PORTFOLIO_QUERY_KEY = ['sycee', 'portfolio'] as const

export const portfolioApi = {
  get: () => request<Portfolio>('/api/sycee/portfolio'),
  createTrade: (trade: PortfolioTradeInput) =>
    request<{ trade: PortfolioTrade; portfolio: Portfolio }>('/api/sycee/portfolio/trades', {
      method: 'POST',
      body: JSON.stringify(trade),
    }),
  updateTrade: (tradeId: string, changes: Partial<PortfolioTradeInput>) =>
    request<{ trade: PortfolioTrade; portfolio: Portfolio }>(
      `/api/sycee/portfolio/trades/${encodeURIComponent(tradeId)}`,
      { method: 'PATCH', body: JSON.stringify(changes) },
    ),
  deleteTrade: (tradeId: string) =>
    request<{ ok: boolean; portfolio: Portfolio }>(
      `/api/sycee/portfolio/trades/${encodeURIComponent(tradeId)}`,
      { method: 'DELETE' },
    ),
  searchInstruments: (query: string) =>
    request<{ results: InstrumentSearchResult[] }>(
      `/api/kline/instruments/search?q=${encodeURIComponent(query)}&limit=12&asset_types=stock,etf`,
    ),
  quotes: async (symbols: string[]): Promise<Record<string, PositionQuote>> => {
    const responses = await Promise.allSettled(symbols.map(symbol => (
      request<DailyResponse>(`/api/kline/daily?symbol=${encodeURIComponent(symbol)}&days=10`)
    )))
    const quotes: Record<string, PositionQuote> = {}
    for (const result of responses) {
      if (result.status !== 'fulfilled') continue
      const response = result.value
      const latest = response.rows.at(-1)
      if (!latest || !Number.isFinite(latest.close) || latest.close <= 0) continue
      quotes[response.symbol] = {
        symbol: response.symbol,
        name: response.name ?? response.stock_info?.name ?? '',
        price: latest.close,
        change_pct: typeof latest.change_pct === 'number' ? latest.change_pct : null,
        date: latest.date,
        is_live: latest.is_live === true,
      }
    }
    return quotes
  },
}

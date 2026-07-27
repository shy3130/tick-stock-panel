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

export type PortfolioAlertChannel = 'feishu' | 'wecom'

export interface PortfolioSellAlertConfig {
  enabled: boolean
  strategy_id: string
  webhook_channels: PortfolioAlertChannel[]
  rule_id: string
}

export interface PortfolioSellAlertRule {
  id: string
  name: string
  enabled: boolean
  type: 'strategy'
  asset_type: 'stock'
  scope: 'symbols'
  symbols: string[]
  sector: null
  strategy_id: string
  direction: 'exit'
  notify_events: ['sell_signal']
  conditions: []
  logic: 'and'
  cooldown_seconds: number
  severity: 'warn'
  webhook_channels: PortfolioAlertChannel[]
  message: string
}

export interface PortfolioSellAlertStatus {
  config: PortfolioSellAlertConfig
  state: 'disabled' | 'waiting_for_positions' | 'ready'
  position_count: number
  symbols: string[]
  desired_rule: PortfolioSellAlertRule | null
}

export interface PortfolioSellAlertUpdate {
  enabled: boolean
  strategy_id: string
  webhook_channels: PortfolioAlertChannel[]
}

export interface SellAlertStrategy {
  id: string
  name: string
  description?: string
  exit_signals: string[]
}

export interface ExistingMonitorRule extends Omit<PortfolioSellAlertRule, 'type' | 'asset_type' | 'scope' | 'sector' | 'direction' | 'notify_events' | 'conditions' | 'logic' | 'severity' | 'webhook_channels'> {
  type: string
  asset_type?: string
  scope: string
  sector?: string | null
  direction: string
  notify_events?: string[]
  conditions: Array<Record<string, unknown>>
  logic: string
  severity: string
  webhook_channels?: string[]
  created_at?: string
}

export const PORTFOLIO_QUERY_KEY = ['sycee', 'portfolio'] as const
export const PORTFOLIO_SELL_ALERT_QUERY_KEY = ['sycee', 'portfolio-sell-alert'] as const

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

export const portfolioSellAlertApi = {
  get: () => request<PortfolioSellAlertStatus>('/api/sycee/portfolio/sell-alert'),
  update: (update: PortfolioSellAlertUpdate) =>
    request<PortfolioSellAlertStatus>('/api/sycee/portfolio/sell-alert', {
      method: 'PUT',
      body: JSON.stringify(update),
    }),
  strategies: () => request<{ strategies: SellAlertStrategy[] }>(
    '/api/strategies?asset_type=stock&timeframe=1d',
  ),
  rules: () => request<{ rules: ExistingMonitorRule[] }>('/api/monitor-rules'),
  saveRule: (rule: PortfolioSellAlertRule) =>
    request<{ ok: boolean; rule: ExistingMonitorRule }>('/api/monitor-rules', {
      method: 'POST',
      body: JSON.stringify(rule),
    }),
  deleteRule: (ruleId: string) =>
    request<{ ok: boolean }>(`/api/monitor-rules/${encodeURIComponent(ruleId)}`, {
      method: 'DELETE',
    }),
}

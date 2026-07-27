import { request } from '@/lib/api'

export interface StrategyParamDefinition {
  id: string
  default: string | number | boolean
}

export interface ComparableStrategy {
  id: string
  name: string
  description: string
  source: 'builtin' | 'custom' | 'ai'
  execution_backend: 'polars_expr' | 'matrix_native' | 'python_history_legacy'
  params: StrategyParamDefinition[]
  params_defaults: Record<string, unknown>
  basic_filter: Record<string, unknown>
  scoring: Record<string, number>
  entry_signals: string[]
  exit_signals: string[]
  stop_loss: number | null
  take_profit: number | null
  trailing_stop: number | null
  trailing_take_profit_activate: number | null
  trailing_take_profit_drawdown: number | null
  max_hold_days: number | null
}

export interface StrategyBacktestResult {
  run_id: string
  stats: Record<string, unknown>
  equity_curve: { date: string; value: number }[]
  strategy_info: { id: string; name: string }
  elapsed_ms: number
  error: string | null
}

export interface BacktestProgress {
  day: number
  total: number
  date: string
  equity: number
}

interface WatchlistEntry {
  symbol: string
  name?: string | null
}

export const strategyCompareApi = {
  strategies: () => request<{ strategies: ComparableStrategy[] }>(
    '/api/strategies?asset_type=stock&timeframe=1d',
  ),
  watchlist: () => request<{ symbols: WatchlistEntry[] }>('/api/watchlist'),
}

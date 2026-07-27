import { request } from '@/lib/api'

export type StrategyTrackStatus = 'tracking' | 'paused' | 'closed'

export interface StrategyObservationInput {
  end_date: string
  run_id: string
  total_return: number | null
  annual_return: number | null
  sharpe: number | null
  max_drawdown: number | null
  win_rate: number | null
  trade_count: number | null
  ending_equity: number | null
  elapsed_ms: number
}

export interface StrategyObservation extends StrategyObservationInput {
  id: string
  observed_at: string
}

export interface StrategyTrackInput {
  strategy_id: string
  strategy_name: string
  symbols: string[]
  start_date: string
  initial_capital: number
  max_positions: number
  commission_pct: number
  stamp_tax_pct: number
  slippage_bps: number
  params: Record<string, unknown>
  overrides: Record<string, unknown>
  note: string
}

export interface StrategyTrack extends StrategyTrackInput {
  id: string
  status: StrategyTrackStatus
  observations: StrategyObservation[]
  created_at: string
  updated_at: string
}

export const STRATEGY_TRACKS_QUERY_KEY = ['sycee', 'strategy-tracks'] as const

export const strategyTrackingApi = {
  list: () => request<{ tracks: StrategyTrack[]; total: number }>(
    '/api/sycee/strategy-tracks',
  ),
  create: (input: StrategyTrackInput) => request<{ track: StrategyTrack }>(
    '/api/sycee/strategy-tracks',
    { method: 'POST', body: JSON.stringify(input) },
  ),
  update: (trackId: string, changes: { status?: StrategyTrackStatus; note?: string }) =>
    request<{ track: StrategyTrack }>(
      `/api/sycee/strategy-tracks/${encodeURIComponent(trackId)}`,
      { method: 'PATCH', body: JSON.stringify(changes) },
    ),
  saveObservation: (trackId: string, input: StrategyObservationInput) => request<{
    track: StrategyTrack
    observation: StrategyObservation
    action: 'created' | 'replaced'
  }>(
    `/api/sycee/strategy-tracks/${encodeURIComponent(trackId)}/observations`,
    { method: 'POST', body: JSON.stringify(input) },
  ),
  delete: (trackId: string) => request<{ ok: true }>(
    `/api/sycee/strategy-tracks/${encodeURIComponent(trackId)}`,
    { method: 'DELETE' },
  ),
}

import { request } from '@/lib/api'
import type { PortfolioTrade } from '../portfolio/api'

export type MistakeTag =
  | 'plan_deviation'
  | 'late_entry'
  | 'early_exit'
  | 'late_exit'
  | 'oversize'
  | 'thesis_error'
  | 'execution'
  | 'emotional'

export type PnlResult = 'profit' | 'loss' | 'breakeven' | 'planned'

export interface TradeAttribution {
  cost_basis: number
  realized_pnl: number | null
  return_pct: number | null
  holding_days: number | null
  pnl_result: PnlResult
}

export interface TradeReviewInput {
  strategy_id: string
  entry_reason: string
  expectation: string
  invalidation: string
  exit_reason: string
  conclusion: string
  mistake_tags: MistakeTag[]
}

export interface TradeReview extends TradeReviewInput {
  id: string
  trade_id: string
  created_at: string
  updated_at: string
}

export interface TradeReviewItem {
  trade: PortfolioTrade | null
  attribution: TradeAttribution | null
  review: TradeReview | null
}

export interface TradeReviewSummary {
  trade_count: number
  reviewed_count: number
  sell_count: number
  reviewed_sell_count: number
  orphaned_count: number
}

export interface TradeReviewResponse {
  items: TradeReviewItem[]
  summary: TradeReviewSummary
}

export interface ReviewStrategy {
  id: string
  name: string
}

export const TRADE_REVIEWS_QUERY_KEY = ['sycee', 'trade-reviews'] as const
export const TRADE_REVIEW_STRATEGIES_QUERY_KEY = ['sycee', 'trade-review-strategies'] as const

export const tradeReviewApi = {
  list: () => request<TradeReviewResponse>('/api/sycee/trade-reviews'),
  save: (tradeId: string, review: TradeReviewInput) =>
    request<{ review: TradeReview }>(`/api/sycee/trade-reviews/${encodeURIComponent(tradeId)}`, {
      method: 'PUT',
      body: JSON.stringify(review),
    }),
  delete: (tradeId: string) =>
    request<{ ok: boolean }>(`/api/sycee/trade-reviews/${encodeURIComponent(tradeId)}`, {
      method: 'DELETE',
    }),
  strategies: () => request<{ strategies: ReviewStrategy[] }>('/api/strategies?timeframe=1d'),
}

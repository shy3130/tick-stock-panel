import { request } from '@/lib/api'

import { portfolioApi, type Portfolio, type PositionQuote } from '../portfolio/api'
import { researchLedgerApi, type ResearchEntry } from '../research-ledger/api'
import { strategyTrackingApi, type StrategyTrack } from '../strategy-tracking/api'

export interface BriefingWatchlistEntry {
  symbol: string
  name?: string | null
  note?: string
}

export interface BriefingAlert {
  ts: number
  source: string
  type: string
  symbol?: string
  name?: string | null
  message: string
  price?: number | null
  change_pct?: number | null
  severity?: string
}

export interface BriefingRankItem {
  name: string
  count: number
  avg_pct: number
}

export interface BriefingMarketOverview {
  as_of: string | null
  breadth: {
    total: number
    up: number
    down: number
    flat: number
    up_pct: number
    down_pct: number
  }
  limit: {
    limit_up: number
    broken: number
    limit_down: number
  }
  emotion: {
    score: number
    label: string
  }
  concept_rank: { leading: BriefingRankItem[] }
  industry_rank: { leading: BriefingRankItem[] }
}

export interface BriefingMarketRecap {
  id: string
  as_of: string
  summary?: string
  emotion_score?: number | null
  emotion_label?: string
  created_at: string
}

export interface DailyBriefingSources {
  portfolio: Portfolio | null
  quotes: Record<string, PositionQuote>
  watchlist: BriefingWatchlistEntry[]
  alerts: BriefingAlert[]
  overview: BriefingMarketOverview | null
  recaps: BriefingMarketRecap[]
  tracks: StrategyTrack[]
  research: ResearchEntry[]
  unavailable: string[]
}

function fulfilled<T>(result: PromiseSettledResult<T>): T | null {
  return result.status === 'fulfilled' ? result.value : null
}

export async function loadDailyBriefingSources(): Promise<DailyBriefingSources> {
  const portfolioBundle = portfolioApi.get().then(async portfolio => ({
    portfolio,
    quotes: await portfolioApi.quotes(portfolio.positions.map(position => position.symbol)),
  }))

  const results = await Promise.allSettled([
    portfolioBundle,
    request<{ symbols: BriefingWatchlistEntry[] }>('/api/watchlist'),
    request<{ alerts: BriefingAlert[] }>('/api/alerts?days=3&limit=100'),
    request<BriefingMarketOverview>('/api/overview/market'),
    request<{ reports: BriefingMarketRecap[] }>('/api/market-recap/reports'),
    strategyTrackingApi.list(),
    researchLedgerApi.list(),
  ] as const)

  const [portfolioResult, watchlistResult, alertsResult, overviewResult, recapsResult, tracksResult, researchResult] = results
  const portfolio = fulfilled(portfolioResult)
  const watchlist = fulfilled(watchlistResult)
  const alerts = fulfilled(alertsResult)
  const overview = fulfilled(overviewResult)
  const recaps = fulfilled(recapsResult)
  const tracks = fulfilled(tracksResult)
  const research = fulfilled(researchResult)
  const unavailable = [
    portfolio ? null : '持仓',
    watchlist ? null : '自选',
    alerts ? null : '监控提醒',
    overview ? null : '市场概览',
    recaps ? null : '市场复盘',
    tracks ? null : '策略跟踪',
    research ? null : '研究账本',
  ].filter((label): label is string => label !== null)

  return {
    portfolio: portfolio?.portfolio ?? null,
    quotes: portfolio?.quotes ?? {},
    watchlist: watchlist?.symbols ?? [],
    alerts: alerts?.alerts ?? [],
    overview,
    recaps: recaps?.reports ?? [],
    tracks: tracks?.tracks ?? [],
    research: research?.entries ?? [],
    unavailable,
  }
}

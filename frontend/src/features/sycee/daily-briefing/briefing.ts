import { buildPortfolioView } from '../portfolio/portfolioView.ts'
import type { ResearchEntry } from '../research-ledger/api.ts'
import type { StrategyObservation } from '../strategy-tracking/api.ts'
import type { DailyBriefingSources } from './api.ts'
import {
  prioritizeEvents,
  type PrioritizedEventGroup,
  type ScopedBriefingAlert,
} from './eventPriority.ts'

export type BriefingMode = 'morning' | 'evening'
export type BriefingTone = 'neutral' | 'positive' | 'warning' | 'danger'

export interface BriefingFocusItem {
  id: string
  tone: BriefingTone
  title: string
  detail: string
  href?: string
}

export interface BriefingPosition {
  symbol: string
  name: string
  quantity: number
  currentPrice: number | null
  marketValue: number | null
  unrealizedPnl: number | null
  returnPct: number | null
  dailyChangePct: number | null
  quoteDate: string | null
  isLive: boolean
}

export type BriefingRelevantAlert = ScopedBriefingAlert

export interface BriefingTrack {
  id: string
  name: string
  status: 'tracking' | 'paused' | 'closed'
  latest: StrategyObservation | null
  pending: boolean
}

export interface BriefingResearch {
  id: string
  title: string
  subject: string
  status: ResearchEntry['status']
  plan: string
  updatedAt: string
}

export interface DailyBriefing {
  mode: BriefingMode
  generatedAt: string
  asOf: string
  unavailable: string[]
  market: {
    label: string
    score: number | null
    up: number | null
    down: number | null
    flat: number | null
    upPct: number | null
    limitUp: number | null
    limitDown: number | null
    broken: number | null
    leaders: Array<{ name: string; avgPct: number; kind: '概念' | '行业' }>
    recapSummary: string | null
  }
  portfolio: {
    positions: BriefingPosition[]
    marketValue: number
    unrealizedPnl: number
    floatingReturn: number | null
    realizedPnl: number
    unpricedCount: number
    liveCount: number
  }
  alerts: BriefingRelevantAlert[]
  eventGroups: PrioritizedEventGroup[]
  alertCounts: { holding: number; watchlist: number }
  tracks: BriefingTrack[]
  staleTrackCount: number
  research: BriefingResearch[]
  openResearchCount: number
  watchlistCount: number
  focus: BriefingFocusItem[]
}

function localDate(date: Date): string {
  const year = date.getFullYear()
  const month = String(date.getMonth() + 1).padStart(2, '0')
  const day = String(date.getDate()).padStart(2, '0')
  return `${year}-${month}-${day}`
}

function latestObservation(observations: StrategyObservation[]): StrategyObservation | null {
  return observations.reduce<StrategyObservation | null>(
    (latest, item) => (!latest || item.end_date > latest.end_date ? item : latest),
    null,
  )
}

function latestRecap(sources: DailyBriefingSources) {
  return [...sources.recaps].sort((a, b) => (
    b.as_of.localeCompare(a.as_of) || b.created_at.localeCompare(a.created_at)
  ))[0] ?? null
}

function cleanSymbol(symbol: string | undefined): string {
  return (symbol ?? '').trim().toUpperCase()
}

function alertWindowStart(mode: BriefingMode, now: Date): number {
  if (mode === 'morning') return now.getTime() - 24 * 60 * 60 * 1000
  const start = new Date(now)
  start.setHours(0, 0, 0, 0)
  return start.getTime()
}

function focusFromEventGroup(group: PrioritizedEventGroup): BriefingFocusItem {
  const scope = group.scope === 'holding' ? '持仓' : '自选'
  const direction = group.direction === 'risk' ? '风险' : group.direction === 'opportunity' ? '机会' : '观察'
  return {
    id: `event-${group.id}`,
    tone: group.direction === 'risk' ? 'danger' : group.direction === 'opportunity' ? 'positive' : group.level === 'high' ? 'warning' : 'neutral',
    title: `${group.name} · ${scope}${direction}`,
    detail: `优先级 ${group.score} · ${group.primary.message}${group.evidence.length > 1 ? ` · ${group.evidence.length} 条证据` : ''}`,
    href: '/monitor',
  }
}

export function buildDailyBriefing(
  sources: DailyBriefingSources,
  mode: BriefingMode,
  now = new Date(),
): DailyBriefing {
  const recap = latestRecap(sources)
  const asOf = sources.overview?.as_of || recap?.as_of || localDate(now)
  const emptyPortfolio = {
    trades: [],
    positions: [],
    summary: { position_count: 0, trade_count: 0, cost_value: 0, realized_pnl: 0 },
  }
  const portfolioView = buildPortfolioView(sources.portfolio ?? emptyPortfolio, sources.quotes)
  const holdingSymbols = new Set(portfolioView.positions.map(position => cleanSymbol(position.symbol)))
  const watchlistSymbols = new Set(sources.watchlist.map(item => cleanSymbol(item.symbol)))

  const positions = portfolioView.positions.map<BriefingPosition>(position => ({
    symbol: position.symbol,
    name: position.name || position.symbol,
    quantity: position.quantity,
    currentPrice: position.current_price,
    marketValue: position.market_value,
    unrealizedPnl: position.unrealized_pnl,
    returnPct: position.return_pct,
    dailyChangePct: sources.quotes[position.symbol]?.change_pct ?? null,
    quoteDate: position.quote_date,
    isLive: position.is_live,
  })).sort((a, b) => (b.marketValue ?? -1) - (a.marketValue ?? -1))

  const windowStart = alertWindowStart(mode, now)
  const alerts = sources.alerts
    .filter(alert => alert.ts >= windowStart)
    .map<BriefingRelevantAlert | null>(alert => {
      const symbol = cleanSymbol(alert.symbol)
      if (!symbol) return null
      if (holdingSymbols.has(symbol)) return { ...alert, scope: 'holding' }
      if (watchlistSymbols.has(symbol)) return { ...alert, scope: 'watchlist' }
      return null
    })
    .filter((alert): alert is BriefingRelevantAlert => alert !== null)
    .sort((a, b) => b.ts - a.ts)
    .slice(0, 12)

  const tracks = sources.tracks.map<BriefingTrack>(track => {
    const latest = latestObservation(track.observations)
    return {
      id: track.id,
      name: track.strategy_name,
      status: track.status,
      latest,
      pending: track.status === 'tracking' && (!latest || latest.end_date < asOf),
    }
  }).sort((a, b) => Number(b.pending) - Number(a.pending) || a.name.localeCompare(b.name, 'zh-CN'))

  const researchEntries = sources.research
    .filter(entry => entry.status === 'draft' || entry.status === 'tracking')
    .sort((a, b) => a.updated_at.localeCompare(b.updated_at))
  const research = researchEntries.slice(0, 6).map<BriefingResearch>(entry => ({
    id: entry.id,
    title: entry.title,
    subject: entry.subject,
    status: entry.status,
    plan: entry.plan,
    updatedAt: entry.updated_at,
  }))
  const staleTrackCount = tracks.filter(track => track.pending).length
  const eventGroups = prioritizeEvents(alerts, now)
  const focus: BriefingFocusItem[] = eventGroups.slice(0, 3).map(focusFromEventGroup)

  if (portfolioView.summary.unpriced_count > 0) {
    focus.push({
      id: 'unpriced-positions',
      tone: 'warning',
      title: `${portfolioView.summary.unpriced_count} 只持仓缺少行情`,
      detail: '浮动盈亏未覆盖全部持仓，请检查行情同步状态。',
      href: '/portfolio',
    })
  }
  if (staleTrackCount > 0) {
    focus.push({
      id: 'stale-tracks',
      tone: 'warning',
      title: `${staleTrackCount} 项策略快照待更新`,
      detail: `最新交易日为 ${asOf}，跟踪结果尚未全部对齐。`,
      href: '/strategy-tracking',
    })
  }
  const nextResearch = researchEntries.find(entry => entry.plan.trim())
  if (nextResearch) {
    focus.push({
      id: `research-${nextResearch.id}`,
      tone: 'neutral',
      title: nextResearch.title,
      detail: nextResearch.plan,
      href: '/research-ledger',
    })
  }
  if (portfolioView.positions.length === 0 && sources.portfolio) {
    focus.push({
      id: 'empty-portfolio',
      tone: 'neutral',
      title: '尚未录入当前持仓',
      detail: '日报暂时只能按自选股和研究记录筛选提醒。',
      href: '/portfolio',
    })
  }
  if (focus.length === 0) {
    focus.push({
      id: 'all-clear',
      tone: 'positive',
      title: mode === 'morning' ? '盘前暂无紧急事项' : '今日暂无待处理事项',
      detail: '持仓提醒、策略快照和研究动作均无新增异常。',
    })
  }

  const overview = sources.overview
  const leaders = [
    ...(overview?.concept_rank.leading.slice(0, 2).map(item => ({ name: item.name, avgPct: item.avg_pct, kind: '概念' as const })) ?? []),
    ...(overview?.industry_rank.leading.slice(0, 2).map(item => ({ name: item.name, avgPct: item.avg_pct, kind: '行业' as const })) ?? []),
  ]
  const liveCount = positions.filter(position => position.isLive).length
  const floatingReturn = portfolioView.summary.priced_cost_value > 0
    ? portfolioView.summary.unrealized_pnl / portfolioView.summary.priced_cost_value
    : null

  return {
    mode,
    generatedAt: now.toISOString(),
    asOf,
    unavailable: sources.unavailable,
    market: {
      label: overview?.emotion.label || recap?.emotion_label || '暂无情绪数据',
      score: overview?.emotion.score ?? recap?.emotion_score ?? null,
      up: overview?.breadth.up ?? null,
      down: overview?.breadth.down ?? null,
      flat: overview?.breadth.flat ?? null,
      upPct: overview?.breadth.up_pct ?? null,
      limitUp: overview?.limit.limit_up ?? null,
      limitDown: overview?.limit.limit_down ?? null,
      broken: overview?.limit.broken ?? null,
      leaders,
      recapSummary: recap?.as_of === asOf ? recap.summary?.trim() || null : null,
    },
    portfolio: {
      positions,
      marketValue: portfolioView.summary.market_value,
      unrealizedPnl: portfolioView.summary.unrealized_pnl,
      floatingReturn,
      realizedPnl: portfolioView.summary.realized_pnl,
      unpricedCount: portfolioView.summary.unpriced_count,
      liveCount,
    },
    alerts,
    eventGroups,
    alertCounts: {
      holding: alerts.filter(alert => alert.scope === 'holding').length,
      watchlist: alerts.filter(alert => alert.scope === 'watchlist').length,
    },
    tracks,
    staleTrackCount,
    research,
    openResearchCount: researchEntries.length,
    watchlistCount: watchlistSymbols.size,
    focus: focus.slice(0, 6),
  }
}

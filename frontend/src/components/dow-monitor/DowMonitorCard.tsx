import { Trash2 } from 'lucide-react'
import { useEffect, useRef, useState } from 'react'

import { cn } from '@/lib/cn'
import {
  bestBidAsk,
  overlayDowTimeframeState,
  overlayQuote,
} from '@/lib/realtimeOverlays'
import type {
  RealtimeStatus,
  RealtimeSymbolState,
} from '@/lib/realtimeMarketData'

import { DowMiniChart, getLatestValidDowSignalSide } from './DowMiniChart'
import { formatServerTimestamp } from './formatServerTimestamp'
import type {
  DowMonitorNotification,
  DowMonitorOverviewSymbol,
  DowMonitorTimeframeState,
  DowSignalSide,
  DowTimeframe,
} from './types'

const TIMEFRAMES: Array<{ value: DowTimeframe; label: string }> = [
  { value: '5m', label: '5分' },
  { value: '15m', label: '15分' },
  { value: '30m', label: '30分' },
  { value: '60m', label: '60分' },
  { value: 'day', label: '日K' },
]

type VisualState = 'buy' | 'sell' | 'watch' | 'none' | 'blocked'

function visualState(
  state: DowMonitorTimeframeState | undefined,
  forceBlocked: boolean,
): VisualState {
  if (forceBlocked || state?.freshness_state !== 'LIVE') {
    return state || forceBlocked ? 'blocked' : 'none'
  }
  const rawActionCode = state.snapshot?.action_code
  const actionCode = typeof rawActionCode === 'string' ? rawActionCode.toUpperCase() : null
  if (actionCode === 'OPEN_LONG' || actionCode === 'BUY') return 'buy'
  if (
    actionCode === 'OPEN_SHORT'
    || actionCode === 'CLOSE_LONG'
    || actionCode === 'CLOSE_SHORT'
    || actionCode === 'SELL'
    || actionCode === 'RISK'
    || actionCode === 'REDUCE'
  ) return 'sell'
  if (actionCode === 'WATCH') return 'watch'
  const backendSide = getLatestValidDowSignalSide(state.chart)
  if (backendSide === 'BUY') return 'buy'
  if (backendSide === 'SELL' || backendSide === 'RISK') return 'sell'
  return 'none'
}

function stateClass(state: VisualState) {
  switch (state) {
    case 'buy':
      return 'border-emerald-500/30 bg-emerald-500/10 text-emerald-400'
    case 'sell':
      return 'border-red-500/30 bg-red-500/10 text-red-400'
    case 'watch':
      return 'border-amber-500/30 bg-amber-500/10 text-amber-400'
    case 'blocked':
      return 'border-border bg-elevated/50 text-muted opacity-60'
    default:
      return 'border-border bg-elevated/50 text-muted'
  }
}

function signalClass(side: DowSignalSide) {
  return side === 'BUY' ? 'text-emerald-400' : 'text-red-400'
}

type CurrentStateTone = 'good' | 'bad' | 'neutral'
type ForecastItem = {
  title: string
  text: string
}
type FlowSnapshot = {
  netValue: number
  bidNotional: number
  askNotional: number
  turnover: number
  volumeAvgRatio: number | null
}

function currentStateClass(tone: CurrentStateTone) {
  if (tone === 'good') return 'border-emerald-500/25 bg-emerald-500/10 text-emerald-300'
  if (tone === 'bad') return 'border-red-500/25 bg-red-500/10 text-red-300'
  return 'border-border bg-elevated/40 text-secondary'
}

function notional(price?: number, volume?: number) {
  return typeof price === 'number' && typeof volume === 'number' && Number.isFinite(price) && Number.isFinite(volume)
    ? price * volume
    : 0
}

function formatAmount(value: number) {
  if (!Number.isFinite(value) || value <= 0) return '0'
  if (value >= 100_000_000) return `${(value / 100_000_000).toFixed(2)}亿`
  if (value >= 10_000) return `${(value / 10_000).toFixed(0)}万`
  return value.toFixed(0)
}

function formatRatio(value: number | null) {
  if (value == null || !Number.isFinite(value)) return '-'
  return `${value.toFixed(2)}x`
}

function signedPct(value: number | null) {
  if (value == null || !Number.isFinite(value)) return '-'
  return `${value > 0 ? '+' : ''}${value.toFixed(0)}%`
}

function signedPctExact(value: number | null | undefined) {
  if (value == null || !Number.isFinite(value)) return '-'
  return `${value > 0 ? '+' : ''}${value.toFixed(1)}%`
}

function signedAmount(value: number) {
  if (!Number.isFinite(value) || value === 0) return '0'
  return `${value > 0 ? '+' : '-'}${formatAmount(Math.abs(value))}`
}

function formatWanAmount(value: number | null | undefined) {
  if (value == null || !Number.isFinite(value) || value === 0) return '0万'
  return `${value > 0 ? '+' : '-'}${Math.abs(value).toFixed(0)}万`
}

function shortTime(value: string | null | undefined) {
  if (!value) return null
  const match = String(value).match(/(\d{2}:\d{2})/)
  return match?.[1] ?? null
}

function bestCapitalWindow(intradayCapital: DowMonitorOverviewSymbol['intraday_capital']) {
  const usable = usableCapitalWindows(intradayCapital)
  return usable.find(window => window.minutes === 30) ?? usable[0] ?? null
}

function usableCapitalWindows(intradayCapital: DowMonitorOverviewSymbol['intraday_capital']) {
  const windows = intradayCapital?.windows ?? []
  return windows.filter(window =>
    window
    && typeof window.total_net_delta === 'number'
    && Number.isFinite(window.total_net_delta)
    && typeof window.price_change_pct === 'number'
    && Number.isFinite(window.price_change_pct),
  ).sort((left, right) => (left.minutes ?? 0) - (right.minutes ?? 0))
}

function windowRangeText(window: ReturnType<typeof bestCapitalWindow>) {
  if (!window) return null
  const start = shortTime(window.start_time)
  const end = shortTime(window.end_time)
  return start && end ? `${start}-${end}` : window.label ?? null
}

function signedWanDelta(value: number | null | undefined) {
  if (value == null || !Number.isFinite(value)) return '-'
  return value === 0 ? '0万' : formatWanAmount(value)
}

function isFlatDelta(value: number | null | undefined) {
  return value == null || !Number.isFinite(value) || Math.abs(value) < 0.5
}

function capitalContinuityText(
  windows: ReturnType<typeof usableCapitalWindows>,
  activeWindow: ReturnType<typeof bestCapitalWindow>,
) {
  const sampled = windows
    .filter(window => window.minutes != null && window.total_net_delta != null)
    .slice(0, 3)
  if (sampled.length < 2) return null
  const parts = sampled.map(window => {
    const large = window.large_net_delta != null
      ? ` / 大单${signedWanDelta(window.large_net_delta)}`
      : ''
    return `${window.minutes}分 总${signedWanDelta(window.total_net_delta)}${large}`
  })
  const totalDeltas = sampled
    .map(window => window.total_net_delta)
    .filter((value): value is number => value != null && Number.isFinite(value) && !isFlatDelta(value))
  const largeDeltas = sampled
    .map(window => window.large_net_delta)
    .filter((value): value is number => value != null && Number.isFinite(value) && !isFlatDelta(value))
  const totalUp = totalDeltas.filter(value => value > 0).length
  const totalDown = totalDeltas.filter(value => value < 0).length
  const largeUp = largeDeltas.filter(value => value > 0).length
  const largeDown = largeDeltas.filter(value => value < 0).length
  let conclusion = '连续窗口分歧，先看下一段资金是否同向。'
  if (totalUp >= 2 && largeUp >= 2) {
    conclusion = '总资金和大单资金连续修复，短线承接更可信。'
  } else if (totalDown >= 2 && largeDown >= 2) {
    conclusion = '总资金和大单资金连续走弱，卖压仍在延续。'
  } else if (activeWindow?.total_net_delta != null && activeWindow.total_net_delta < 0 && totalUp > 0) {
    conclusion = '短窗口转弱，但长窗口仍有修复痕迹，属于分歧观察。'
  } else if (activeWindow?.total_net_delta != null && activeWindow.total_net_delta > 0 && totalDown > 0) {
    conclusion = '短窗口修复，但长窗口压力未完全解除。'
  }
  return `${parts.join('；')}。${conclusion}`
}

function directionConclusionItem({
  directionLabel,
  probability,
  tone,
  capitalNet,
  largeNet,
  flow30m,
  capitalWindow,
  pressureRatio,
  summary,
}: {
  directionLabel: string
  probability: number
  tone: CurrentStateTone
  capitalNet: number | null
  largeNet: number | null
  flow30m: number | null
  capitalWindow: ReturnType<typeof bestCapitalWindow>
  pressureRatio: number
  summary: ReturnType<typeof currentStateSummaryWithValues>
}): ForecastItem {
  const windowTotalDelta = capitalWindow?.total_net_delta ?? null
  const windowLargeDelta = capitalWindow?.large_net_delta ?? null
  const windowPricePct = capitalWindow?.price_change_pct ?? summary.changePct ?? null
  const windowFundsFlat = isFlatDelta(windowTotalDelta) && isFlatDelta(windowLargeDelta)
  const supports: string[] = []
  const pressures: string[] = []

  if (capitalNet != null && capitalNet > 0) supports.push(`当日资金净流入 ${formatWanAmount(capitalNet)}`)
  if (capitalNet != null && capitalNet < 0) pressures.push(`当日资金净流出 ${formatWanAmount(capitalNet)}`)
  if (largeNet != null && largeNet > 0) supports.push(`大单净流入 ${formatWanAmount(largeNet)}`)
  if (largeNet != null && largeNet < 0) pressures.push(`大单净流出 ${formatWanAmount(largeNet)}`)
  if (!windowFundsFlat && windowTotalDelta != null && windowTotalDelta > 0) supports.push(`窗口资金改善 ${formatWanAmount(windowTotalDelta)}`)
  if (!windowFundsFlat && windowTotalDelta != null && windowTotalDelta < 0) pressures.push(`窗口资金恶化 ${formatWanAmount(windowTotalDelta)}`)
  if (!windowFundsFlat && windowLargeDelta != null && windowLargeDelta > 0) supports.push(`大单改善 ${formatWanAmount(windowLargeDelta)}`)
  if (!windowFundsFlat && windowLargeDelta != null && windowLargeDelta < 0) pressures.push(`大单恶化 ${formatWanAmount(windowLargeDelta)}`)
  if (flow30m != null && flow30m > 0) supports.push(`30分钟资金回补 ${formatWanAmount(flow30m)}`)
  if (flow30m != null && flow30m < 0) pressures.push(`30分钟资金流出 ${formatWanAmount(flow30m)}`)
  if (pressureRatio >= 1.2) supports.push('买盘强于卖盘')
  if (pressureRatio <= 0.83) pressures.push('卖盘强于买盘')
  if (windowPricePct != null && windowPricePct > 0.3) supports.push(`价格同步上涨 ${signedPctExact(windowPricePct)}`)
  if (windowPricePct != null && windowPricePct < -0.3) pressures.push(`价格同步下跌 ${signedPctExact(windowPricePct)}`)
  if (windowFundsFlat) pressures.push('最近资金窗口未更新')

  const dominant = tone === 'good' ? supports : tone === 'bad' ? pressures : [...supports.slice(0, 2), ...pressures.slice(0, 2)]
  const reason = dominant.slice(0, 3).join('，') || summary.reason
  const ending = tone === 'good'
    ? '上涨条件占优。'
    : tone === 'bad'
      ? '下跌风险占优。'
      : '多空证据分歧，先按震荡观察。'

  return {
    title: '分析结论',
    text: `${directionLabel}（${probability}%）：${reason}，${ending}`,
  }
}

function netFlowLabel(value: number, prefix: '累计' | '切片') {
  if (value > 0) return `${prefix}净流入 ${signedAmount(value)}`
  if (value < 0) return `${prefix}净流出 ${signedAmount(value)}`
  return `${prefix}净流入 0`
}

function minuteAggregateKey(
  item: DowMonitorOverviewSymbol,
  realtimeState: RealtimeSymbolState | undefined,
  selectedState: DowMonitorTimeframeState | undefined,
) {
  const raw = realtimeState?.candlestick?.timestamp
    ?? realtimeState?.quote?.timestamp
    ?? realtimeState?.depth?.timestamp
    ?? realtimeState?.eventAt
    ?? selectedState?.source_timestamp
    ?? item.quote_timestamp
    ?? item.last_success_at
    ?? item.updated_at
  return `${item.symbol}:${String(raw ?? '').slice(0, 16)}`
}

function currentStateSummaryWithValues(
  item: DowMonitorOverviewSymbol,
  selectedState: DowMonitorTimeframeState | undefined,
  realtimeState: RealtimeSymbolState | undefined,
  change: number | null,
) {
  const timeframeStates = (['5m', '15m', '30m', '60m'] as DowTimeframe[])
    .map(value => visualState(item.states[value], !item.enabled))
  const buyFrames = timeframeStates.filter(state => state === 'buy').length
  const sellFrames = timeframeStates.filter(state => state === 'sell').length
  const bids = realtimeState?.depth?.bids ?? []
  const asks = realtimeState?.depth?.asks ?? []
  const bidNotional = bids.reduce((sum, level) => sum + notional(level.price, level.volume), 0)
  const askNotional = asks.reduce((sum, level) => sum + notional(level.price, level.volume), 0)
  const maxBid = bids.reduce((max, level) => Math.max(max, notional(level.price, level.volume)), 0)
  const maxAsk = asks.reduce((max, level) => Math.max(max, notional(level.price, level.volume)), 0)
  const netBidAsk = bidNotional - askNotional
  const depthRatio = askNotional > 0 ? bidNotional / askNotional : bidNotional > 0 ? Infinity : 1
  const candle = realtimeState?.candlestick
  const bars = selectedState?.chart?.bars ?? []
  const latestBar = bars[bars.length - 1]
  const previousBar = bars[bars.length - 2]
  const priorBars = bars.slice(Math.max(0, bars.length - 6), Math.max(0, bars.length - 1))
  const latestVolume = typeof candle?.volume === 'number'
    ? candle.volume
    : latestBar && typeof latestBar.volume === 'number'
      ? latestBar.volume
      : null
  const previousVolume = previousBar && typeof previousBar.volume === 'number' ? previousBar.volume : null
  const avgVolume = priorBars.length > 0
    ? priorBars.reduce((sum, bar) => sum + (bar && Number.isFinite(bar.volume) ? bar.volume : 0), 0) / priorBars.length
    : null
  const volumeQoq = latestVolume != null && previousVolume != null && previousVolume > 0
    ? (latestVolume / previousVolume - 1) * 100
    : null
  const volumeAvgRatio = latestVolume != null && avgVolume != null && avgVolume > 0
    ? latestVolume / avgVolume
    : null
  const candleTurnover = typeof candle?.turnover === 'number'
    ? candle.turnover
    : notional(candle?.close, candle?.volume)
  const currentPrice = typeof candle?.close === 'number' && Number.isFinite(candle.close)
    ? candle.close
    : typeof item.last_price === 'number' && Number.isFinite(item.last_price)
      ? item.last_price
      : null
  const recentPriceLows = [
    ...bars.slice(-30).map(bar => typeof bar?.low === 'number' && Number.isFinite(bar.low) ? bar.low : null),
    typeof candle?.low === 'number' && Number.isFinite(candle.low) ? candle.low : null,
  ].filter((value): value is number => value != null && value > 0)
  const recentLow = recentPriceLows.length > 0 ? Math.min(...recentPriceLows) : null
  const reboundFromLowPct = currentPrice != null && recentLow != null
    ? (currentPrice / recentLow - 1) * 100
    : null
  const candleUp = typeof candle?.close === 'number'
    && typeof candle.open === 'number'
    && candle.close >= candle.open
  const candleDown = typeof candle?.close === 'number'
    && typeof candle.open === 'number'
    && candle.close < candle.open
  const activeTurnover = candleTurnover > 0

  let flowLabel = '资金中性'
  if (netBidAsk > 0 && maxBid > maxAsk * 1.5 && depthRatio >= 1.15 && (!activeTurnover || candleUp)) {
    flowLabel = '大单净流入'
  } else if (netBidAsk < 0 && maxAsk > maxBid * 1.5 && depthRatio <= 0.85 && (!activeTurnover || candleDown)) {
    flowLabel = '大单净流出'
  } else if (depthRatio >= 1.2) {
    flowLabel = '买盘占优'
  } else if (depthRatio <= 0.83) {
    flowLabel = '卖盘占优'
  } else if (activeTurnover && candleUp) {
    flowLabel = '分钟偏流入'
  } else if (activeTurnover && candleDown) {
    flowLabel = '分钟偏流出'
  }

  let structureScore = 0
  if (change != null && change > 0.3) structureScore += 1
  if (change != null && change < -0.3) structureScore -= 1
  if (buyFrames >= 2) structureScore += 1
  if (sellFrames >= 2) structureScore -= 1
  let score = structureScore
  if (flowLabel.includes('流入') || flowLabel === '买盘占优') score += 1
  if (flowLabel.includes('流出') || flowLabel === '卖盘占优') score -= 1

  const tone: CurrentStateTone = score >= 2 ? 'good' : score <= -2 ? 'bad' : 'neutral'
  const label = tone === 'good' ? '买入观察' : tone === 'bad' ? '减仓/卖出' : '继续观察'
  const probability = tone === 'neutral'
    ? Math.min(64, 50 + Math.abs(score) * 7)
    : Math.min(92, 56 + Math.abs(score) * 12)
  const reason = buyFrames > sellFrames
    ? `${buyFrames}个周期偏多`
    : sellFrames > buyFrames
      ? `${sellFrames}个周期偏弱`
      : change == null
        ? '等待实时确认'
        : `涨跌${change > 0 ? '+' : ''}${change.toFixed(2)}%`
  return {
    tone,
    label,
    probability,
    flowLabel,
    reason,
    structureScore,
    netValue: netBidAsk,
    bidNotional,
    askNotional,
    turnover: candleTurnover,
    volumeAvgRatio,
    changePct: change,
    currentPrice,
    recentLow,
    reboundFromLowPct,
    sliceText: netFlowLabel(netBidAsk, '切片'),
    depthText: `买${formatAmount(bidNotional)}/卖${formatAmount(askNotional)} 比${formatRatio(depthRatio)}`,
    orderText: `最大买${formatAmount(maxBid)}/卖${formatAmount(maxAsk)}`,
    minuteText: `分钟${formatAmount(candleTurnover)} 环${signedPct(volumeQoq)} 均${formatRatio(volumeAvgRatio)}`,
    featureText: [
      `${netBidAsk >= 0 ? '买盘强于卖盘' : '卖盘强于买盘'} ${formatRatio(depthRatio)}`,
      `量能环比 ${signedPct(volumeQoq)}`,
      `近5均比 ${formatRatio(volumeAvgRatio)}`,
      reason,
    ].join('｜'),
  }
}

function currentStateFromCumulative(
  summary: ReturnType<typeof currentStateSummaryWithValues>,
  cumulative: {
    netValue: number
    bidNotional: number
    askNotional: number
    turnover: number
    avgVolumeRatio: number | null
  },
  intradayCapital: DowMonitorOverviewSymbol['intraday_capital'],
) {
  let score = summary.structureScore
  const capitalWindows = usableCapitalWindows(intradayCapital)
  const capitalWindow = capitalWindows.find(window => window.minutes === 30) ?? capitalWindows[0] ?? null
  const capitalNet = intradayCapital?.total_net ?? intradayCapital?.flow_today ?? null
  const largeNet = intradayCapital?.large_net ?? null
  const flow30m = intradayCapital?.flow_30m ?? capitalWindow?.total_net_delta ?? null
  const hasTradingDayCapital = capitalNet != null || largeNet != null || flow30m != null
  if (hasTradingDayCapital) {
    if ((capitalNet ?? 0) > 0) score += 2
    if ((capitalNet ?? 0) < 0) score -= 2
    if ((largeNet ?? 0) > 0) score += 1
    if ((largeNet ?? 0) < 0) score -= 1
    if ((flow30m ?? 0) > 0) score += 1
    if ((flow30m ?? 0) < 0) score -= 1
  } else {
    if (cumulative.netValue > 0) score += 2
    if (cumulative.netValue < 0) score -= 2
  }
  const cumulativePressureRatio = cumulative.askNotional > 0
    ? cumulative.bidNotional / cumulative.askNotional
    : cumulative.bidNotional > 0
      ? Infinity
      : 1
  const tradingDayPressureRatio = intradayCapital?.total_in != null
    && intradayCapital.total_out != null
    && intradayCapital.total_out > 0
    ? intradayCapital.total_in / intradayCapital.total_out
    : null
  const pressureRatio = tradingDayPressureRatio ?? cumulativePressureRatio
  if (pressureRatio >= 1.2) score += 1
  if (pressureRatio <= 0.83) score -= 1
  if (!hasTradingDayCapital && cumulative.avgVolumeRatio != null && cumulative.avgVolumeRatio >= 1.5) {
    if (cumulative.netValue > 0) score += 1
    if (cumulative.netValue < 0) score -= 1
  }
  const tone: CurrentStateTone = score >= 2 ? 'good' : score <= -2 ? 'bad' : 'neutral'
  const label = tone === 'good' ? '买入观察' : tone === 'bad' ? '减仓/卖出' : '继续观察'
  const probability = tone === 'neutral'
    ? Math.min(64, 50 + Math.abs(score) * 7)
    : Math.min(92, 56 + Math.abs(score) * 12)
  const flowLabel = cumulative.netValue > 0 ? '累计净流入' : cumulative.netValue < 0 ? '累计净流出' : '资金中性'
  const directionLabel = tone === 'good' ? '偏涨' : tone === 'bad' ? '偏跌' : '震荡'
  const probabilityLabel = tone === 'good' ? '上涨概率' : tone === 'bad' ? '下跌概率' : '震荡概率'
  const cumulativeFlowText = hasTradingDayCapital
    ? `${(capitalNet ?? 0) >= 0 ? '当日资金净流入' : '当日资金净流出'} ${formatWanAmount(capitalNet ?? 0)}`
    : netFlowLabel(cumulative.netValue, '累计')
  const latestSliceText = summary.sliceText.replace(/^切片/, '最新切片')
  const pressureText = pressureRatio >= 1.2
    ? '买盘明显强于卖盘'
    : pressureRatio <= 0.83
      ? '卖盘明显强于买盘'
      : '买卖盘接近平衡'
  const volumeText = cumulative.avgVolumeRatio != null && cumulative.avgVolumeRatio >= 1.5
    ? '量能放大'
    : cumulative.avgVolumeRatio != null && cumulative.avgVolumeRatio <= 0.8
      ? '量能偏弱'
      : '量能一般'
  const flowReason = hasTradingDayCapital
    ? capitalNet != null && capitalNet > 0
      ? '当日资金累计净流入'
      : capitalNet != null && capitalNet < 0
        ? '当日资金累计净流出'
        : '当日资金累计中性'
    : cumulative.netValue > 0
      ? '页面累计净流入'
      : cumulative.netValue < 0
        ? '页面累计净流出'
        : '页面累计中性'
  const capitalEvidence = hasTradingDayCapital
    ? [
        cumulativeFlowText,
        `大单 ${formatWanAmount(largeNet)}`,
        `30分钟 ${formatWanAmount(flow30m)}`,
        latestSliceText,
      ]
    : [
        cumulativeFlowText,
        `累计买卖比 ${formatRatio(cumulativePressureRatio)}`,
        `累计均比 ${formatRatio(cumulative.avgVolumeRatio)}`,
        latestSliceText,
      ]
  const explanationItems = realtimeForecastItems({
    summary,
    hasTradingDayCapital,
    capitalNet,
    largeNet,
    flow15m: intradayCapital?.flow_15m ?? null,
    flow30m,
    capitalWindow,
    capitalWindows,
    pressureRatio,
  })
  const forecastItems = [
    directionConclusionItem({
      directionLabel,
      probability,
      tone,
      capitalNet,
      largeNet,
      flow30m,
      capitalWindow,
      pressureRatio,
      summary,
    }),
    ...explanationItems,
  ]
  return {
    ...summary,
    tone,
    label,
    probability,
    directionLabel,
    probabilityLabel,
    flowLabel,
    conciseReason: `${flowReason}，${pressureText}，${volumeText}`,
    forecastItems,
    evidenceText: capitalEvidence.join('；'),
    latestSliceText,
    cumulativePressureText: `累计买卖比 ${formatRatio(cumulativePressureRatio)}`,
    cumulativeTurnoverText: `累计成交 ${formatAmount(cumulative.turnover)}`,
    cumulativeVolumeText: `累计均比 ${formatRatio(cumulative.avgVolumeRatio)}`,
  }
}

function realtimeForecastItems({
  summary,
  hasTradingDayCapital,
  capitalNet,
  largeNet,
  flow15m,
  flow30m,
  capitalWindow,
  capitalWindows,
  pressureRatio,
}: {
  summary: ReturnType<typeof currentStateSummaryWithValues>
  hasTradingDayCapital: boolean
  capitalNet: number | null
  largeNet: number | null
  flow15m: number | null
  flow30m: number | null
  capitalWindow: ReturnType<typeof bestCapitalWindow>
  capitalWindows: ReturnType<typeof usableCapitalWindows>
  pressureRatio: number
}): ForecastItem[] {
  const deepDrop = summary.changePct != null && summary.changePct <= -3
  const rebound = summary.reboundFromLowPct ?? 0
  const repairedFlow = Math.max(flow15m ?? Number.NEGATIVE_INFINITY, flow30m ?? Number.NEGATIVE_INFINITY)
  const hasCapitalRepair = hasTradingDayCapital && repairedFlow > 0
  const sliceSupport = summary.netValue > 0 || pressureRatio >= 1.1
  const volumeSupport = summary.volumeAvgRatio != null && summary.volumeAvgRatio >= 1.3
  const repairAmount = flow30m ?? repairedFlow
  const lowToCurrentText = summary.currentPrice != null && summary.recentLow != null
    ? `从低点 ${summary.recentLow.toFixed(2)} 拉到 ${summary.currentPrice.toFixed(2)}，脱离低点 ${signedPctExact(rebound)}`
    : `脱离低点 ${signedPctExact(rebound)}`
  const windowRange = windowRangeText(capitalWindow)
  const windowPricePct = capitalWindow?.price_change_pct ?? null
  const windowTotalDelta = capitalWindow?.total_net_delta ?? null
  const windowLargeDelta = capitalWindow?.large_net_delta ?? null
  const windowFundsFlat = isFlatDelta(windowTotalDelta) && isFlatDelta(windowLargeDelta)
  const continuityText = capitalContinuityText(capitalWindows, capitalWindow)
  const windowRepairAmount = windowTotalDelta ?? repairAmount
  const totalWindowText = capitalWindow
    && capitalWindow.start_total_net != null
    && capitalWindow.end_total_net != null
    && windowTotalDelta != null
    ? `${windowRange ? `${windowRange}，` : ''}总资金从 ${formatWanAmount(capitalWindow.start_total_net)} 修复到 ${formatWanAmount(capitalWindow.end_total_net)}，净改善 ${formatWanAmount(windowTotalDelta)}`
    : `当前总资金仍为 ${formatWanAmount(capitalNet)}，近30分钟净改善 ${formatWanAmount(repairAmount)}`
  const largeWindowText = capitalWindow
    && capitalWindow.start_large_net != null
    && capitalWindow.end_large_net != null
    && windowLargeDelta != null
    ? `大单资金净流仍为 ${formatWanAmount(largeNet)}，但${windowRange ? `${windowRange} ` : ''}从 ${formatWanAmount(capitalWindow.start_large_net)} 修复到 ${formatWanAmount(capitalWindow.end_large_net)}，净改善 ${formatWanAmount(windowLargeDelta)}，说明砸盘力量在变弱。`
    : `大单资金净流仍为 ${formatWanAmount(largeNet)}，但近段资金回补 ${formatWanAmount(windowRepairAmount)}，说明砸盘力量在变弱。`
  const priceCapitalSyncText = capitalWindow && windowPricePct != null
    ? `${windowRange ? `${windowRange}，` : ''}价格 ${signedPctExact(windowPricePct)}，资金同步改善 ${formatWanAmount(windowRepairAmount)}；${lowToCurrentText}，比单纯价格反弹更可信。`
    : `${lowToCurrentText}，同时资金在修复，比单纯价格反弹更可信。`

  if (deepDrop && rebound >= 1 && hasCapitalRepair) {
    const largeText = largeNet != null && largeNet < 0
      ? largeWindowText
      : largeNet != null && largeNet > 0
        ? `大单已转为净流入 ${formatWanAmount(largeNet)}，拉升主动性更强。`
        : '大单方向等待确认，但近段资金已经开始修复。'
    return [
      {
        title: '下跌后卖压衰减',
        text: largeText,
      },
      {
        title: '总资金开始修复',
        text: `${totalWindowText}。`,
      },
      {
        title: '价格和资金开始同步改善',
        text: priceCapitalSyncText,
      },
      {
        title: '低位承接出现',
        text: volumeSupport || sliceSupport
          ? '不是一根瞬间拉升，而是低位持续承接，盘口/量能开始配合。'
          : '价格已经从低位修复，但还需要盘口和量能继续确认。',
      },
      {
        title: '但还不是强势反转',
        text: `总资金/大单仍未完全转正，更准确标签：下跌承接 / 资金修复 / 卖压衰减 / 弱转强观察。`,
      },
    ]
  }

  if (deepDrop && rebound >= 1 && sliceSupport) {
    return [
      {
        title: '低位反抽预判',
        text: `${lowToCurrentText}，最新盘口出现承接：${summary.sliceText}。`,
      },
      {
        title: '资金仍需确认',
        text: volumeSupport ? '量能已有配合，但缺少当日资金累计确认。' : '等待资金和量能继续确认。',
      },
    ]
  }

  if (capitalWindow && windowTotalDelta != null) {
    const improved = windowTotalDelta > 0
    const largeImproved = windowLargeDelta != null && windowLargeDelta > 0
    const windowPriceText = windowPricePct != null ? signedPctExact(windowPricePct) : '-'
    const currentCapitalText = [
      capitalWindow.end_total_net != null ? `总资金 ${formatWanAmount(capitalWindow.end_total_net)}` : null,
      capitalWindow.end_large_net != null ? `大单资金 ${formatWanAmount(capitalWindow.end_large_net)}` : null,
    ].filter(Boolean).join('，')
    if (windowFundsFlat) {
      return [
        {
          title: '资金未更新',
          text: `${windowRange ? `${windowRange}，` : ''}价格 ${windowPriceText}；${currentCapitalText || '资金源'}在该窗口未变化，可能是收盘后或资金源停止更新，不按该切片判断转弱。`,
        },
        ...(continuityText
          ? [{
              title: '连续性观察',
              text: continuityText,
            }]
          : []),
        {
          title: '当前判断',
          text: '资金没有新增变化，当前只保留当日累计状态；等待下一次有效资金更新后再确认短线强弱。',
        },
      ]
    }
    const totalText = capitalWindow.start_total_net != null && capitalWindow.end_total_net != null
      ? `总资金从 ${formatWanAmount(capitalWindow.start_total_net)} 到 ${formatWanAmount(capitalWindow.end_total_net)}，${improved ? '改善' : '恶化'} ${formatWanAmount(windowTotalDelta)}`
      : `总资金${improved ? '改善' : '恶化'} ${formatWanAmount(windowTotalDelta)}`
    const largeText = capitalWindow.start_large_net != null && capitalWindow.end_large_net != null && windowLargeDelta != null
      ? `大单资金从 ${formatWanAmount(capitalWindow.start_large_net)} 到 ${formatWanAmount(capitalWindow.end_large_net)}，${largeImproved ? '改善' : '恶化'} ${formatWanAmount(windowLargeDelta)}`
      : largeNet != null
        ? `当前大单资金 ${formatWanAmount(largeNet)}`
        : '大单变化等待确认'
    return [
      {
        title: improved ? '时间切片改善' : '时间切片转弱',
        text: `${windowRange ? `${windowRange}，` : ''}价格 ${windowPriceText}；${totalText}；${largeText}。`,
      },
      ...(continuityText
        ? [{
            title: '连续性观察',
            text: continuityText,
          }]
        : [{
            title: largeImproved ? '大单卖压收窄' : '大单压力未解',
            text: largeText,
          }]),
      {
        title: '当前判断',
        text: improved && (capitalNet ?? 0) < 0
          ? '资金仍为负，但短线正在修复，属于弱转强观察；需要继续看总资金和大单能否延续收窄。'
          : improved
            ? '资金和价格同向改善，短线状态偏好；如果大单也同步改善，信号可信度更高。'
            : '价格或资金在最近窗口走弱，先按偏弱/观察处理，等待下一段切片修复。',
      },
    ]
  }

  if (hasTradingDayCapital && (capitalNet ?? 0) < 0 && hasCapitalRepair) {
    return [
      {
        title: '资金修复预判',
        text: `当日资金仍为负 ${formatWanAmount(capitalNet)}，但近段资金回补 ${formatWanAmount(repairedFlow)}。`,
      },
      {
        title: '等待大单确认',
        text: '若大单继续收窄或转正，方向有机会改善。',
      },
    ]
  }

  if (hasTradingDayCapital && (capitalNet ?? 0) > 0 && (largeNet ?? 0) > 0 && pressureRatio >= 1) {
    return [
      {
        title: '主动拉升预判',
        text: `当日资金 ${formatWanAmount(capitalNet)}，大单 ${formatWanAmount(largeNet)}，价格和资金同步偏强。`,
      },
    ]
  }

  if (summary.changePct != null && summary.changePct > 0 && (capitalNet ?? 0) < 0) {
    return [
      {
        title: '拉高风险预警',
        text: '价格反弹但当日资金仍流出，更像技术反抽或托价。',
      },
    ]
  }

  return [
    {
      title: '等待确认',
      text: `${hasTradingDayCapital ? '资金修复不明显' : '缺少当日资金累计'}，${summary.reason}。`,
    },
  ]
}

function blockedLabel(
  item: DowMonitorOverviewSymbol,
  state: DowMonitorTimeframeState | undefined,
  forceBlocked: boolean,
  blockedReason?: string,
) {
  if (forceBlocked) return blockedReason ?? '监控状态不可用'
  if (!item.enabled) return '监控已暂停'
  if (state?.freshness_state === 'STALE_DATA') return '数据延迟'
  if (state?.freshness_state === 'ANALYSIS_PAUSED') return '分析暂停'
  return null
}

export function DowMonitorCard({
  item,
  notifications,
  onOpen,
  onToggle,
  onRemove,
  onRead,
  forceBlocked = false,
  blockedReason,
  quoteReady = true,
  notificationLoading = false,
  notificationError = false,
  togglePending = false,
  removePending = false,
  readPendingIds,
  realtimeState,
  realtimeStatus,
}: {
  item: DowMonitorOverviewSymbol
  notifications: DowMonitorNotification[]
  onOpen: (symbol: string, timeframe: DowTimeframe) => void
  onToggle: (symbol: string, enabled: boolean) => void
  onRemove: (symbol: string) => void
  onRead?: (notificationId: string) => void
  forceBlocked?: boolean
  blockedReason?: string
  quoteReady?: boolean
  notificationLoading?: boolean
  notificationError?: boolean
  togglePending?: boolean
  removePending?: boolean
  readPendingIds?: ReadonlySet<string>
  realtimeState?: RealtimeSymbolState
  realtimeStatus?: RealtimeStatus
}) {
  const [timeframe, setTimeframe] = useState<DowTimeframe>('5m')
  const displayedItem = overlayQuote(item, realtimeState)
  const selectedState = overlayDowTimeframeState(
    displayedItem.states[timeframe],
    realtimeState,
  )
  const blocked = blockedLabel(item, selectedState, forceBlocked, blockedReason)
  const quoteAvailable = quoteReady || Boolean(realtimeState?.quote)
  const price = quoteAvailable
    && typeof displayedItem.last_price === 'number'
    && Number.isFinite(displayedItem.last_price)
    ? displayedItem.last_price
    : null
  const change = quoteAvailable
    && typeof displayedItem.change_pct === 'number'
    && Number.isFinite(displayedItem.change_pct)
    ? displayedItem.change_pct * 100
    : null
  const priceDirectionClass = change == null
    ? 'text-foreground'
    : change > 0
      ? 'text-bull'
      : change < 0
        ? 'text-bear'
        : 'text-muted'
  const name = typeof item.name === 'string' && item.name.trim() && item.name.trim() !== item.symbol
    ? item.name.trim()
    : null
  const quoteTime = quoteAvailable
    ? formatServerTimestamp(displayedItem.quote_timestamp)
    : null
  const successTime = quoteReady ? formatServerTimestamp(item.last_success_at) : null
  const { bid, ask } = bestBidAsk(realtimeState?.depth)
  const realtimeDelayed = Boolean(
    realtimeState?.quoteDelayed
    || realtimeState?.depthDelayed
    || realtimeState?.candlestickDelayed,
  )
  const liveCurrentState = currentStateSummaryWithValues(
    displayedItem,
    selectedState,
    realtimeState,
    change,
  )
  const currentStateKey = minuteAggregateKey(displayedItem, realtimeState, selectedState)
  const initialFlowSnapshot = {
    netValue: liveCurrentState.netValue,
    bidNotional: liveCurrentState.bidNotional,
    askNotional: liveCurrentState.askNotional,
    turnover: liveCurrentState.turnover,
    volumeAvgRatio: liveCurrentState.volumeAvgRatio,
  }
  const flowSnapshotsRef = useRef<{ symbol: string; values: Map<string, FlowSnapshot> }>({
    symbol: displayedItem.symbol,
    values: new Map([[currentStateKey, initialFlowSnapshot]]),
  })
  const [currentStateSnapshot, setCurrentStateSnapshot] = useState(() => ({
    summary: liveCurrentState,
    cumulative: {
      netValue: liveCurrentState.netValue,
      bidNotional: liveCurrentState.bidNotional,
      askNotional: liveCurrentState.askNotional,
      turnover: liveCurrentState.turnover,
      avgVolumeRatio: liveCurrentState.volumeAvgRatio,
    },
  }))
  useEffect(() => {
    if (flowSnapshotsRef.current.symbol !== displayedItem.symbol) {
      flowSnapshotsRef.current = { symbol: displayedItem.symbol, values: new Map() }
    }
    if (!flowSnapshotsRef.current.values.has(currentStateKey)) {
      flowSnapshotsRef.current.values.set(currentStateKey, {
        netValue: liveCurrentState.netValue,
        bidNotional: liveCurrentState.bidNotional,
        askNotional: liveCurrentState.askNotional,
        turnover: liveCurrentState.turnover,
        volumeAvgRatio: liveCurrentState.volumeAvgRatio,
      })
    }
    const snapshots = [...flowSnapshotsRef.current.values.values()]
    const cumulative = snapshots.reduce(
      (result, value) => ({
        netValue: result.netValue + value.netValue,
        bidNotional: result.bidNotional + value.bidNotional,
        askNotional: result.askNotional + value.askNotional,
        turnover: result.turnover + value.turnover,
        volumeRatioSum: result.volumeRatioSum + (value.volumeAvgRatio ?? 0),
        volumeRatioCount: result.volumeRatioCount + (value.volumeAvgRatio == null ? 0 : 1),
      }),
      {
        netValue: 0,
        bidNotional: 0,
        askNotional: 0,
        turnover: 0,
        volumeRatioSum: 0,
        volumeRatioCount: 0,
      },
    )
    setCurrentStateSnapshot({
      summary: liveCurrentState,
      cumulative: {
        netValue: cumulative.netValue,
        bidNotional: cumulative.bidNotional,
        askNotional: cumulative.askNotional,
        turnover: cumulative.turnover,
        avgVolumeRatio: cumulative.volumeRatioCount > 0
          ? cumulative.volumeRatioSum / cumulative.volumeRatioCount
          : null,
      },
    })
  }, [currentStateKey])
  const currentState = currentStateFromCumulative(
    currentStateSnapshot.summary,
    currentStateSnapshot.cumulative,
    displayedItem.intraday_capital,
  )
  return (
    <article
      data-testid={`card-${item.symbol}`}
      data-tradable={blocked ? 'false' : 'true'}
      className={cn(
        'group relative min-w-0 overflow-hidden rounded-card border bg-surface transition-colors hover:border-accent/40',
        blocked ? 'border-border/70 opacity-75' : 'border-border',
      )}
    >
      <div
        data-testid={`card-summary-${item.symbol}`}
        data-layout="compact-two-row"
        className="grid grid-cols-[minmax(0,1fr)_auto_auto] grid-rows-2 items-center gap-x-2 px-2.5 py-1.5"
      >
        <div className="flex min-w-0 items-center gap-2">
          <span className="shrink-0 font-mono text-sm font-semibold tracking-wide">
            {item.symbol}
          </span>
          {name && <span className="truncate text-xs text-secondary">{name}</span>}
        </div>

        <button
          type="button"
          role="switch"
          aria-label={`${item.symbol} 监控开关`}
          aria-checked={item.enabled}
          disabled={togglePending}
          onClick={() => onToggle(item.symbol, !item.enabled)}
          className={cn(
            'relative col-start-2 row-start-1 h-[18px] w-8 shrink-0 rounded-full transition-colors disabled:cursor-wait disabled:opacity-50',
            item.enabled ? 'bg-accent/70' : 'bg-border',
          )}
        >
          <span
            className={cn(
              'absolute top-0.5 h-3.5 w-3.5 rounded-full bg-white transition-transform',
              item.enabled ? 'translate-x-0' : '-translate-x-4',
            )}
            style={{ right: 2 }}
          />
        </button>

        <button
          type="button"
          aria-label={`移除 ${item.symbol}`}
          disabled={removePending}
          onClick={() => onRemove(item.symbol)}
          className="col-start-3 row-start-1 rounded p-0.5 text-muted transition-colors hover:bg-danger/10 hover:text-danger disabled:cursor-wait disabled:opacity-50"
        >
          <Trash2 className="h-3.5 w-3.5" />
        </button>

        <div className="col-span-3 row-start-2 mt-0.5 flex min-w-0 items-baseline gap-2 overflow-hidden">
          <span className={cn(
            'shrink-0 font-mono text-[16px] tabular-nums',
            priceDirectionClass,
          )}>
            {price == null ? '—' : price.toFixed(2)}
          </span>
          {change != null && (
            <span className={cn(
              'shrink-0 font-mono text-[10px] tabular-nums',
              priceDirectionClass,
            )}>
              {change > 0 ? '+' : ''}{change.toFixed(2)}%
            </span>
          )}
          {(quoteTime || successTime) && (
            <span className="ml-auto flex min-w-0 gap-2 overflow-hidden font-mono text-[9px] text-muted">
              {quoteTime && <span className="whitespace-nowrap">行情 {quoteTime}</span>}
              {successTime && <span className="whitespace-nowrap">成功 {successTime}</span>}
            </span>
          )}
          {(bid != null || ask != null) && (
            <span className="shrink-0 font-mono text-[9px] text-muted">
              买一 {bid?.toFixed(2) ?? '—'} · 卖一 {ask?.toFixed(2) ?? '—'}
            </span>
          )}
          {realtimeStatus && (realtimeState || realtimeStatus !== 'realtime') && (
            <span
              className={cn(
                'shrink-0 rounded px-1 py-0.5 text-[9px]',
                realtimeStatus === 'realtime' && !realtimeDelayed
                  ? 'bg-emerald-500/10 text-emerald-400'
                  : 'bg-amber-500/10 text-amber-400',
              )}
            >
              {realtimeDelayed
                ? '延迟'
                : realtimeStatus === 'realtime'
                  ? '实时'
                  : realtimeStatus === 'connecting'
                    ? '连接中'
                    : 'HTTP 回退'}
            </span>
          )}
        </div>
      </div>

      <div className="grid grid-cols-5 gap-1 px-2.5 pb-1">
        {TIMEFRAMES.map(option => {
          const state = item.states[option.value]
          const currentVisualState = visualState(state, forceBlocked || !item.enabled)
          return (
            <button
              key={option.value}
              type="button"
              aria-label={option.label}
              aria-pressed={timeframe === option.value}
              data-tradable={currentVisualState === 'blocked' ? 'false' : 'true'}
              onClick={() => setTimeframe(option.value)}
              className={cn(
                'h-5 rounded border text-[9px] font-medium transition-colors',
                stateClass(currentVisualState),
                timeframe === option.value && 'ring-1 ring-accent/70',
              )}
            >
              {option.label}
            </button>
          )
        })}
      </div>

      <button
        type="button"
        aria-label={`打开 ${item.symbol} 完整K线`}
        onClick={() => onOpen(item.symbol, timeframe)}
        className="block w-full border-y border-border/50 px-1 text-left focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-accent"
      >
        <DowMiniChart
          chart={selectedState?.chart ?? {}}
          testId={`mini-chart-${item.symbol}-${timeframe}`}
          height={180}
          showLines={false}
        />
      </button>

      <div className="px-2.5 py-1.5">
        <div
          data-testid={`realtime-state-${item.symbol}`}
          className={cn(
            'min-w-0 rounded border px-2 py-1.5 text-[10px]',
            currentStateClass(currentState.tone),
          )}
        >
          <div className="flex min-w-0 flex-wrap items-center gap-x-1.5 gap-y-0.5">
            <span className="shrink-0 font-medium">方向：{currentState.directionLabel}</span>
            <span className="shrink-0 font-medium">{currentState.probabilityLabel}</span>
            <span className="shrink-0 font-mono tabular-nums">{currentState.probability}%</span>
          </div>
          <div className="mt-1 text-[9px] opacity-90">
            主因：{currentState.conciseReason}
          </div>
          <div className="mt-1 space-y-0.5 text-[9px] opacity-90">
            {currentState.forecastItems.map(item => (
              <div key={item.title}>
                <span className="font-medium">{item.title}：</span>
                <span>{item.text}</span>
              </div>
            ))}
          </div>
          <div className="mt-0.5 text-[9px] opacity-80">
            证据：{currentState.evidenceText}
          </div>
        </div>
      </div>

      <section
        role="log"
        aria-label={`${item.symbol} 消息通知`}
        className="h-32 overflow-y-auto border-t border-border/60 bg-base/20 px-2.5 py-1.5"
      >
        {blocked && (
          <div className="mb-1 text-[10px] font-medium text-muted">{blocked}</div>
        )}
        {notificationLoading && (
          <div className="mb-1 text-center text-[10px] text-muted">正在加载通知</div>
        )}
        {notificationError && (
          <div className="mb-1 text-center text-[10px] text-danger">通知加载失败</div>
        )}
        {notifications.length === 0 && !notificationLoading && !notificationError ? (
          <div className="flex h-full items-center justify-center text-[10px] text-muted">
            暂无消息通知
          </div>
        ) : notifications.length > 0 ? (
          <div>
            {notifications.map((notification, index) => {
              const isLatest = index === 0
              const triggerTimeframe = TIMEFRAMES.find(
                option => option.value === notification.timeframe,
              )?.label ?? notification.timeframe
              const triggerTime = formatServerTimestamp(notification.triggered_at)
              const triggerPrice = Number.isFinite(notification.trigger_price)
                ? notification.trigger_price.toFixed(2)
                : null
              return (
                <div
                  key={notification.notification_id}
                  data-testid={`card-message-${notification.notification_id}`}
                  className={cn(
                    'border-b border-border/50 py-1.5 last:border-b-0',
                    isLatest && 'border-l-2 border-l-accent pl-2',
                  )}
                >
                  <div className="flex min-w-0 items-center gap-1.5 text-[10px]">
                    {isLatest && (
                      <span className="shrink-0 text-[9px] font-medium text-accent">最新</span>
                    )}
                    <span className={cn('shrink-0 font-medium', signalClass(notification.side))}>
                      {notification.action_name}
                    </span>
                    <span className="shrink-0 font-mono text-[9px] text-secondary">
                      周期 {triggerTimeframe}
                    </span>
                    <span className="truncate text-secondary">{notification.shape_name}</span>
                    {notification.read_at == null && onRead && (
                      <button
                        type="button"
                        aria-label={`标记 ${notification.symbol} 已读`}
                        disabled={readPendingIds?.has(notification.notification_id)}
                        onClick={() => onRead(notification.notification_id)}
                        className="ml-auto shrink-0 text-[9px] text-muted underline-offset-2 hover:text-foreground hover:underline disabled:cursor-wait disabled:opacity-50"
                      >
                        已读
                      </button>
                    )}
                  </div>
                  <div className="mt-0.5 flex min-w-0 items-center gap-1.5 font-mono text-[9px] text-muted">
                    <span className="shrink-0">触发 {triggerTime ?? '—'}</span>
                    <span className="shrink-0 text-foreground">@{triggerPrice ?? '—'}</span>
                  </div>
                </div>
              )
            })}
          </div>
        ) : null}
      </section>
    </article>
  )
}

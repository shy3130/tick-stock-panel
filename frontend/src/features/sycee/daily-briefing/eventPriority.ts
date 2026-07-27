import type { BriefingAlert } from './api.ts'

export type EventScope = 'holding' | 'watchlist'
export type EventDirection = 'risk' | 'opportunity' | 'observe'
export type EventPriorityLevel = 'high' | 'medium' | 'normal'

export interface ScopedBriefingAlert extends BriefingAlert {
  scope: EventScope
}

export interface EventScoreReason {
  key: string
  label: string
  points: number
}

export interface PrioritizedEventGroup {
  id: string
  symbol: string
  name: string
  scope: EventScope
  direction: EventDirection
  score: number
  level: EventPriorityLevel
  reasons: EventScoreReason[]
  latestTs: number
  primary: ScopedBriefingAlert
  evidence: ScopedBriefingAlert[]
}

const RISK_TYPES = new Set([
  'sell_signal',
  'pool_exit',
  'dropped',
  'exit',
  '炸板预警',
  '翘板预警',
])
const OPPORTUNITY_TYPES = new Set([
  'buy_signal',
  'pool_entry',
  'new_entry',
  'entry',
])

export function eventDirection(type: string): EventDirection {
  if (RISK_TYPES.has(type)) return 'risk'
  if (OPPORTUNITY_TYPES.has(type)) return 'opportunity'
  return 'observe'
}

function recencyReason(timestamp: number, now: Date): EventScoreReason | null {
  const age = Math.max(0, now.getTime() - timestamp)
  if (age <= 60 * 60 * 1000) return { key: 'recency', label: '1小时内', points: 10 }
  if (age <= 6 * 60 * 60 * 1000) return { key: 'recency', label: '6小时内', points: 7 }
  if (age <= 24 * 60 * 60 * 1000) return { key: 'recency', label: '24小时内', points: 4 }
  return null
}

export function scoreEvent(alert: ScopedBriefingAlert, now = new Date()): {
  score: number
  reasons: EventScoreReason[]
  direction: EventDirection
} {
  const direction = eventDirection(alert.type)
  const reasons: EventScoreReason[] = [
    alert.scope === 'holding'
      ? { key: 'scope', label: '当前持仓', points: 35 }
      : { key: 'scope', label: '自选关注', points: 15 },
  ]

  if (alert.severity === 'critical') reasons.push({ key: 'severity', label: '严重告警', points: 25 })
  else if (alert.severity === 'warn') reasons.push({ key: 'severity', label: '警告级别', points: 15 })
  else reasons.push({ key: 'severity', label: '普通提醒', points: 5 })

  if (direction === 'risk') {
    reasons.push({
      key: 'direction',
      label: '风险/退出',
      points: alert.scope === 'holding' ? 20 : 10,
    })
  } else if (direction === 'opportunity') {
    reasons.push({
      key: 'direction',
      label: '机会/进入',
      points: alert.scope === 'watchlist' ? 12 : 6,
    })
  }

  const conditionCount = alert.conditions?.length ?? 0
  if (conditionCount > 0) {
    reasons.push({
      key: 'conditions',
      label: `${conditionCount}项条件命中`,
      points: Math.min(8, conditionCount * 4),
    })
  }
  const signalCount = alert.signals?.length ?? 0
  if (signalCount > 0) {
    reasons.push({
      key: 'signals',
      label: `${signalCount}个信号`,
      points: Math.min(8, signalCount * 3),
    })
  }
  const recency = recencyReason(alert.ts, now)
  if (recency) reasons.push(recency)

  return {
    score: Math.min(100, reasons.reduce((total, reason) => total + reason.points, 0)),
    reasons,
    direction,
  }
}

function priorityLevel(score: number): EventPriorityLevel {
  if (score >= 70) return 'high'
  if (score >= 45) return 'medium'
  return 'normal'
}

function evidenceKey(alert: ScopedBriefingAlert): string {
  return `${alert.source}:${alert.rule_id || alert.strategy_id || alert.type}`
}

export function prioritizeEvents(
  alerts: ScopedBriefingAlert[],
  now = new Date(),
): PrioritizedEventGroup[] {
  const grouped = new Map<string, ScopedBriefingAlert[]>()
  for (const alert of alerts) {
    const symbol = (alert.symbol ?? '').trim().toUpperCase()
    if (!symbol) continue
    const key = `${symbol}:${eventDirection(alert.type)}`
    grouped.set(key, [...(grouped.get(key) ?? []), alert])
  }

  return Array.from(grouped.entries()).map(([id, evidence]) => {
    const sortedEvidence = [...evidence].sort((a, b) => b.ts - a.ts)
    const ranked = sortedEvidence
      .map(alert => ({ alert, result: scoreEvent(alert, now) }))
      .sort((a, b) => b.result.score - a.result.score || b.alert.ts - a.alert.ts)
    const primary = ranked[0]
    const independentCount = new Set(sortedEvidence.map(evidenceKey)).size
    const confirmationPoints = Math.min(12, Math.max(0, independentCount - 1) * 6)
    const reasons = [...primary.result.reasons]
    if (confirmationPoints > 0) {
      reasons.push({
        key: 'confirmation',
        label: `${independentCount}条独立证据`,
        points: confirmationPoints,
      })
    }
    const score = Math.min(100, primary.result.score + confirmationPoints)
    return {
      id,
      symbol: (primary.alert.symbol ?? '').trim().toUpperCase(),
      name: primary.alert.name?.trim() || primary.alert.symbol || '',
      scope: primary.alert.scope,
      direction: primary.result.direction,
      score,
      level: priorityLevel(score),
      reasons,
      latestTs: sortedEvidence[0].ts,
      primary: primary.alert,
      evidence: sortedEvidence,
    }
  }).sort((a, b) => b.score - a.score || b.latestTs - a.latestTs)
}

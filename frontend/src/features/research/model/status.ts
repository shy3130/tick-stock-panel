import { pickEnum } from './parse'
import type { BadgeTone } from '@/components/ui/Primitives'

export const JOB_STATUSES = ['pending', 'running', 'interrupted', 'completed', 'failed', 'cancelled'] as const
export type JobStatus = (typeof JOB_STATUSES)[number]

export const RESEARCH_VERDICTS = ['accepted', 'rejected', 'unavailable', 'inconclusive'] as const
export type ResearchVerdict = (typeof RESEARCH_VERDICTS)[number]

export const DATA_STATUSES = ['ready', 'partial', 'missing', 'stale', 'censored'] as const
export type DataStatus = (typeof DATA_STATUSES)[number]

export const PROMOTION_STATUSES = ['not_promoted', 'candidate', 'promoted'] as const
export type PromotionStatus = (typeof PROMOTION_STATUSES)[number]

export const ENGINEERING_STATUSES = ['completed', 'partial', 'planned'] as const
export type EngineeringStatus = (typeof ENGINEERING_STATUSES)[number]

export const RESULT_PROFILES = [
  'arm_comparison',
  'event_signal',
  'shape_distribution',
  'retrieval',
  'calendar_effect',
] as const
export type ResultProfile = (typeof RESULT_PROFILES)[number]

export const SCOPE_TYPES = ['symbols', 'full_market'] as const
export type ScopeType = (typeof SCOPE_TYPES)[number]

export const FACTOR_CATEGORIES = [
  'intraday',
  'daily',
  'weekly',
  'shape',
  'risk',
  'routing',
  'exclusion',
  'other',
] as const
export type FactorCategory = (typeof FACTOR_CATEGORIES)[number] | string

export type RunScope =
  | { type: 'symbols'; symbols: string[] }
  | { type: 'full_market' }

export const JOB_STATUS_META: Record<JobStatus, { label: string; tone: BadgeTone }> = {
  pending: { label: '排队', tone: 'muted' },
  running: { label: '运行中', tone: 'accent' },
  interrupted: { label: '已中断', tone: 'warning' },
  completed: { label: '已完成', tone: 'success' },
  failed: { label: '失败', tone: 'danger' },
  cancelled: { label: '已取消', tone: 'muted' },
}

export const VERDICT_META: Record<ResearchVerdict, { label: string; tone: BadgeTone }> = {
  accepted: { label: '接受', tone: 'success' },
  rejected: { label: '拒绝', tone: 'danger' },
  unavailable: { label: '不可用', tone: 'warning' },
  inconclusive: { label: '无结论', tone: 'muted' },
}

export const DATA_STATUS_META: Record<DataStatus, { label: string; tone: BadgeTone }> = {
  ready: { label: '可用', tone: 'success' },
  partial: { label: '部分', tone: 'warning' },
  missing: { label: '缺失', tone: 'danger' },
  stale: { label: '过期', tone: 'warning' },
  censored: { label: '删失', tone: 'warning' },
}

export const PROMOTION_META: Record<PromotionStatus, { label: string; tone: BadgeTone }> = {
  not_promoted: { label: '未晋级', tone: 'muted' },
  candidate: { label: '候选', tone: 'accent' },
  promoted: { label: '已晋级', tone: 'success' },
}

export const ENGINEERING_META: Record<EngineeringStatus, { label: string; tone: BadgeTone }> = {
  completed: { label: '完成', tone: 'success' },
  partial: { label: '部分', tone: 'warning' },
  planned: { label: '计划', tone: 'muted' },
}

export const PROFILE_META: Record<ResultProfile, { label: string }> = {
  arm_comparison: { label: 'Arm 对照' },
  event_signal: { label: '事件信号' },
  shape_distribution: { label: '形态分布' },
  retrieval: { label: '检索路由' },
  calendar_effect: { label: '日历效应' },
}

export function parseJobStatus(value: unknown): JobStatus | null {
  return pickEnum(value, JOB_STATUSES)
}

export function parseVerdict(value: unknown): ResearchVerdict | null {
  return pickEnum(value, RESEARCH_VERDICTS)
}

export function parseDataStatus(value: unknown): DataStatus | null {
  return pickEnum(value, DATA_STATUSES)
}

export function parsePromotionStatus(value: unknown): PromotionStatus | null {
  return pickEnum(value, PROMOTION_STATUSES)
}

export function parseEngineeringStatus(value: unknown): EngineeringStatus | null {
  return pickEnum(value, ENGINEERING_STATUSES)
}

export function parseResultProfile(value: unknown): ResultProfile | null {
  return pickEnum(value, RESULT_PROFILES)
}

export function parseScope(value: unknown): RunScope | null {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null
  const record = value as Record<string, unknown>
  const type = pickEnum(record.type, SCOPE_TYPES)
  if (type === 'full_market') return { type: 'full_market' }
  if (type === 'symbols') {
    const symbols = Array.isArray(record.symbols)
      ? record.symbols.filter((item): item is string => typeof item === 'string' && Boolean(item.trim()))
      : []
    return { type: 'symbols', symbols }
  }
  return null
}

export function scopeLabel(scope: RunScope | null | undefined): string {
  if (!scope) return '—'
  if (scope.type === 'full_market') return '全市场'
  if (scope.symbols.length === 0) return '标的（空）'
  if (scope.symbols.length <= 2) return scope.symbols.join(' ')
  return `${scope.symbols[0]} 等 ${scope.symbols.length} 只`
}

export function scopeShort(scopes: readonly string[] | undefined): string {
  const set = new Set(scopes ?? [])
  const parts: string[] = []
  if (set.has('symbols')) parts.push('S')
  if (set.has('full_market')) parts.push('FM')
  return parts.length ? parts.join('/') : '—'
}

export function isActiveJob(status: JobStatus | null | undefined): boolean {
  return status === 'pending' || status === 'running'
}

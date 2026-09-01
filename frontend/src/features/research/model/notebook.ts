import { asArray, asBoolean, asRecord, asString, asStringArray } from './parse'
import { parseScope, type RunScope } from './status'

export const HYPOTHESIS_STATUS_VALUES = [
  'exploring',
  'testing',
  'validated',
  'rejected',
  'monitoring',
] as const
export type ResearchHypothesisStatus = (typeof HYPOTHESIS_STATUS_VALUES)[number]

export const EVIDENCE_KIND_VALUES = ['backtest', 'note', 'observation', 'factor_run'] as const
export type ResearchEvidenceKind = (typeof EVIDENCE_KIND_VALUES)[number]

export const RECAP_SCHEDULE_TEMPLATES = [
  'market_recap_daily',
  'watchlist_recap_daily',
  'strategy_pool_weekly',
] as const
export type RecapScheduleTemplate = (typeof RECAP_SCHEDULE_TEMPLATES)[number]

export const FACTOR_RUN_TEMPLATE = 'factor_run' as const

export const RESEARCH_SCHEDULE_TEMPLATES = [...RECAP_SCHEDULE_TEMPLATES, FACTOR_RUN_TEMPLATE] as const
export type ResearchScheduleTemplate = (typeof RESEARCH_SCHEDULE_TEMPLATES)[number]

export interface ResearchEvidence {
  ts: string
  kind: ResearchEvidenceKind | string
  ref: string
  summary: string
}

export interface ResearchHypothesis {
  id: string
  title: string
  thesis: string
  status: ResearchHypothesisStatus | string
  tags: string[]
  evidence: ResearchEvidence[]
  created_at: string
  updated_at: string
}

export interface ResearchRunCard {
  run_id: string
  kind: string
  config: Record<string, unknown>
  config_hash: string
  strategy_hash: string
  stats: Record<string, unknown>
  created_at: string
}

export interface ResearchSchedule {
  id: string
  name: string
  template: ResearchScheduleTemplate | string
  cron: string
  enabled: boolean
  params: Record<string, unknown>
  created_at: string
  updated_at: string
  last_run_at: string | null
  last_status: string | null
  last_error: string | null
}

export interface ResearchScheduleRunResult {
  title?: string
  summary?: string
  artifacts?: unknown[]
  warnings?: string[]
  [key: string]: unknown
}

export interface ResearchScheduleRunNowResponse {
  schedule: ResearchSchedule
  result: ResearchScheduleRunResult
}

export interface FactorRunScheduleParams {
  factor_id: string
  scope: RunScope
  parameters: Record<string, unknown>
}

export interface CreateHypothesisBody {
  title: string
  thesis: string
  status?: ResearchHypothesisStatus | string
  tags?: string[]
}

export interface UpdateHypothesisBody {
  title?: string
  thesis?: string
  status?: ResearchHypothesisStatus | string
  tags?: string[]
}

export interface AddEvidenceBody {
  kind: ResearchEvidenceKind | string
  ref?: string
  summary: string
}

export interface CreateScheduleBody {
  name: string
  template: ResearchScheduleTemplate | string
  cron: string
  enabled?: boolean
  params?: Record<string, unknown>
}

export interface UpdateScheduleBody {
  name?: string
  template?: ResearchScheduleTemplate | string
  cron?: string
  enabled?: boolean
  params?: Record<string, unknown>
}

export interface ConfirmTSuitabilityBody {
  pool_id: string
  as_of: string
  limit: number
}

export function isHypothesisStatus(value: string): value is ResearchHypothesisStatus {
  return (HYPOTHESIS_STATUS_VALUES as readonly string[]).includes(value)
}

export function isEvidenceKind(value: string): value is ResearchEvidenceKind {
  return (EVIDENCE_KIND_VALUES as readonly string[]).includes(value)
}

export function isScheduleTemplate(value: string): value is ResearchScheduleTemplate {
  return (RESEARCH_SCHEDULE_TEMPLATES as readonly string[]).includes(value)
}

export function isRecapScheduleTemplate(value: string): value is RecapScheduleTemplate {
  return (RECAP_SCHEDULE_TEMPLATES as readonly string[]).includes(value)
}

export function isFactorRunTemplate(value: string): boolean {
  return value === FACTOR_RUN_TEMPLATE
}

export function parseEvidence(value: unknown): ResearchEvidence | null {
  const rec = asRecord(value)
  if (!rec) return null
  const summary = asString(rec.summary) ?? ''
  const kind = asString(rec.kind) ?? 'note'
  return {
    ts: asString(rec.ts) ?? '',
    kind,
    ref: asString(rec.ref) ?? '',
    summary,
  }
}

export function parseHypothesis(value: unknown): ResearchHypothesis | null {
  const rec = asRecord(value)
  const id = rec ? asString(rec.id) : null
  if (!rec || !id) return null
  return {
    id,
    title: asString(rec.title) ?? '',
    thesis: asString(rec.thesis) ?? '',
    status: asString(rec.status) ?? 'exploring',
    tags: asStringArray(rec.tags),
    evidence: asArray(rec.evidence).map(parseEvidence).filter((item): item is ResearchEvidence => item !== null),
    created_at: asString(rec.created_at) ?? '',
    updated_at: asString(rec.updated_at) ?? '',
  }
}

export function parseHypothesisList(payload: unknown): { items: ResearchHypothesis[] } {
  const rec = asRecord(payload)
  const rows = rec ? asArray(rec.items ?? rec.hypotheses) : asArray(payload)
  return {
    items: rows.map(parseHypothesis).filter((item): item is ResearchHypothesis => item !== null),
  }
}

export function parseRunCard(value: unknown): ResearchRunCard | null {
  if (value == null) return null
  const rec = asRecord(value)
  const runId = rec ? asString(rec.run_id ?? rec.id) : null
  if (!rec || !runId) return null
  // Factor durable runs are not recap run-cards; require the C2 card envelope.
  if (asRecord(rec.files) || rec.job_status != null || rec.result_profile != null) return null
  return {
    run_id: runId,
    kind: asString(rec.kind) ?? '',
    config: asRecord(rec.config) ?? {},
    config_hash: asString(rec.config_hash) ?? '',
    strategy_hash: asString(rec.strategy_hash) ?? '',
    stats: asRecord(rec.stats) ?? {},
    created_at: asString(rec.created_at) ?? '',
  }
}

export function parseSchedule(value: unknown): ResearchSchedule | null {
  const rec = asRecord(value)
  const id = rec ? asString(rec.id) : null
  if (!rec || !id) return null
  const template = asString(rec.template) ?? ''
  return {
    id,
    name: asString(rec.name) ?? '',
    template,
    cron: asString(rec.cron) ?? '',
    enabled: asBoolean(rec.enabled) ?? false,
    params: asRecord(rec.params) ?? {},
    created_at: asString(rec.created_at) ?? '',
    updated_at: asString(rec.updated_at) ?? '',
    last_run_at: asString(rec.last_run_at),
    last_status: asString(rec.last_status),
    last_error: asString(rec.last_error),
  }
}

export function parseScheduleList(payload: unknown): { items: ResearchSchedule[] } {
  const rec = asRecord(payload)
  const rows = rec ? asArray(rec.items ?? rec.schedules) : asArray(payload)
  return {
    items: rows.map(parseSchedule).filter((item): item is ResearchSchedule => item !== null),
  }
}

export function parseScheduleRunNow(payload: unknown): ResearchScheduleRunNowResponse {
  const rec = asRecord(payload)
  const schedule = rec ? parseSchedule(rec.schedule) : null
  if (!schedule) throw new Error('定时运行未返回 schedule')
  const resultRec = asRecord(rec?.result) ?? {}
  return {
    schedule,
    result: {
      title: asString(resultRec.title) ?? undefined,
      summary: asString(resultRec.summary) ?? undefined,
      artifacts: asArray(resultRec.artifacts),
      warnings: asStringArray(resultRec.warnings),
      ...resultRec,
    },
  }
}

export function parseFactorRunScheduleParams(value: unknown): FactorRunScheduleParams | null {
  const rec = asRecord(value)
  if (!rec) return null
  const keys = Object.keys(rec)
  if (keys.length !== 3 || !keys.includes('factor_id') || !keys.includes('scope') || !keys.includes('parameters')) {
    return null
  }
  const factorId = asString(rec.factor_id)
  const scope = parseScope(rec.scope)
  const parameters = asRecord(rec.parameters)
  if (!factorId || !scope || !parameters) return null
  return { factor_id: factorId, scope, parameters }
}

export function factorRunScheduleParams(input: FactorRunScheduleParams): Record<string, unknown> {
  return {
    factor_id: input.factor_id,
    scope: input.scope,
    parameters: input.parameters,
  }
}

export function extractScheduledRunId(result: ResearchScheduleRunResult | null | undefined): string | null {
  const summary = result?.summary
  if (!summary) return null
  const match = /run_id=([A-Za-z0-9_-]+)/.exec(summary)
  return match?.[1] ?? null
}

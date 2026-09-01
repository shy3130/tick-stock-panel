import { parseHypothesis, type ResearchHypothesis } from './notebook'
import { asArray, asCursor, asNumber, asRecord, asString } from './parse'
import { parseArtifacts, parseProvenance, parseUnavailableReasons, type ArtifactAvailability, type ProvenanceBlock } from './provenance'
import {
  parseArmRows,
  parseEventRows,
  parseHorizonRows,
  parseNormalizedResult,
  parseRisk,
  type ArmRow,
  type EventRow,
  type HorizonRow,
  type NormalizedResearchResult,
  type RiskBlock,
} from './result'
import {
  parseDataStatus,
  parseJobStatus,
  parsePromotionStatus,
  parseResultProfile,
  parseScope,
  parseVerdict,
  type DataStatus,
  type JobStatus,
  type PromotionStatus,
  type ResearchVerdict,
  type ResultProfile,
  type RunScope,
} from './status'

export { scopeLabel } from './status'
export type { RunScope } from './status'

export interface RunWindow {
  start: string | null
  end: string | null
  oos_start: string | null
}

export interface RunLinks {
  self: string | null
  stream: string | null
  events: string | null
  series: string | null
}

export interface WarningItem {
  code: string
  message: string
}

export interface UnavailableReason {
  code: string
  message: string
  observed: number | null
  required: number | null
}

export interface ResearchRunSummary {
  run_id: string
  factor_id: string
  factor_title: string | null
  job_status: JobStatus | null
  verdict: ResearchVerdict | null
  data_status: DataStatus | null
  promotion_status: PromotionStatus
  scope: RunScope | null
  parameters: Record<string, unknown>
  label: string | null
  favorite: boolean
  source_run_id: string | null
  created_at: string | null
  started_at: string | null
  finished_at: string | null
  duration_ms: number | null
  sample_count: number | null
  baseline: string | null
  window: RunWindow | null
  result_profile: ResultProfile | null
  links: RunLinks
}

export interface ResearchRunDetail extends ResearchRunSummary {
  request: { factor_id: string; scope: RunScope | null; parameters: Record<string, unknown> }
  summary: Record<string, unknown>
  arms: ArmRow[]
  horizons: HorizonRow[]
  risk: RiskBlock | null
  provenance: ProvenanceBlock
  warnings: WarningItem[]
  unavailable_reasons: UnavailableReason[]
  artifacts: ArtifactAvailability
  result: NormalizedResearchResult | null
  progress: { stage: string | null; ratio: number | null; message: string | null } | null
  hypotheses: ResearchHypothesis[]
}

export interface RunListQuery {
  factor_id?: string
  job_status?: string
  verdict?: string
  scope_type?: string
  favorite?: boolean
  cursor?: string
  limit?: number
}

export interface RunListPage {
  items: ResearchRunSummary[]
  next_cursor: string | null
}

export interface EventListQuery {
  cursor?: string
  limit?: number
  symbol?: string
  arm?: string
  qualified?: boolean
  reachable?: boolean
  censor_code?: string
  date?: string
}

export interface EventListPage {
  items: EventRow[]
  next_cursor: string | null
}

export interface CreateRunRequest {
  factor_id: string
  scope: RunScope
  parameters: Record<string, unknown>
  source_run_id?: string | null
}

export interface CreatedRun {
  run_id: string
  job_status: JobStatus | null
  factor_id: string
  scope: RunScope | null
  created_at: string | null
  links: RunLinks
}

export interface RunStreamEvent {
  type: 'snapshot' | 'progress' | 'warning' | 'interrupted' | 'completed' | 'failed' | 'cancelled' | 'heartbeat'
  id: string | null
  job_status: JobStatus | null
  stage: string | null
  ratio: number | null
  message: string | null
  payload: Record<string, unknown>
}

export function parseRunList(payload: unknown): RunListPage {
  const rec = asRecord(payload)
  const rows = rec ? asArray(rec.items ?? rec.runs) : asArray(payload)
  return {
    items: rows.map(parseRunSummary).filter((item): item is ResearchRunSummary => item !== null),
    next_cursor: rec ? asCursor(rec.next_cursor ?? rec.cursor) : null,
  }
}

export function parseRunSummary(value: unknown): ResearchRunSummary | null {
  const rec = asRecord(value)
  const runId = rec ? asString(rec.run_id ?? rec.id) : null
  if (!rec || !runId) return null
  const parameters = asRecord(rec.parameters) ?? asRecord(asRecord(rec.request)?.parameters) ?? {}
  const scope = parseScope(rec.scope) ?? parseScope(asRecord(rec.request)?.scope)
  return {
    run_id: runId,
    factor_id: asString(rec.factor_id) ?? '',
    factor_title: asString(rec.factor_title ?? rec.title),
    job_status: parseJobStatus(rec.job_status ?? rec.status),
    verdict: parseVerdict(rec.verdict),
    data_status: parseDataStatus(rec.data_status),
    promotion_status: parsePromotionStatus(rec.promotion_status) ?? 'not_promoted',
    scope,
    parameters,
    label: asString(rec.label),
    favorite: rec.favorite === true,
    source_run_id: asString(rec.source_run_id),
    created_at: asString(rec.created_at),
    started_at: asString(rec.started_at),
    finished_at: asString(rec.finished_at ?? rec.completed_at),
    duration_ms: asNumber(rec.duration_ms ?? rec.elapsed_ms),
    sample_count: asNumber(rec.sample_count ?? rec.samples),
    baseline: asString(rec.baseline),
    window: parseWindow(rec.window, parameters),
    result_profile: parseResultProfile(rec.result_profile ?? rec.profile),
    links: parseLinks(rec.links, runId),
  }
}

export function parseRunDetail(payload: unknown): ResearchRunDetail {
  const summary = parseRunSummary(payload)
  if (!summary) throw new Error('运行详情缺少 run_id')
  const rec = asRecord(payload) ?? {}
  const requestRec = asRecord(rec.request)
  const summaryRec = asRecord(rec.summary)
  const resultSource = rec.result
    ?? (summaryRec && (summaryRec.payload != null || (summaryRec.profile != null && summaryRec.run_id == null)) ? rec.summary : null)
    ?? (asRecord(rec.payload) ? rec : null)
  const result = parseNormalizedResult(resultSource, summary.result_profile)
  const metricsSummary = summaryRec && summaryRec.payload == null && summaryRec.profile == null
    ? summaryRec
    : asRecord(asRecord(resultSource)?.summary) ?? result?.summary ?? {}
  const progressRec = asRecord(rec.progress)
  const resultRec = asRecord(resultSource)
  const artifactSource = rec.artifacts ?? rec.artifact_availability ?? rec.artifact
  return {
    ...summary,
    request: {
      factor_id: asString(requestRec?.factor_id) ?? summary.factor_id,
      scope: parseScope(requestRec?.scope) ?? summary.scope,
      parameters: asRecord(requestRec?.parameters) ?? summary.parameters,
    },
    summary: metricsSummary,
    arms: parseArmRows(rec.arms ?? (result && result.profile === 'arm_comparison' ? result.arms : [])),
    horizons: parseHorizonRows(rec.horizons ?? rec.horizon ?? (result && result.profile === 'arm_comparison' ? result.horizons : [])),
    risk: parseRisk(rec.risk ?? (result && result.profile === 'arm_comparison' ? result.risk : null)),
    provenance: parseProvenance(rec.provenance ?? resultRec?.provenance ?? rec.identity ?? resultRec?.identity),
    warnings: asArray(rec.warnings ?? resultRec?.warnings).map((item) => {
      if (typeof item === 'string') return { code: 'warning', message: item }
      const row = asRecord(item)
      if (!row) return null
      return { code: asString(row.code) ?? 'warning', message: asString(row.message ?? row.detail) ?? '警告' }
    }).filter((item): item is WarningItem => item !== null),
    unavailable_reasons: parseUnavailableReasons(rec.unavailable_reasons ?? resultRec?.unavailable_reasons),
    artifacts: parseArtifacts(artifactSource),
    result,
    progress: progressRec
      ? {
          stage: asString(progressRec.stage ?? progressRec.label),
          ratio: parseRatio(progressRec),
          message: asString(progressRec.message),
        }
      : null,
    hypotheses: asArray(rec.hypotheses).map(parseHypothesis).filter((item): item is ResearchHypothesis => item !== null),
  }
}

export function parseCreatedRun(payload: unknown): CreatedRun {
  const rec = asRecord(payload)
  const runId = rec ? asString(rec.run_id ?? rec.id) : null
  if (!rec || !runId) throw new Error('创建运行未返回 run_id')
  return {
    run_id: runId,
    job_status: parseJobStatus(rec.job_status ?? rec.status),
    factor_id: asString(rec.factor_id) ?? '',
    scope: parseScope(rec.scope),
    created_at: asString(rec.created_at),
    links: parseLinks(rec.links, runId),
  }
}

export function parseEventPage(payload: unknown): EventListPage {
  const rec = asRecord(payload)
  const rows = rec ? asArray(rec.items ?? rec.events) : asArray(payload)
  return {
    items: parseEventRows(rows),
    next_cursor: rec ? asCursor(rec.next_cursor ?? rec.cursor) : null,
  }
}

export function parseStreamEvent(type: string, id: string | null, data: unknown): RunStreamEvent {
  const rec = asRecord(data) ?? {}
  const inner = asRecord(rec.payload) ?? rec
  const named = asString(rec.event_type) ?? type
  const normalizedType = (
    named === 'snapshot' || named === 'progress' || named === 'warning' || named === 'interrupted'
    || named === 'completed' || named === 'failed' || named === 'cancelled' || named === 'heartbeat'
  ) ? named : 'snapshot'
  return {
    type: normalizedType,
    id: asCursor(id ?? rec.seq ?? inner.seq),
    job_status: parseJobStatus(inner.job_status ?? rec.job_status ?? rec.status ?? inner.status),
    stage: asString(inner.stage ?? rec.stage ?? inner.label),
    ratio: parseRatio(inner) ?? parseRatio(rec),
    message: asString(inner.message ?? rec.message),
    payload: inner,
  }
}

function parseRatio(rec: Record<string, unknown> | null): number | null {
  if (!rec) return null
  const percent = asNumber(rec.percent)
  if (percent != null) return Math.min(1, Math.max(0, percent / 100))
  const raw = asNumber(rec.ratio ?? rec.progress ?? rec.pct)
  if (raw == null) return null
  return raw > 1 ? Math.min(1, raw / 100) : raw
}

function parseLinks(value: unknown, runId: string): RunLinks {
  const rec = asRecord(value)
  return {
    self: asString(rec?.self) ?? `/api/research/runs/${encodeURIComponent(runId)}`,
    stream: asString(rec?.stream) ?? `/api/research/runs/${encodeURIComponent(runId)}/stream`,
    events: asString(rec?.events) ?? `/api/research/runs/${encodeURIComponent(runId)}/events`,
    series: asString(rec?.series) ?? `/api/research/runs/${encodeURIComponent(runId)}/series`,
  }
}

function parseWindow(value: unknown, parameters: Record<string, unknown>): RunWindow | null {
  const rec = asRecord(value)
  const start = asString(rec?.start) ?? asString(parameters.start)
  const end = asString(rec?.end) ?? asString(parameters.end)
  const oos = asString(rec?.oos_start) ?? asString(parameters.oos_start)
  if (!start && !end && !oos) return null
  return { start, end, oos_start: oos }
}

export function windowLabel(window: RunWindow | null | undefined): string {
  if (!window) return '—'
  if (window.start && window.end) {
    return window.oos_start ? `${window.start} → ${window.end} / OOS ${window.oos_start}` : `${window.start} → ${window.end}`
  }
  return window.start || window.end || '—'
}

export function parseRunLinkResponse(payload: unknown): { run_id: string; hypothesis: ResearchHypothesis } {
  const rec = asRecord(payload)
  const runId = rec ? asString(rec.run_id) : null
  const hypothesis = rec ? parseHypothesis(rec.hypothesis) : null
  if (!runId || !hypothesis) throw new Error('关联假设未返回 run_id 或 hypothesis')
  return { run_id: runId, hypothesis }
}

import { parseSeriesPoints, type SeriesPoint } from '../model/result'
import {
  parseCreatedRun,
  parseEventPage,
  parseRunDetail,
  parseRunLinkResponse,
  parseRunList,
  parseStreamEvent,
  type CreatedRun,
  type CreateRunRequest,
  type EventListPage,
  type EventListQuery,
  type ResearchRunDetail,
  type RunListPage,
  type RunListQuery,
  type RunStreamEvent,
} from '../model/run'
import type { ResearchHypothesis } from '../model/notebook'
import { jsonBody, researchRequest } from './transport'

function runListQuery(filters: RunListQuery): string {
  const params = new URLSearchParams()
  if (filters.factor_id) params.set('factor_id', filters.factor_id)
  if (filters.job_status) params.set('job_status', filters.job_status)
  if (filters.verdict) params.set('verdict', filters.verdict)
  if (filters.scope_type) params.set('scope.type', filters.scope_type)
  if (filters.favorite === true) params.set('favorite', 'true')
  if (filters.cursor) params.set('cursor', filters.cursor)
  params.set('limit', String(filters.limit ?? 50))
  return `?${params.toString()}`
}

function eventQuery(filters: EventListQuery): string {
  const params = new URLSearchParams()
  if (filters.cursor) params.set('cursor', filters.cursor)
  params.set('limit', String(Math.min(filters.limit ?? 200, 200)))
  if (filters.symbol) params.set('symbol', filters.symbol)
  if (filters.arm) params.set('arm', filters.arm)
  if (filters.qualified != null) params.set('qualified', String(filters.qualified))
  if (filters.reachable != null) params.set('reachable', String(filters.reachable))
  if (filters.censor_code) params.set('censor_code', filters.censor_code)
  if (filters.date) params.set('date', filters.date)
  return `?${params.toString()}`
}

export function listRuns(filters: RunListQuery = {}): Promise<RunListPage> {
  return researchRequest(`/api/research/runs${runListQuery(filters)}`, undefined, parseRunList)
}

export function getRun(runId: string): Promise<ResearchRunDetail> {
  return researchRequest(`/api/research/runs/${encodeURIComponent(runId)}`, undefined, parseRunDetail)
}

export function createRun(body: CreateRunRequest): Promise<CreatedRun> {
  return researchRequest('/api/research/runs', jsonBody(body), parseCreatedRun)
}

export function patchRun(runId: string, body: { label?: string; favorite?: boolean }): Promise<ResearchRunDetail> {
  return researchRequest(
    `/api/research/runs/${encodeURIComponent(runId)}`,
    { method: 'PATCH', body: JSON.stringify(body) },
    parseRunDetail,
  )
}

export function cancelRun(runId: string): Promise<ResearchRunDetail> {
  return researchRequest(
    `/api/research/runs/${encodeURIComponent(runId)}/cancellation`,
    { method: 'POST' },
    parseRunDetail,
  )
}

export function linkRunHypothesis(runId: string, hypothesisId: string): Promise<{ run_id: string; hypothesis: ResearchHypothesis }> {
  return researchRequest(
    `/api/research/runs/${encodeURIComponent(runId)}/links`,
    jsonBody({ hypothesis_id: hypothesisId }),
    parseRunLinkResponse,
  )
}

export function listRunEvents(runId: string, filters: EventListQuery = {}): Promise<EventListPage> {
  return researchRequest(
    `/api/research/runs/${encodeURIComponent(runId)}/events${eventQuery(filters)}`,
    undefined,
    parseEventPage,
  )
}

export function getRunSeries(runId: string, kind?: string): Promise<SeriesPoint[]> {
  const params = new URLSearchParams()
  if (kind) params.set('kind', kind)
  params.set('max_points', '2000')
  return researchRequest(
    `/api/research/runs/${encodeURIComponent(runId)}/series?${params.toString()}`,
    undefined,
    parseSeriesPoints,
  )
}

export function openRunStream(runId: string, lastEventId?: string): EventSource {
  const params = new URLSearchParams()
  if (lastEventId) params.set('last_event_id', lastEventId)
  const q = params.toString()
  const url = `/api/research/runs/${encodeURIComponent(runId)}/stream${q ? `?${q}` : ''}`
  return new EventSource(url)
}

export function readStreamMessage(type: string, event: MessageEvent): RunStreamEvent {
  let payload: unknown = event.data
  if (typeof event.data === 'string' && event.data) {
    try {
      payload = JSON.parse(event.data)
    } catch {
      payload = { message: event.data }
    }
  }
  return parseStreamEvent(type || 'snapshot', event.lastEventId || null, payload)
}

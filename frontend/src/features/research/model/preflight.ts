import { asArray, asBoolean, asNumber, asRecord, asString, asStringArray } from './parse'
import { parseScope, type RunScope } from './status'

export interface PreflightSource {
  kind: string
  status: string
  generation: string | null
  manifest_sha256: string | null
  available_from: string | null
  available_to: string | null
  message: string | null
}

export interface PreflightReason {
  code: string
  message: string
  source: string | null
  observed: number | null
  required: number | null
}

export interface PreflightCohort {
  requested_symbols: number | null
  eligible_symbols: number | null
  censored_symbols: number | null
}

export interface PreflightResource {
  class: string | null
  full_market_supported: boolean | null
}

export interface PreflightResult {
  ready: boolean
  factor_id: string
  normalized_request: Record<string, unknown>
  sources: PreflightSource[]
  cohort: PreflightCohort | null
  warnings: PreflightReason[]
  blocking_reasons: PreflightReason[]
  resource_estimate: PreflightResource | null
  scope: RunScope | null
}

export interface PreflightRequest {
  factor_id: string
  scope: RunScope
  parameters: Record<string, unknown>
}

export function parsePreflight(payload: unknown): PreflightResult {
  const rec = asRecord(payload)
  if (!rec) throw new Error('预检响应无效')
  return {
    ready: rec.ready === true,
    factor_id: asString(rec.factor_id) ?? '',
    normalized_request: asRecord(rec.normalized_request) ?? {},
    sources: asArray(rec.sources).map(parseSource).filter((item): item is PreflightSource => item !== null),
    cohort: parseCohort(rec.cohort),
    warnings: asArray(rec.warnings).map(parseReason).filter((item): item is PreflightReason => item !== null),
    blocking_reasons: asArray(rec.blocking_reasons).map(parseReason).filter((item): item is PreflightReason => item !== null),
    resource_estimate: parseResource(rec.resource_estimate),
    scope: parseScope(rec.scope),
  }
}

function parseSource(value: unknown): PreflightSource | null {
  const rec = asRecord(value)
  if (!rec) return null
  return {
    kind: asString(rec.kind) ?? 'unknown',
    status: asString(rec.status) ?? 'unknown',
    generation: asString(rec.generation),
    manifest_sha256: asString(rec.manifest_sha256),
    available_from: asString(rec.available_from),
    available_to: asString(rec.available_to),
    message: asString(rec.message),
  }
}

export function parseReason(value: unknown): PreflightReason | null {
  if (typeof value === 'string') return { code: value, message: value, source: null, observed: null, required: null }
  const rec = asRecord(value)
  if (!rec) return null
  const message = asString(rec.message) ?? asString(rec.code) ?? '未说明原因'
  return {
    code: asString(rec.code) ?? 'unknown',
    message,
    source: asString(rec.source),
    observed: asNumber(rec.observed),
    required: asNumber(rec.required),
  }
}

function parseCohort(value: unknown): PreflightCohort | null {
  const rec = asRecord(value)
  if (!rec) return null
  return {
    requested_symbols: asNumber(rec.requested_symbols),
    eligible_symbols: asNumber(rec.eligible_symbols),
    censored_symbols: asNumber(rec.censored_symbols),
  }
}

function parseResource(value: unknown): PreflightResource | null {
  const rec = asRecord(value)
  if (!rec) return null
  return {
    class: asString(rec.resource_class) ?? asString(rec.class),
    full_market_supported: asBoolean(rec.full_market_supported),
  }
}

export function reasonsFromErrorDetails(details: Record<string, unknown>): PreflightReason[] {
  const rows = asArray(details.blocking_reasons)
  if (rows.length) {
    return rows.map(parseReason).filter((item): item is PreflightReason => item !== null)
  }
  return asStringArray(details.reasons).map((message) => ({
    code: 'preflight_blocked',
    message,
    source: null,
    observed: null,
    required: null,
  }))
}

import { asArray, asRecord, asString, asStringArray } from './parse'
import { readUiGroups, parseJsonSchema, type JsonSchemaNode } from './schema'
import {
  parseDataStatus,
  parseEngineeringStatus,
  parsePromotionStatus,
  parseResultProfile,
  parseVerdict,
  type DataStatus,
  type EngineeringStatus,
  type FactorCategory,
  type PromotionStatus,
  type ResearchVerdict,
  type ResultProfile,
  type ScopeType,
} from './status'

/** Public IDs from design §5.3. Catalog rendering always comes from GET /factors. */
export const PUBLIC_FACTOR_IDS = [
  'n-shape',
  'mtf-direction',
  'weak-to-strong',
  'volume-breakout',
  'macd-arms',
  'single-yang-no-break',
  'zuoyi-defense',
  'daily-open-anchor',
  'hold-firm',
  'dugu-trend',
  'mera',
  'pre-surge',
  'escape-risk',
  'n-depth',
  'negative-exclusion',
  'doji-patterns',
  'chip-peak-patterns',
  'weekly-flagpole',
  'escape-windows',
] as const

export type PublicFactorId = (typeof PUBLIC_FACTOR_IDS)[number]

export interface FactorLatestRun {
  run_id: string
  created_at: string | null
  job_status: string | null
  verdict: ResearchVerdict | null
}

export interface FactorScopeCapability {
  type: ScopeType
  supported: boolean
  notes: string | null
  unavailable_capabilities: string[]
}

export interface FactorCatalogItem {
  id: string
  title: string
  category: FactorCategory
  description: string
  engineering_status: EngineeringStatus | null
  latest_data_status: DataStatus | null
  latest_verdict: ResearchVerdict | null
  promotion_status: PromotionStatus
  supported_scopes: ScopeType[]
  result_profile: ResultProfile | null
  data_requirements: string[]
  todo_status: string | null
  docs: string[]
  latest_run: FactorLatestRun | null
  scope_capabilities: FactorScopeCapability[]
}

export interface FactorArmSpec {
  id: string
  title: string
  description: string | null
}

export interface FactorGate {
  id: string
  title: string
  description: string | null
}

export interface FactorDetail extends FactorCatalogItem {
  parameter_schema: JsonSchemaNode | null
  ui_groups: { id: string; title: string; fields: string[] }[]
  arms: FactorArmSpec[]
  strongest_baseline: string | null
  acceptance_gates: FactorGate[]
  provenance_requirements: string[]
  known_gaps: string[]
  latest_runs: FactorLatestRun[]
}

export interface FactorCatalogQuery {
  category?: string
  engineering_status?: string
  data_status?: string
  verdict?: string
  scope?: string
  query?: string
}

export function parseFactorCatalog(payload: unknown): { items: FactorCatalogItem[] } {
  const rec = asRecord(payload)
  const rows = rec ? asArray(rec.items ?? rec.factors) : asArray(payload)
  return { items: rows.map(parseFactorCatalogItem).filter((item): item is FactorCatalogItem => item !== null) }
}

export function parseFactorDetail(payload: unknown): FactorDetail {
  const rec = asRecord(payload)
  if (!rec) throw new Error('因子详情响应无效')
  const base = parseFactorCatalogItem(rec)
  if (!base) throw new Error('因子详情缺少 id')
  const schema = parseJsonSchema(
    rec.parameter_schema ?? rec.request_schema ?? rec.json_schema ?? rec.schema,
  )
  const ui = asRecord(rec.ui) ?? rec
  return {
    ...base,
    parameter_schema: schema,
    ui_groups: readUiGroups(ui),
    arms: parseArms(rec.arms),
    strongest_baseline: asString(rec.strongest_baseline ?? rec.baseline),
    acceptance_gates: parseGates(rec.acceptance_gates ?? rec.gates),
    provenance_requirements: asStringArray(rec.provenance_requirements),
    known_gaps: asStringArray(rec.known_gaps ?? rec.gaps),
    latest_runs: asArray(rec.latest_runs).map(parseLatestRun).filter((item): item is FactorLatestRun => item !== null),
  }
}

function parseFactorCatalogItem(value: unknown): FactorCatalogItem | null {
  const rec = asRecord(value)
  const id = rec ? asString(rec.id) : null
  if (!rec || !id) return null
  const supported = asStringArray(rec.supported_scopes).filter((item): item is ScopeType => item === 'symbols' || item === 'full_market')
  return {
    id,
    title: asString(rec.title) ?? id,
    category: asString(rec.category) ?? 'other',
    description: asString(rec.description) ?? '',
    engineering_status: parseEngineeringStatus(rec.engineering_status),
    latest_data_status: parseDataStatus(rec.latest_data_status ?? rec.data_status),
    latest_verdict: parseVerdict(rec.latest_verdict ?? rec.verdict),
    promotion_status: parsePromotionStatus(rec.promotion_status) ?? 'not_promoted',
    supported_scopes: supported.length ? supported : ['symbols'],
    result_profile: parseResultProfile(rec.result_profile),
    data_requirements: asStringArray(rec.data_requirements),
    todo_status: asString(rec.todo_status),
    docs: asStringArray(rec.docs),
    latest_run: parseLatestRun(
      rec.latest_run ?? (
        rec.latest_run_id
          ? {
              run_id: rec.latest_run_id,
              created_at: null,
              job_status: null,
              verdict: rec.latest_verdict,
            }
          : null
      ),
    ),
    scope_capabilities: parseScopeCapabilities(rec.scope_capabilities, supported),
  }
}

function parseLatestRun(value: unknown): FactorLatestRun | null {
  const rec = asRecord(value)
  const runId = rec ? asString(rec.run_id) : null
  if (!rec || !runId) return null
  return {
    run_id: runId,
    created_at: asString(rec.created_at),
    job_status: asString(rec.job_status),
    verdict: parseVerdict(rec.verdict),
  }
}

function parseScopeCapabilities(value: unknown, supported: ScopeType[]): FactorScopeCapability[] {
  const rows = asArray(value)
  if (rows.length > 0) {
    return rows.map((item) => {
      const rec = asRecord(item)
      if (!rec) return null
      const type = rec.type === 'full_market' || rec.type === 'symbols' ? rec.type : null
      if (!type) return null
      return {
        type,
        supported: rec.supported !== false,
        notes: asString(rec.notes),
        unavailable_capabilities: asStringArray(rec.unavailable_capabilities),
      }
    }).filter((item): item is FactorScopeCapability => item !== null)
  }
  return [
    {
      type: 'symbols',
      supported: supported.includes('symbols'),
      notes: null,
      unavailable_capabilities: [],
    },
    {
      type: 'full_market',
      supported: supported.includes('full_market'),
      notes: null,
      unavailable_capabilities: [],
    },
  ]
}

function parseArms(value: unknown): FactorArmSpec[] {
  return asArray(value).map((item, index) => {
    if (typeof item === 'string') return { id: item, title: item, description: null }
    const rec = asRecord(item)
    if (!rec) return null
    const id = asString(rec.id) ?? asString(rec.name) ?? `arm-${index + 1}`
    return { id, title: asString(rec.title ?? rec.name) ?? id, description: asString(rec.description) }
  }).filter((item): item is FactorArmSpec => item !== null)
}

function parseGates(value: unknown): FactorGate[] {
  return asArray(value).map((item, index) => {
    if (typeof item === 'string') return { id: `gate-${index + 1}`, title: item, description: null }
    const rec = asRecord(item)
    if (!rec) return null
    const id = asString(rec.id) ?? `gate-${index + 1}`
    return { id, title: asString(rec.title ?? rec.name) ?? id, description: asString(rec.description) }
  }).filter((item): item is FactorGate => item !== null)
}

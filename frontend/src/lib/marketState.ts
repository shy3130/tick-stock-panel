export type MarketState = 'concentrated' | 'transition' | 'dispersed' | 'unavailable'

export interface MarketStateSnapshot {
  available: boolean
  state: MarketState
  target_date: string
  signal_date: string | null
  methodology: {
    id: 'market_concentration_v1'
    version: 1
    smoothing_days: 5
    calibration_days: 252
    min_calibration_days: 120
    t_lag: 1
    hidden_formula_replicated: false
    formulas: Record<string, string>
  }
  metrics: Record<
    'return_std' | 'return_q90_q10' | 'turnover_hhi' | 'positive_return_hhi' | 'top3_contribution' | 'top5_contribution',
    number | null
  >
  percentiles: Record<'return_std' | 'turnover_hhi' | 'positive_return_hhi' | 'top3_contribution', number | null>
  coverage: {
    stock_count: number | null
    industry_count: number | null
    symbol_coverage: number | null
    turnover_coverage: number | null
    calibration_days: number
  }
  gates: {
    automatic_research_allowed: boolean
    reasons: string[]
  }
  reason: string | null
  warnings: string[]
  source: {
    daily: 'canonical_enriched'
    industry: 'pit_financial_snapshot'
    adjustment: 'raw_close'
    external_fallback: false
  }
}

export const marketStateQueryKey = (asOf?: string) => ['research-market-state', asOf ?? 'latest'] as const

const DATE_RE = /^\d{4}-\d{2}-\d{2}$/
const STATES = ['concentrated', 'transition', 'dispersed', 'unavailable'] as const
const METRIC_KEYS = ['return_std', 'return_q90_q10', 'turnover_hhi', 'positive_return_hhi', 'top3_contribution', 'top5_contribution'] as const
const PERCENTILE_KEYS = ['return_std', 'turnover_hhi', 'positive_return_hhi', 'top3_contribution'] as const

function record(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown>
    : null
}

function exactKeys(value: Record<string, unknown>, keys: readonly string[]): boolean {
  const actual = Object.keys(value)
  return actual.length === keys.length && actual.every(key => keys.includes(key))
}

function validDate(value: unknown): value is string {
  if (typeof value !== 'string' || !DATE_RE.test(value)) return false
  const [year, month, day] = value.split('-').map(Number)
  const date = new Date(Date.UTC(year, month - 1, day))
  return date.getUTCFullYear() === year && date.getUTCMonth() === month - 1 && date.getUTCDate() === day
}

function nullableFinite(value: unknown): value is number | null {
  return value === null || (typeof value === 'number' && Number.isFinite(value))
}

function nullableNonNegativeInteger(value: unknown): value is number | null {
  return value === null || (typeof value === 'number' && Number.isSafeInteger(value) && value >= 0)
}

function nullableRatio(value: unknown): value is number | null {
  return value === null || (typeof value === 'number' && Number.isFinite(value) && value >= 0 && value <= 1)
}

function validStringArray(value: unknown): value is string[] {
  return Array.isArray(value) && value.every(item => typeof item === 'string')
}

function validMetricRecord(value: unknown, keys: readonly string[]): value is Record<string, number | null> {
  const candidate = record(value)
  return candidate !== null
    && exactKeys(candidate, keys)
    && keys.every(key => nullableFinite(candidate[key]))
}

function validMethodology(value: unknown): boolean {
  const candidate = record(value)
  if (!candidate || !exactKeys(candidate, ['id', 'version', 'smoothing_days', 'calibration_days', 'min_calibration_days', 't_lag', 'hidden_formula_replicated', 'formulas'])) return false
  const formulas = record(candidate.formulas)
  return candidate.id === 'market_concentration_v1'
    && candidate.version === 1
    && candidate.smoothing_days === 5
    && candidate.calibration_days === 252
    && candidate.min_calibration_days === 120
    && candidate.t_lag === 1
    && candidate.hidden_formula_replicated === false
    && formulas !== null
    && Object.values(formulas).every(formula => typeof formula === 'string')
}

/** Reject malformed or AI-altered market-state payloads before they can unlock research UI. */
export function parseMarketStateSnapshot(value: unknown): MarketStateSnapshot | null {
  const snapshot = record(value)
  if (!snapshot || !exactKeys(snapshot, ['available', 'state', 'target_date', 'signal_date', 'methodology', 'metrics', 'percentiles', 'coverage', 'gates', 'reason', 'warnings', 'source'])) return null
  if (typeof snapshot.available !== 'boolean' || !STATES.includes(snapshot.state as MarketState) || !validDate(snapshot.target_date) || (snapshot.signal_date !== null && !validDate(snapshot.signal_date)) || !validMethodology(snapshot.methodology) || !validMetricRecord(snapshot.metrics, METRIC_KEYS) || !validMetricRecord(snapshot.percentiles, PERCENTILE_KEYS) || !validStringArray(snapshot.warnings)) return null
  if (snapshot.signal_date !== null && snapshot.signal_date >= snapshot.target_date) return null
  if (snapshot.available && snapshot.signal_date === null) return null

  const coverage = record(snapshot.coverage)
  const gates = record(snapshot.gates)
  const source = record(snapshot.source)
  if (!coverage || !exactKeys(coverage, ['stock_count', 'industry_count', 'symbol_coverage', 'turnover_coverage', 'calibration_days']) || !nullableNonNegativeInteger(coverage.stock_count) || !nullableNonNegativeInteger(coverage.industry_count) || !nullableRatio(coverage.symbol_coverage) || !nullableRatio(coverage.turnover_coverage) || typeof coverage.calibration_days !== 'number' || !Number.isSafeInteger(coverage.calibration_days) || coverage.calibration_days < 0) return null
  if (!gates || !exactKeys(gates, ['automatic_research_allowed', 'reasons']) || typeof gates.automatic_research_allowed !== 'boolean' || !validStringArray(gates.reasons)) return null
  if (!source || !exactKeys(source, ['daily', 'industry', 'adjustment', 'external_fallback']) || source.daily !== 'canonical_enriched' || source.industry !== 'pit_financial_snapshot' || source.adjustment !== 'raw_close' || source.external_fallback !== false) return null
  if (snapshot.reason !== null && typeof snapshot.reason !== 'string') return null
  if ((!snapshot.available && snapshot.state !== 'unavailable') || (snapshot.available && snapshot.state === 'unavailable')) return null
  if (gates.automatic_research_allowed !== (snapshot.available && snapshot.state === 'dispersed')) return null
  if (snapshot.available) {
    if (Object.values(snapshot.metrics as Record<string, number | null>).some(value => value === null)) return null
    if (Object.values(snapshot.percentiles as Record<string, number | null>).some(value => value === null || value < 0 || value > 1)) return null
    if (
      coverage.stock_count === null
      || coverage.stock_count < 1_000
      || coverage.industry_count === null
      || coverage.industry_count < 20
      || coverage.symbol_coverage === null
      || coverage.symbol_coverage < 0.9
      || coverage.turnover_coverage === null
      || coverage.turnover_coverage < 0.95
      || coverage.calibration_days < 120
    ) return null
  }

  return snapshot as unknown as MarketStateSnapshot
}

/** Explicit read-only client; the endpoint is never contacted until its consumer enables the query. */
export async function fetchMarketState(asOf?: string): Promise<MarketStateSnapshot> {
  const query = asOf ? `?as_of=${encodeURIComponent(asOf)}` : ''
  const response = await fetch(`/api/research/t-suitability/market-state${query}`, {
    headers: { Accept: 'application/json' },
  })
  const body: unknown = await response.json().catch(() => null)
  if (!response.ok) {
    const detail = record(body)
    const message = typeof detail?.detail === 'string' ? detail.detail : `${response.status} ${response.statusText}`
    throw new Error(message)
  }
  const snapshot = parseMarketStateSnapshot(body)
  if (!snapshot) throw new Error('市场状态响应不符合固定研究契约，已停止展示研究动作。')
  return snapshot
}

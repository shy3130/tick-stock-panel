import { parseMarketStateSnapshot } from './marketState.ts'

function assert(condition: unknown, message: string): asserts condition {
  if (!condition) throw new Error(message)
}

function makeSnapshot(): Record<string, unknown> {
  return {
    available: true,
    state: 'dispersed',
    target_date: '2026-08-26',
    signal_date: '2026-08-25',
    methodology: {
      id: 'market_concentration_v1',
      version: 1,
      smoothing_days: 5,
      calibration_days: 252,
      min_calibration_days: 120,
      t_lag: 1,
      hidden_formula_replicated: false,
      formulas: { return_std: 'std(raw_close returns)' },
    },
    metrics: {
      return_std: 0.03,
      return_q90_q10: 0.08,
      turnover_hhi: 0.2,
      positive_return_hhi: 0.3,
      top3_contribution: 0.4,
      top5_contribution: 0.5,
    },
    percentiles: {
      return_std: 0.8,
      turnover_hhi: 0.2,
      positive_return_hhi: 0.3,
      top3_contribution: 0.4,
    },
    coverage: {
      stock_count: 5000,
      industry_count: 31,
      symbol_coverage: 0.99,
      amount_symbol_coverage: 0.99,
      turnover_coverage: 0.98,
      calibration_days: 180,
    },
    gates: { automatic_research_allowed: true, reasons: [] },
    reason: null,
    warnings: [],
    source: {
      daily: 'canonical_enriched',
      industry: 'pit_financial_snapshot',
      adjustment: 'raw_close',
      external_fallback: false,
    },
  }
}

const valid = parseMarketStateSnapshot(makeSnapshot())
assert(valid?.state === 'dispersed' && valid.gates.automatic_research_allowed, '合法分散状态应保留固定研究门槛')

const unknownMetric = makeSnapshot()
;(unknownMetric.metrics as Record<string, unknown>).model_signal = '买入'
assert(parseMarketStateSnapshot(unknownMetric) === null, '市场指标不得接受 AI 自由字段')

const invalidGate = makeSnapshot()
;(invalidGate.gates as Record<string, unknown>).automatic_research_allowed = false
assert(parseMarketStateSnapshot(invalidGate) === null, '分散状态的研究门槛不得被篡改')

const unavailable = makeSnapshot()
unavailable.available = false
unavailable.state = 'unavailable'
;(unavailable.gates as Record<string, unknown>).automatic_research_allowed = false
;(unavailable.metrics as Record<string, unknown>).return_std = null
assert(parseMarketStateSnapshot(unavailable)?.state === 'unavailable', '不可用状态应显式保留，不得伪装为零值')

const badSource = makeSnapshot()
;(badSource.source as Record<string, unknown>).external_fallback = true
assert(parseMarketStateSnapshot(badSource) === null, '外部 fallback 不得进入市场状态 UI')

const missingAvailableMetric = makeSnapshot()
;(missingAvailableMetric.metrics as Record<string, unknown>).return_std = null
assert(parseMarketStateSnapshot(missingAvailableMetric) === null, '可用状态必须包含完整可复算指标')

const impossibleCoverage = makeSnapshot()
;(impossibleCoverage.coverage as Record<string, unknown>).turnover_coverage = 1.01

const impossibleAmountCoverage = makeSnapshot()
;(impossibleAmountCoverage.coverage as Record<string, unknown>).amount_symbol_coverage = 1.01
assert(parseMarketStateSnapshot(impossibleAmountCoverage) === null, '成交额标的覆盖率必须在 0 到 1 之间')
assert(parseMarketStateSnapshot(impossibleCoverage) === null, '覆盖率必须在 0 到 1 之间')

for (const signalDate of [null, '2026-08-26', '2026-08-27']) {
  const invalidT1 = makeSnapshot()
  invalidT1.signal_date = signalDate
  assert(parseMarketStateSnapshot(invalidT1) === null, `可用状态必须严格 T-1：${signalDate}`)
}

for (const [field, value] of [
  ['stock_count', 999],
  ['industry_count', 19],
  ['symbol_coverage', 0.89],
  ['amount_symbol_coverage', 0.89],
  ['turnover_coverage', 0.94],
  ['calibration_days', 119],
] as const) {
  const insufficient = makeSnapshot()
  ;(insufficient.coverage as Record<string, unknown>)[field] = value
  assert(parseMarketStateSnapshot(insufficient) === null, `覆盖门槛不足必须 fail-closed：${field}`)
}

console.log('marketState.test.ts ok')

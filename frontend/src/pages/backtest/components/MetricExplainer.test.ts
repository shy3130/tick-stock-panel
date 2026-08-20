import { METRIC_TERM_LIST, METRIC_TERMS, MetricExplainer } from './MetricExplainer.tsx'

function assert(condition: unknown, message: string): asserts condition {
  if (!condition) throw new Error(message)
}

assert(METRIC_TERM_LIST.length >= 18, `dictionary has at least 18 terms (got ${METRIC_TERM_LIST.length})`)

const terms = METRIC_TERM_LIST.map(item => item.term)
assert(new Set(terms).size === terms.length, 'no duplicate term keys')

const REQUIRED_FIELDS = ['name', 'definition', 'direction', 'caveat'] as const
for (const item of METRIC_TERM_LIST) {
  for (const field of REQUIRED_FIELDS) {
    assert(typeof item[field] === 'string' && item[field].trim().length > 0, `${item.term}.${field} is non-empty string`)
  }
}

for (const term of ['sharpe', 'sortino', 'max_drawdown', 'profit_factor', 'payoff_ratio', 'calmar', 'information_ratio', 'tracking_error', 'alpha', 'beta', 'var', 'cvar', 'expectancy', 'mae', 'mfe', 'dsr', 'pbo', 'capacity_utilization']) {
  assert(METRIC_TERMS[term] != null, `core term ${term} present`)
}

assert(METRIC_TERMS.sharpe === METRIC_TERM_LIST.find(item => item.term === 'sharpe'), 'map lookup consistent with list')
assert(METRIC_TERMS.unknown_term_xyz === undefined, 'unknown term missing')

assert(typeof MetricExplainer === 'function', 'component is importable function')

console.log('MetricExplainer.test.ts ok')

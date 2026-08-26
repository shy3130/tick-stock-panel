const DATE_RE = /^\d{4}-\d{2}-\d{2}$/
const POOL_ID_RE = /^[a-f0-9]{16}$/
const SYMBOL_RE = /^\d{6}\.(?:SH|SZ|BJ)$/
const MAX_CANDIDATES = 12


export const SHORT_POOL_PRESET = {
  preset_id: 'short_momentum_quality_v1',
  version: 1,
  name: '短线动量质量观察',
  description: '以流动性、趋势位置、温和动量、波动与涨停风险约束形成的固定研究观察池',
} as const

const EVIDENCE_RULES = {
  exclude_st: { label: '排除 ST/退市', op: '=', target: true, criterion: '是', unit: '' },
  listing_days: { label: '上市天数', op: '>=', target: 120, criterion: '≥ 120 天', unit: '天' },
  amount: { label: '成交额', op: '>=', target: 300_000_000, criterion: '≥ 3 亿元', unit: '元' },
  turnover_rate: { label: '换手率', op: 'between', target: [2, 18], criterion: '2%–18%', unit: '%' },
  above_ma20: { label: '站上 MA20', op: '=', target: true, criterion: '是', unit: '' },
  momentum_20d: { label: '20 日动量', op: 'between', target: [0.03, 0.25], criterion: '3%–25%', unit: '小数(0.05=5%)' },
  distance_to_60d_high: { label: '距 60 日高点', op: 'between', target: [-15, 0], criterion: '-15%–0%', unit: '%' },
  atr_pct_14: { label: 'ATR(14)', op: 'between', target: [2, 9], criterion: '2%–9%', unit: '%' },
  vol_ratio_5d: { label: '5 日量比', op: '>=', target: 1, criterion: '≥ 1', unit: '倍' },
  change_pct: { label: '当日涨跌幅', op: 'between', target: [-0.03, 0.08], criterion: '-3%–8%', unit: '小数' },
  limit_up: { label: '涨停', op: '=', target: false, criterion: '否', unit: '' },
  broken_limit_up: { label: '炸板', op: '=', target: false, criterion: '否', unit: '' },
} as const

type EvidenceField = keyof typeof EVIDENCE_RULES

export interface ShortPoolEvidence {
  field: EvidenceField
  label: string
  actual: number | boolean
  display: string
  op: string
  target: number | boolean | readonly number[]
  criterion: string
  unit: string
}

export interface ShortPoolCandidate {
  rank: number
  symbol: string
  name: string
  evidence: ShortPoolEvidence[]
}

export interface ShortPoolCard {
  pool_id: string
  as_of: string
  count: number
  total: number
  preset: typeof SHORT_POOL_PRESET
  candidates: ShortPoolCandidate[]
}
function record(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown>
    : null
}

function finiteInteger(value: unknown, min: number): number | null {
  return typeof value === 'number' && Number.isSafeInteger(value) && value >= min ? value : null
}

function exactKeys(value: Record<string, unknown>, keys: readonly string[]): boolean {
  const actual = Object.keys(value)
  return actual.length === keys.length && actual.every(key => keys.includes(key))
}

function sameTarget(actual: unknown, expected: number | boolean | readonly number[]): boolean {
  if (Array.isArray(expected)) {
    return Array.isArray(actual)
      && actual.length === expected.length
      && actual.every((item, index) => item === expected[index])
  }
  return actual === expected
}

function validDate(value: string): boolean {
  if (!DATE_RE.test(value)) return false
  const [year, month, day] = value.split('-').map(Number)
  const date = new Date(Date.UTC(year, month - 1, day))
  return date.getUTCFullYear() === year
    && date.getUTCMonth() === month - 1
    && date.getUTCDate() === day
}

function actualSatisfies(
  actual: number | boolean,
  op: string,
  target: number | boolean | readonly number[],
): boolean {
  if (op === '=') return actual === target
  if (op === '>=' && typeof actual === 'number' && typeof target === 'number') return actual >= target
  if (
    op === 'between'
    && typeof actual === 'number'
    && Array.isArray(target)
    && target.length === 2
  ) return actual >= target[0] && actual <= target[1]
  return false
}

function displayActual(field: EvidenceField, value: number | boolean): string {
  if (typeof value === 'boolean') return value ? '是' : '否'
  if (field === 'momentum_20d' || field === 'change_pct') return `${(value * 100).toFixed(2)}%`
  if (field === 'turnover_rate' || field === 'distance_to_60d_high' || field === 'atr_pct_14') return `${value.toFixed(2)}%`
  if (field === 'amount') return `${(value / 100_000_000).toFixed(2)}亿元`
  if (field === 'vol_ratio_5d') return `${value.toFixed(2)}倍`
  if (field === 'listing_days') return `${value.toFixed(0)}天`
  return String(value)
}

function extractEvidence(value: unknown, expectedField: EvidenceField): ShortPoolEvidence | null {
  const item = record(value)
  if (!item || !exactKeys(item, ['field', 'label', 'actual', 'display', 'op', 'target', 'criterion', 'unit'])) return null
  const rule = EVIDENCE_RULES[expectedField]
  const booleanField = expectedField === 'exclude_st' || expectedField === 'above_ma20' || expectedField === 'limit_up' || expectedField === 'broken_limit_up'
  const suppliedActual = item.actual
  let actual: number | boolean
  if (booleanField) {
    if (typeof suppliedActual !== 'boolean') return null
    actual = suppliedActual
  } else {
    if (typeof suppliedActual !== 'number' || !Number.isFinite(suppliedActual)) return null
    actual = suppliedActual
  }
  if (
    item.field !== expectedField
    || typeof item.label !== 'string'
    || typeof item.display !== 'string'
    || typeof item.criterion !== 'string'
    || typeof item.unit !== 'string'
    || item.op !== rule.op
    || !sameTarget(item.target, rule.target)
    || !actualSatisfies(actual, rule.op, rule.target)
  ) return null

  return {
    field: expectedField,
    label: rule.label,
    actual,
    display: displayActual(expectedField, actual),
    op: rule.op,
    target: rule.target,
    criterion: rule.criterion,
    unit: rule.unit,
  }
}

function extractCandidate(value: unknown, expectedRank: number): ShortPoolCandidate | null {
  const item = record(value)
  if (!item || !exactKeys(item, ['rank', 'symbol', 'name', 'evidence']) || item.rank !== expectedRank || typeof item.symbol !== 'string' || typeof item.name !== 'string' || !SYMBOL_RE.test(item.symbol) || !item.name.trim() || !Array.isArray(item.evidence) || item.evidence.length !== Object.keys(EVIDENCE_RULES).length) return null

  const evidence: ShortPoolEvidence[] = []
  for (const [index, field] of Object.keys(EVIDENCE_RULES).entries()) {
    const entry = extractEvidence(item.evidence[index], field as EvidenceField)
    if (!entry) return null
    evidence.push(entry)
  }
  return { rank: expectedRank, symbol: item.symbol, name: item.name.trim(), evidence }
}

function validSelectionBasis(value: unknown): boolean {
  const basis = record(value)
  if (!basis || !exactKeys(basis, ['conditions', 'order_by', 'limit', 'deterministic']) || !Array.isArray(basis.conditions) || basis.conditions.length !== Object.keys(EVIDENCE_RULES).length || basis.limit === null || finiteInteger(basis.limit, 5) === null || (basis.limit as number) > MAX_CANDIDATES || basis.deterministic !== true) return false
  const orderBy = record(basis.order_by)
  if (!orderBy || !exactKeys(orderBy, ['field', 'direction']) || orderBy.field !== 'momentum_20d' || orderBy.direction !== 'desc') return false

  return basis.conditions.every((condition, index) => {
    const item = record(condition)
    const field = Object.keys(EVIDENCE_RULES)[index] as EvidenceField | undefined
    return item !== null
      && field !== undefined
      && exactKeys(item, ['field', 'op', 'value'])
      && item.field === field
      && item.op === EVIDENCE_RULES[field].op
      && sameTarget(item.value, EVIDENCE_RULES[field].target)
  })
}

function validArtifact(value: unknown, poolId: string, asOf: string, count: number): boolean {
  if (!Array.isArray(value) || value.length !== 1) return false
  const artifact = record(value[0])
  return artifact !== null
    && exactKeys(artifact, ['kind', 'pool_id', 'as_of', 'count', 'location'])
    && artifact.kind === 'short_pool'
    && artifact.pool_id === poolId
    && artifact.as_of === asOf
    && artifact.count === count
    && artifact.location === `user_data/short_pools/${poolId}.json`
}


/**
 * 从固定短线观察池结果中提取可呈现数据。所有边界字段与固定 preset 必须齐全；
 * 候选和证据逐项清洗，未知字段、模型自由代码或证据均不会进入 UI。
 */
export function extractShortPoolCard(result: unknown): ShortPoolCard | null {
  const payload = record(result)
  if (!payload || !exactKeys(payload, ['status', 'summary', 'pool_id', 'as_of', 'count', 'total', 'preset', 'candidates', 'disclaimer', 'selection_basis', 'ai_role', 'next_actions', 'artifacts']) || payload.status !== 'success') return null

  const poolId = typeof payload.pool_id === 'string' ? payload.pool_id : ''
  const asOf = typeof payload.as_of === 'string' ? payload.as_of : ''
  const reportedCount = finiteInteger(payload.count, 0)
  const total = finiteInteger(payload.total, 0)
  const preset = record(payload.preset)
  const expectedActions = reportedCount === 0
    ? []
    : ['view_stock_detail', 'add_to_watchlist', 'stage_strategy_backtest']
  const hasBoundary = typeof payload.summary === 'string'
    && payload.summary.length > 0
    && payload.disclaimer === '研究观察池，非投资建议'
    && payload.ai_role === 'AI 只解释证据；不得生成、删除或重排候选；不提供买卖方向、价格或仓位建议'
    && Array.isArray(payload.next_actions)
    && payload.next_actions.length === expectedActions.length
    && payload.next_actions.every((action, index) => action === expectedActions[index])

  if (!POOL_ID_RE.test(poolId) || !validDate(asOf) || reportedCount === null || reportedCount > MAX_CANDIDATES || total === null || total < reportedCount || !preset || !exactKeys(preset, ['preset_id', 'version', 'name', 'description']) || preset.preset_id !== SHORT_POOL_PRESET.preset_id || preset.version !== SHORT_POOL_PRESET.version || preset.name !== SHORT_POOL_PRESET.name || preset.description !== SHORT_POOL_PRESET.description || !hasBoundary || !validSelectionBasis(payload.selection_basis) || !Array.isArray(payload.candidates) || payload.candidates.length !== reportedCount || !validArtifact(payload.artifacts, poolId, asOf, reportedCount)) return null

  const candidates: ShortPoolCandidate[] = []
  for (const [index, candidateValue] of payload.candidates.entries()) {
    const candidate = extractCandidate(candidateValue, index + 1)
    if (!candidate) return null
    candidates.push(candidate)
  }

  return {
    pool_id: poolId,
    as_of: asOf,
    count: candidates.length,
    total,
    preset: SHORT_POOL_PRESET,
    candidates,
  }
}

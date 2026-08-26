import { extractShortPoolCard } from './shortPoolCard.ts'

function assert(condition: unknown, message: string): asserts condition {
  if (!condition) throw new Error(message)
}

const fields = [
  ['exclude_st', true, '=', true],
  ['listing_days', 300, '>=', 120],
  ['amount', 400_000_000, '>=', 300_000_000],
  ['turnover_rate', 6, 'between', [2, 18]],
  ['above_ma20', true, '=', true],
  ['momentum_20d', 0.12, 'between', [0.03, 0.25]],
  ['distance_to_60d_high', -4, 'between', [-15, 0]],
  ['atr_pct_14', 4, 'between', [2, 9]],
  ['vol_ratio_5d', 1.2, '>=', 1],
  ['change_pct', 0.03, 'between', [-0.03, 0.08]],
  ['limit_up', false, '=', false],
  ['broken_limit_up', false, '=', false],
] as const

function makeCandidate(rank: number): Record<string, unknown> {
  return {
    rank,
    symbol: `${String(600000 + rank).padStart(6, '0')}.SH`,
    name: `样本${rank}`,
    evidence: fields.map(([field, actual, op, target]) => ({
      field,
      label: field,
      actual,
      display: String(actual),
      op,
      target,
      criterion: field,
      unit: '',
    })),
  }
}


function makePayload(count = 1): Record<string, unknown> {
  const poolId = '0123456789abcdef'
  return {
    status: 'success',
    summary: '短线动量质量观察池(确定性筛选)',
    pool_id: poolId,
    as_of: '2026-08-26',
    count,
    total: count,
    preset: {
      preset_id: 'short_momentum_quality_v1',
      version: 1,
      name: '短线动量质量观察',
      description: '以流动性、趋势位置、温和动量、波动与涨停风险约束形成的固定研究观察池',
    },
    candidates: Array.from({ length: count }, (_, index) => makeCandidate(index + 1)),
    disclaimer: '研究观察池，非投资建议',
    selection_basis: {
      conditions: fields.map(([field, _actual, op, value]) => ({ field, op, value })),
      order_by: { field: 'momentum_20d', direction: 'desc' },
      limit: 8,
      deterministic: true,
    },
    ai_role: 'AI 只解释证据；不得生成、删除或重排候选；不提供买卖方向、价格或仓位建议',
    next_actions: count === 0
      ? []
      : ['view_stock_detail', 'add_to_watchlist', 'stage_strategy_backtest'],
    artifacts: [{ kind: 'short_pool', pool_id: poolId, as_of: '2026-08-26', count, location: `user_data/short_pools/${poolId}.json` }],
  }
}

const valid = extractShortPoolCard(makePayload())
assert(valid !== null, '合法固定短线池应被解析')
assert(valid.candidates[0].symbol === '600001.SH', '候选代码应保留工具结果')
assert(valid.candidates[0].evidence.length === 12, '应保留全部固定条件证据')

const forgedDisplay = makePayload()
;((forgedDisplay.candidates as Record<string, unknown>[])[0].evidence as Record<string, unknown>[])[5].display = '明日满仓买入'
const sanitizedDisplay = extractShortPoolCard(forgedDisplay)
assert(sanitizedDisplay !== null, '合法 actual 不应因非权威展示文本丢失')
assert(sanitizedDisplay.candidates[0].evidence[5].display === '12.00%', '展示值必须由已验证 actual 在前端重算')

const empty = extractShortPoolCard(makePayload(0))
assert(empty !== null && empty.count === 0 && empty.candidates.length === 0, '空池是合法结果')

const badPool = makePayload()
badPool.pool_id = 'pool-from-model'
assert(extractShortPoolCard(badPool) === null, '非法 pool_id 应 fail-closed')

const badSymbol = makePayload()
;(badSymbol.candidates as Record<string, unknown>[])[0].symbol = '模型建议买入'
assert(extractShortPoolCard(badSymbol) === null, '非法候选代码不得进入 UI')

const oversized = makePayload(13)
assert(extractShortPoolCard(oversized) === null, '候选超过 12 只应 fail-closed')

const badEvidence = makePayload()
;((badEvidence.candidates as Record<string, unknown>[])[0].evidence as Record<string, unknown>[])[0].actual = '模型解释'
assert(extractShortPoolCard(badEvidence) === null, '非法证据不得进入 UI')

const failedCriterion = makePayload()
;((failedCriterion.candidates as Record<string, unknown>[])[0].evidence as Record<string, unknown>[])[1].actual = 30
assert(extractShortPoolCard(failedCriterion) === null, '未满足固定条件的证据应 fail-closed')

const badDate = makePayload()
badDate.as_of = '2026-02-31'
assert(extractShortPoolCard(badDate) === null, '无效自然日应 fail-closed')

const missingBoundary = makePayload()
delete missingBoundary.ai_role
assert(extractShortPoolCard(missingBoundary) === null, '缺少 AI 边界字段应 fail-closed')


console.log('shortPoolCard.test.ts ok')

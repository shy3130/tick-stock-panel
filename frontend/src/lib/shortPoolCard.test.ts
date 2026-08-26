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

function makeMarketState(): Record<string, unknown> {
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
    percentiles: { return_std: 0.8, turnover_hhi: 0.2, positive_return_hhi: 0.3, top3_contribution: 0.4 },
    coverage: { stock_count: 5000, industry_count: 31, symbol_coverage: 0.99, turnover_coverage: 0.98, calibration_days: 180 },
    gates: { automatic_research_allowed: true, reasons: [] },
    reason: null,
    warnings: [],
    source: { daily: 'canonical_enriched', industry: 'pit_financial_snapshot', adjustment: 'raw_close', external_fallback: false },
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
    market_state: makeMarketState(),
    t_research: {
      protocol_id: 'bollinger_volatility_t_research_v1',
      bar_precision: '5m',
      lookback_sessions: 120,
      min_events: 30,
      signal_lag: 'T-1',
      validation: 'strict_walk_forward',
      baseline: 'all_eligible_days',
      filtered: 'market_state=dispersed',
      round_trip_cost_bps: 20,
      cost_sensitivity_bps: [10, 20, 30],
      automatic_run: false,
      status: count > 0 ? 'ready_for_confirmation' : 'blocked_by_market_state',
    },
  }
}

const valid = extractShortPoolCard(makePayload())
assert(valid !== null, '合法固定短线池应被解析')
assert(valid.candidates[0].symbol === '600001.SH', '候选代码应保留工具结果')
assert(valid.candidates[0].evidence.length === 12, '应保留全部固定条件证据')
assert(valid.market_state?.state === 'dispersed' && valid.t_research?.status === 'ready_for_confirmation', '完整且固定的双轴结果才可开启研究确认')
assert(valid.limit === 8, '确认请求必须保留服务端重算所需的固定 limit')

const basePayload = makePayload()
delete basePayload.market_state
delete basePayload.t_research
const baseCard = extractShortPoolCard(basePayload)
assert(baseCard !== null, '基础短线池后端封套必须保持可解析')
assert(baseCard.market_state === null && baseCard.t_research === null, '未提供可选扩展时不得伪造市场状态或做 T 研究')

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

const alteredProtocol = makePayload()
;(alteredProtocol.t_research as Record<string, unknown>).automatic_run = true
const blockedProtocol = extractShortPoolCard(alteredProtocol)
assert(blockedProtocol !== null && blockedProtocol.t_research === null, 'AI 篡改协议不得渲染研究动作，同时保留既有观察池操作')

const alteredMarketState = makePayload()
;(alteredMarketState.market_state as Record<string, unknown>).source = {
  daily: 'canonical_enriched',
  industry: 'untrusted_model_map',
  adjustment: 'raw_close',
  external_fallback: false,
}
const blockedMarketState = extractShortPoolCard(alteredMarketState)
assert(blockedMarketState !== null && blockedMarketState.market_state === null && blockedMarketState.t_research === null, '无效市场轴不得放行研究动作，同时保留既有观察池操作')

console.log('shortPoolCard.test.ts ok')

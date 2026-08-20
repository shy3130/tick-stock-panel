import {
  MIN_SAMPLE_TRADING_DAYS,
  buildPreflightFindings,
  estimateTradingDays,
} from './runPreflight.ts'

function assert(condition: unknown, message: string): asserts condition {
  if (!condition) throw new Error(message)
}

// estimateTradingDays: 2026-01-05 是周一
assert(estimateTradingDays('2026-01-05', '2026-01-09') === 5, 'mon-fri week counts 5')
assert(estimateTradingDays('2026-01-05', '2026-01-11') === 5, 'week incl weekend still 5')
assert(estimateTradingDays('2026-01-05', '2026-01-05') === 1, 'single day counts 1')
assert(estimateTradingDays('2026-01-01', '2025-01-01') === null, 'reversed range null')
assert(estimateTradingDays('', '2026-01-01') === null, 'unparsable start null')

// 样本过短：短区间 + 限定池 + 正常成本 + open_t+1 → 仅 sample_short
const short = buildPreflightFindings({
  start: '2026-01-05',
  end: '2026-03-31',
  symbols: ['600000.SH'],
  fees: 2,
  slippage: 5,
  entryFill: 'open_t+1',
})
assert(short.length === 1 && short[0].key === 'sample_short' && short[0].level === 'warn', 'short range only warns sample_short')
assert(String(short[0].message).includes(String(MIN_SAMPLE_TRADING_DAYS)), 'sample_short message mentions threshold')

// 长区间 + 空池 + 零成本：幸存者偏差 / 零成本 / 次新股 三条 warn
const risky = buildPreflightFindings({
  start: '2018-01-01',
  end: '2026-08-01',
  symbols: [],
  fees: 0,
  slippage: 0,
  entryFill: 'open_t+1',
})
const riskyKeys = risky.map(finding => finding.key)
for (const key of ['survivorship_bias', 'zero_cost', 'young_listings']) {
  assert(riskyKeys.includes(key), `risky config includes ${key}`)
}
assert(!riskyKeys.includes('sample_short'), 'long range no sample_short')
assert(risky.every(finding => finding.level === 'warn'), 'risky findings all warn')

// close_t 建仓口径 → info 提示
const closeT = buildPreflightFindings({
  start: '2018-01-01',
  end: '2026-08-01',
  symbols: ['600000.SH'],
  fees: 2,
  slippage: 5,
  entryFill: 'close_t',
})
assert(closeT.length === 1 && closeT[0].key === 'close_t_entry' && closeT[0].level === 'info', 'close_t entry info only')

// 干净配置 → 无任何提示
const clean = buildPreflightFindings({
  start: '2018-01-01',
  end: '2026-08-01',
  symbols: ['600000.SH'],
  fees: 2,
  slippage: 5,
  entryFill: 'open_t+1',
})
assert(clean.length === 0, 'clean config no findings')

// 门控开启时空池不再触发次新股提醒
const gated = buildPreflightFindings({
  start: '2018-01-01',
  end: '2026-08-01',
  symbols: [],
  fees: 2,
  slippage: 5,
  entryFill: 'open_t+1',
  minListedDays: 120,
})
assert(!gated.some(finding => finding.key === 'young_listings'), 'gated config no young_listings')

console.log('runPreflight.test.ts ok')

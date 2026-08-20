import {
  DEFAULT_FEES_PCT,
  DEFAULT_SLIPPAGE_BPS,
  DEFAULT_STAMP_TAX_PCT,
  MIN_SAMPLE_TRADING_DAYS,
  applyPreflightPatch,
  buildPreflightFindings,
  estimateTradingDays,
  suggestSampleStart,
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

// ---- fix 生成 ----

// sample_short：fix 拉长起始日，且建议值自身满足最少样本（fail-closed 校验）
const shortFix = short[0].fix
assert(shortFix != null, 'sample_short has fix')
assert(typeof shortFix.patch.start === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(shortFix.patch.start as string), 'fix start is YYYY-MM-DD')

// 建议起始日必须让新区间不再触发 sample_short：应用 patch 后重跑 findings 应消失
let appliedStart = ''
const applyResult = applyPreflightPatch(shortFix.patch, {
  start: (value: unknown) => { appliedStart = String(value) },
})
assert(appliedStart < '2026-03-31', 'fix start extends range earlier')
assert(applyResult.includes('start'), 'applyPreflightPatch returns applied keys')
const afterFix = buildPreflightFindings({
  start: appliedStart,
  end: '2026-03-31',
  symbols: ['600000.SH'],
  fees: 2,
  slippage: 5,
  entryFill: 'open_t+1',
})
assert(!afterFix.some(f => f.key === 'sample_short'), 'sample_short resolved after applying fix')

// suggestSampleStart：坏结束日 → null；正常结束日 → 区间满足最少样本
assert(suggestSampleStart('not-a-date', 120) === null, 'unparsable end null')
assert((estimateTradingDays(suggestSampleStart('2026-08-01', 120)!, '2026-08-01') ?? 0) >= MIN_SAMPLE_TRADING_DAYS, 'suggested range meets minimum sample')

// zero_cost：fix 只填缺失项，已设置的不覆盖；stampTax 未提供时不产出 stamp 键
const riskyFix = risky.find(f => f.key === 'zero_cost')!.fix!
assert(riskyFix != null, 'zero_cost has fix')
assert(riskyFix.patch.fees_pct === DEFAULT_FEES_PCT, 'zero_cost fix fills default fees')
assert(riskyFix.patch.slippage_bps === DEFAULT_SLIPPAGE_BPS, 'zero_cost fix fills default slippage')
assert(!('stamp_tax_pct' in riskyFix.patch), 'no stamp key when stampTax not supplied')

const feesOnly = buildPreflightFindings({
  start: '2018-01-01',
  end: '2026-08-01',
  symbols: ['600000.SH'],
  fees: 0,
  slippage: 5,
  entryFill: 'open_t+1',
})
const feesOnlyPatch = feesOnly.find(f => f.key === 'zero_cost')!.fix!.patch
assert('fees_pct' in feesOnlyPatch && !('slippage_bps' in feesOnlyPatch), 'fix only fills missing cost fields')

// 印花税为 0 时（字段已提供）参与零成本判定并给 stamp_tax_pct 键
const stampZero = buildPreflightFindings({
  start: '2018-01-01',
  end: '2026-08-01',
  symbols: ['600000.SH'],
  fees: 2,
  slippage: 5,
  entryFill: 'open_t+1',
  stampTax: 0,
})
const stampZeroFix = stampZero.find(f => f.key === 'zero_cost')!
assert(stampZeroFix != null, 'stamp tax zero triggers zero_cost')
assert(stampZeroFix.fix!.patch.stamp_tax_pct === DEFAULT_STAMP_TAX_PCT, 'stamp fix uses default stamp tax')
assert(!('fees_pct' in stampZeroFix.fix!.patch) && !('slippage_bps' in stampZeroFix.fix!.patch), 'set fields not overwritten')

// 方法论类 finding 不给 fix（不能安全自动修复）
for (const key of ['survivorship_bias', 'young_listings']) {
  assert(risky.find(f => f.key === key)?.fix == null, `${key} has no fix`)
}
assert(closeT[0].fix == null, 'close_t_entry has no fix')

// ---- applyPreflightPatch ----

// 未登记的键被跳过、不抛错；登记的键按原值传递
const seen: Record<string, unknown> = {}
const consumed = applyPreflightPatch(
  { start: '2020-01-01', fees_pct: 0.0002, unknown_field: 1 },
  {
    start: (v: unknown) => { seen.start = v },
    fees_pct: (v: unknown) => { seen.fees_pct = v },
  },
)
assert(consumed.length === 2 && consumed[0] === 'start' && consumed[1] === 'fees_pct', 'only registered keys consumed')
assert(!('unknown_field' in seen), 'unregistered key skipped silently')
assert(seen.start === '2020-01-01' && seen.fees_pct === 0.0002, 'values passed through unchanged')

// 空 patch / 空 appliers：返回空数组
assert(applyPreflightPatch({}, {}).length === 0, 'empty patch applies nothing')
assert(applyPreflightPatch({ start: '2020-01-01' }, {}).length === 0, 'no appliers applies nothing')

console.log('runPreflight.test.ts ok')

import { elapsedSince, estimateEtaMs, formatClock, formatRate } from './runStatus.ts'

function assert(condition: unknown, message: string): asserts condition {
  if (!condition) throw new Error(message)
}

assert(formatClock(480) === '480ms', 'subsecond')
assert(formatClock(12_000) === '12 秒', 'seconds')
assert(formatClock(90_000) === '1 分 30 秒', 'minutes')
assert(formatClock(3_600_000) === '1 小时', 'hours')
assert(estimateEtaMs(10_000, 2, 10) === 40_000, 'eta scales remaining work')
assert(estimateEtaMs(10_000, 0, 10) === null, 'no eta before first completion')
assert(formatRate(12, 60_000) === '12 /分', 'per minute')
assert(formatRate(1, 120_000) === '30.0 /时', 'slow rate')
assert(elapsedSince('2026-08-19T00:00:00Z', Date.parse('2026-08-19T00:01:00Z')) === 60_000, 'elapsed')

console.log('runStatus.test.ts ok')

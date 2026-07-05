import {
  buildAllSignals,
  buildClusterSignals,
  buildThreeLockSignals,
  computeThreeLocks,
  sortKLinesByDateAsc,
  type ThreeLocksKLinePoint,
} from './threeLocks.ts'

function assert(condition: unknown, message: string): asserts condition {
  if (!condition) {
    throw new Error(message)
  }
}

function makeRows(count: number): ThreeLocksKLinePoint[] {
  return Array.from({ length: count }, (_, index) => {
    const close = 100 + Math.pow(index / count, 1.6) * 30
    const recent = index >= count - 5
    return {
      date: `2026-01-${String(index + 1).padStart(3, '0')}`,
      high: close + (recent ? 3 : 1),
      low: close - 1,
      close,
      volume: recent ? 200 : 100,
      main_net_inflow: recent ? (index % 5 < 3 ? 10 : -2) : 1,
    }
  })
}

function withOverrides(
  rows: ThreeLocksKLinePoint[],
  fn: (row: ThreeLocksKLinePoint, index: number) => ThreeLocksKLinePoint
): ThreeLocksKLinePoint[] {
  return rows.map((row, index) => fn({ ...row }, index))
}

function testDescendingInputSortsBeforeCompute() {
  const rows = makeRows(130)
  const descending = rows.slice().reverse()
  assert(sortKLinesByDateAsc(descending)[0].date === rows[0].date, 'sort should restore ascending date order')
  assert(computeThreeLocks(descending).date === rows[rows.length - 1].date, 'latest date should come from ascending tail')
}

function testTrendNeeds60ValidCloses() {
  const result = computeThreeLocks(makeRows(59))
  assert(result.trendLocked === null, 'trend lock should be null with fewer than 60 closes (MA60 not yet ready)')
}

function testTrendAlignsWithMa5102060() {
  const result = computeThreeLocks(makeRows(130))
  assert(result.trendLocked === true, 'trend lock should be true for monotonically rising closes')
  assert(result.ma5 !== null && result.ma10 !== null && result.ma20 !== null && result.ma60 !== null, 'all 4 MAs should be computed')
}

function testCapitalIsThreeDayCumulative() {
  const result = computeThreeLocks(makeRows(130))
  assert(result.capitalLocked === true, 'capital lock should be true when 3-day cumulative inflow > 0')
  assert(result.capital3DaySum !== null && result.capital3DaySum > 0, 'capital 3-day sum should be positive')
}

function testCapitalNullWhenRecentInflowMissing() {
  const rows = withOverrides(makeRows(130), (row, index) =>
    index === 128 ? { ...row, main_net_inflow: null } : row
  )
  const result = computeThreeLocks(rows)
  assert(result.capitalLocked === null, 'capital lock should be null when a recent 3-day inflow is null')
}

function testCapitalLockedWhenAllThreeDaysNegative() {
  const rows = withOverrides(makeRows(130), (row, index) =>
    index >= 127 ? { ...row, main_net_inflow: -10 } : row
  )
  const result = computeThreeLocks(rows)
  assert(result.capitalLocked === false, 'capital lock should be false when 3-day sum is negative')
}

function testPatternExcludesRecentThreeDaysFromPriorHigh() {
  const rows = withOverrides(makeRows(130), (row, index) => {
    if (index === 126) return { ...row, high: 999 }
    if (index >= 127) return { ...row, high: 200 }
    return row
  })
  const result = computeThreeLocks(rows)
  assert(result.patternLocked === false, 'pattern lock should not compare recent highs against a baseline that includes the recent 3 days')
}

function testPatternUsesUpperShadowHigh() {
  const rows = withOverrides(makeRows(130), (row, index) => {
    if (index === 129) return { ...row, close: 129, high: 160 }
    return row
  })
  const result = computeThreeLocks(rows)
  assert(result.patternLocked === true, 'pattern lock should use high, so an upper shadow can create the 20-day high')
}

function testNullSafety() {
  const rows = withOverrides(makeRows(130), (row, index) => {
    if (index === 129) return { ...row, high: null, close: null, volume: null, main_net_inflow: null }
    return row
  })
  const result = computeThreeLocks(rows)
  assert(result.patternLocked === null, 'pattern lock should be null when latest close/high is missing')
  assert(result.capitalLocked === null, 'capital lock should be null when recent inflow is missing')
}

function testCombinedSignalDatesMapToComputedRows() {
  const rows = makeRows(130)
  const signals = buildThreeLockSignals(rows)
  assert(signals.length >= 1, 'signals should include at least the first full three-lock buy event')
  assert(signals[0].kind === 'buy', 'first three-lock signal should be buy')
}

function testPerLockTransitionsAreEmitted() {
  const rows = makeRows(130)
  const { perLock } = buildAllSignals(rows)
  const trendOns = perLock.filter(s => s.lock === 'trend' && s.direction === 'on')
  assert(trendOns.length >= 1, "per-lock trend should emit at least one 'on' transition")
}

function testTypeSafetyFixtureShape() {
  const row: ThreeLocksKLinePoint = {
    date: '2026-06-05',
    high: 12,
    low: 10,
    close: 11,
    volume: 1000,
    main_net_inflow: 200,
  }
  assert(computeThreeLocks([row]).date === row.date, 'fixture should satisfy the typed input contract')
}

function testClusterSignalsEmitOnStateChange() {
  const rows = makeRows(130)
  const clusters = buildClusterSignals(rows)
  assert(clusters.length >= 1, 'clusters should be emitted on state changes')
  const last = computeThreeLocks(rows)
  const lastCluster = clusters[clusters.length - 1]
  assert(
    lastCluster.states.trend === last.trendLocked &&
      lastCluster.states.capital === last.capitalLocked &&
      lastCluster.states.pattern === last.patternLocked,
    'last cluster should reflect current lock state'
  )
}

function testClusterSignalsCompact() {
  const rows = makeRows(130)
  const forced: ThreeLocksKLinePoint[] = rows.map(r => ({ ...r }))
  const clusters = buildClusterSignals(forced)
  assert(clusters.length < 10, 'stable state should not produce many clusters')
}

const tests = [
  testDescendingInputSortsBeforeCompute,
  testTrendNeeds60ValidCloses,
  testTrendAlignsWithMa5102060,
  testCapitalIsThreeDayCumulative,
  testCapitalNullWhenRecentInflowMissing,
  testCapitalLockedWhenAllThreeDaysNegative,
  testPatternExcludesRecentThreeDaysFromPriorHigh,
  testPatternUsesUpperShadowHigh,
  testNullSafety,
  testCombinedSignalDatesMapToComputedRows,
  testPerLockTransitionsAreEmitted,
  testTypeSafetyFixtureShape,
  testClusterSignalsEmitOnStateChange,
  testClusterSignalsCompact,
]

let failed = 0
for (const run of tests) {
  try {
    run()
    console.log(`PASS ${run.name}`)
  } catch (e) {
    failed += 1
    console.error(`FAIL ${run.name}: ${(e as Error).message}`)
  }
}
if (failed > 0) {
  console.error(`${failed}/${tests.length} tests failed`)
  process.exit(1)
}
console.log(`${tests.length}/${tests.length} tests passed`)

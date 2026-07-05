// 三把锁指标（趋势锁/资金锁/形态锁），移植自 ../fquant 的 threeLocks.ts。
// 算法逻辑与 fquant 对齐，不在图表层自创判定公式。

export interface ThreeLocksKLinePoint {
  date: string
  high: number | null | undefined
  low?: number | null | undefined
  close: number | null | undefined
  volume: number | null | undefined
  main_net_inflow: number | null | undefined
}

export type LockState = boolean | null
export type LockKey = 'trend' | 'capital' | 'pattern'
export type LockDirection = 'on' | 'off'

export interface MovingAveragePoint {
  date: string
  value: number | null
}

export interface ThreeLocksResult {
  date: string | null
  trendLocked: LockState
  capitalLocked: LockState
  patternLocked: LockState
  totalLocked: number
  ma5: number | null
  ma10: number | null
  ma20: number | null
  ma60: number | null
  capital3DaySum: number | null
  capitalValidDays: number
  recentHigh3: number | null
  priorHigh17: number | null
}

export interface ThreeLockSignal {
  kind: 'buy' | 'sell'
  date: string
  index: number
}

export interface PerLockSignal {
  lock: LockKey
  direction: LockDirection
  date: string
  index: number
}

export interface AllSignals {
  combined: ThreeLockSignal[]
  perLock: PerLockSignal[]
}

export interface LockStateSnapshot {
  trend: boolean
  capital: boolean
  pattern: boolean
}

export interface ClusterSignal {
  date: string
  index: number
  states: LockStateSnapshot
}

const MA_PERIODS = [5, 10, 20, 60] as const
const CAPITAL_LOOKBACK_DAYS = 3

function isFiniteNumber(value: number | null | undefined): value is number {
  return typeof value === 'number' && Number.isFinite(value)
}

function average(values: number[]): number | null {
  if (values.length === 0) return null
  return values.reduce((sum, value) => sum + value, 0) / values.length
}

function maxFinite(values: Array<number | null | undefined>): number | null {
  const finite = values.filter(isFiniteNumber)
  return finite.length === values.length && finite.length > 0 ? Math.max(...finite) : null
}

export function sortKLinesByDateAsc<T extends { date: string }>(rows: T[]): T[] {
  return rows.slice().sort((left, right) => left.date.localeCompare(right.date))
}

export function buildMovingAverageSeries(
  rows: ThreeLocksKLinePoint[],
  period: number
): MovingAveragePoint[] {
  const sorted = sortKLinesByDateAsc(rows)
  return sorted.map((row, index) => {
    if (index + 1 < period) {
      return { date: row.date, value: null }
    }
    const closes = sorted.slice(index + 1 - period, index + 1).map(item => item.close)
    if (!closes.every(isFiniteNumber)) {
      return { date: row.date, value: null }
    }
    return { date: row.date, value: average(closes as number[]) }
  })
}

export function computeThreeLocks(rows: ThreeLocksKLinePoint[]): ThreeLocksResult {
  const sorted = sortKLinesByDateAsc(rows)
  const latest = sorted[sorted.length - 1] ?? null

  const maSeries: Record<number, number | null> = {}
  for (const period of MA_PERIODS) {
    const series = buildMovingAverageSeries(sorted, period)
    maSeries[period] = series[series.length - 1]?.value ?? null
  }

  const trendLocked =
    maSeries[5] === null || maSeries[10] === null || maSeries[20] === null || maSeries[60] === null
      ? null
      : maSeries[5]! > maSeries[10]! &&
        maSeries[10]! > maSeries[20]! &&
        maSeries[20]! > maSeries[60]!

  const last3 = sorted.slice(-CAPITAL_LOOKBACK_DAYS)
  const validInflowDays = last3.filter(item => item.main_net_inflow != null).length
  const capital3DaySum = last3.reduce((sum, item) => sum + (item.main_net_inflow ?? 0), 0)
  const capitalLocked = validInflowDays < CAPITAL_LOOKBACK_DAYS ? null : capital3DaySum > 0

  const latestClose = latest?.close
  const recentHigh3 = sorted.length >= 3 ? maxFinite(sorted.slice(-3).map(item => item.high)) : null
  const priorHigh17 = sorted.length >= 20 ? maxFinite(sorted.slice(-20, -3).map(item => item.high)) : null
  const patternLocked =
    maSeries[20] === null || !isFiniteNumber(latestClose) || recentHigh3 === null || priorHigh17 === null
      ? null
      : latestClose > maSeries[20]! && recentHigh3 > priorHigh17

  const states: LockState[] = [trendLocked, capitalLocked, patternLocked]
  return {
    date: latest?.date ?? null,
    trendLocked,
    capitalLocked,
    patternLocked,
    totalLocked: states.filter(state => state === true).length,
    ma5: maSeries[5],
    ma10: maSeries[10],
    ma20: maSeries[20],
    ma60: maSeries[60],
    capital3DaySum: validInflowDays === CAPITAL_LOOKBACK_DAYS ? capital3DaySum : null,
    capitalValidDays: validInflowDays,
    recentHigh3,
    priorHigh17,
  }
}

function stateOf(result: ThreeLocksResult, lock: LockKey): LockState {
  if (lock === 'trend') return result.trendLocked
  if (lock === 'capital') return result.capitalLocked
  return result.patternLocked
}

export function buildThreeLockSignals(rows: ThreeLocksKLinePoint[]): ThreeLockSignal[] {
  return buildAllSignals(rows).combined
}

export function buildAllSignals(rows: ThreeLocksKLinePoint[]): AllSignals {
  const sorted = sortKLinesByDateAsc(rows)
  const combined: ThreeLockSignal[] = []
  const perLock: PerLockSignal[] = []

  const previous: Record<LockKey, LockState> = {
    trend: null,
    capital: null,
    pattern: null,
  }
  let previousAllOpen = false

  sorted.forEach((row, index) => {
    const result = computeThreeLocks(sorted.slice(0, index + 1))
    const allOpen =
      result.trendLocked === true &&
      result.capitalLocked === true &&
      result.patternLocked === true

    if (allOpen && !previousAllOpen) {
      combined.push({ kind: 'buy', date: row.date, index })
    }
    if (!allOpen && previousAllOpen) {
      combined.push({ kind: 'sell', date: row.date, index })
    }
    previousAllOpen = allOpen

    ;(['trend', 'capital', 'pattern'] as LockKey[]).forEach(lock => {
      const current = stateOf(result, lock)
      const prev = previous[lock]
      if (current === null) return
      if (prev === null) {
        perLock.push({ lock, direction: current ? 'on' : 'off', date: row.date, index })
        previous[lock] = current
        return
      }
      if (current !== prev) {
        perLock.push({ lock, direction: current ? 'on' : 'off', date: row.date, index })
        previous[lock] = current
      }
    })
  })

  return { combined, perLock }
}

export function buildClusterSignals(rows: ThreeLocksKLinePoint[]): ClusterSignal[] {
  const sorted = sortKLinesByDateAsc(rows)
  const signals: ClusterSignal[] = []
  let previous: LockStateSnapshot | null = null

  sorted.forEach((row, index) => {
    const r = computeThreeLocks(sorted.slice(0, index + 1))
    if (r.trendLocked === null || r.capitalLocked === null || r.patternLocked === null) {
      return
    }
    const states: LockStateSnapshot = {
      trend: r.trendLocked,
      capital: r.capitalLocked,
      pattern: r.patternLocked,
    }
    if (
      previous === null ||
      previous.trend !== states.trend ||
      previous.capital !== states.capital ||
      previous.pattern !== states.pattern
    ) {
      signals.push({ date: row.date, index, states })
      previous = states
    }
  })

  return signals
}

import { describe, expect, it } from 'vitest'

import {
  activeSuddenAnomalyKeys,
  advanceSuddenAnomalyState,
  SUDDEN_ANOMALY_METRICS,
  SUDDEN_ANOMALY_THRESHOLDS,
  suddenAnomalyKey,
  type SuddenAnomalyMetric,
  type SuddenAnomalySymbolReading,
  type SuddenAnomalyTrackerState,
} from './suddenAnomalyHighlights'

const EMPTY_STATE: SuddenAnomalyTrackerState = {
  baselines: {},
  expiresAt: {},
}

const METRICS: SuddenAnomalyMetric[] = [
  'changePct',
  'momentum1m',
  'volumeSpeed',
  'depthPressurePct',
  'toDayHighPct',
  'fromDayLowPct',
]

function reading(
  metric: SuddenAnomalyMetric,
  value: number | null,
  delayed = false,
  symbol = '700.HK',
): SuddenAnomalySymbolReading {
  return {
    symbol,
    metrics: Object.fromEntries(METRICS.map(key => [
      key,
      {
        value: key === metric ? value : null,
        delayed: key === metric ? delayed : false,
      },
    ])) as SuddenAnomalySymbolReading['metrics'],
  }
}

describe('sudden anomaly highlight state', () => {
  it('exports one stable metric order aligned with every threshold', () => {
    expect(SUDDEN_ANOMALY_METRICS).toEqual(METRICS)
    expect(Object.keys(SUDDEN_ANOMALY_THRESHOLDS)).toEqual(
      SUDDEN_ANOMALY_METRICS,
    )
  })

  it.each([
    ['changePct', 1, 1.50],
    ['momentum1m', 0.1, 0.50],
    ['volumeSpeed', 1.2, 2.20],
    ['depthPressurePct', -10, 30],
    ['toDayHighPct', 2, 1.50],
    ['fromDayLowPct', 1, 1.50],
  ] satisfies Array<[SuddenAnomalyMetric, number, number]>)(
    'highlights %s when the next valid value reaches its threshold',
    (metric, baselineValue, changedValue) => {
      const baseline = advanceSuddenAnomalyState(
        EMPTY_STATE,
        [reading(metric, baselineValue)],
        1_000,
      )
      const triggered = advanceSuddenAnomalyState(
        baseline,
        [reading(metric, changedValue)],
        2_000,
      )

      expect(activeSuddenAnomalyKeys(triggered, 2_000)).toContain(
        suddenAnomalyKey('700.HK', metric),
      )
    },
  )

  it.each([
    ['changePct', 1, 1.49],
    ['momentum1m', 0.1, 0.49],
    ['volumeSpeed', 1.2, 2.19],
    ['depthPressurePct', -10, 29.99],
    ['toDayHighPct', 2, 1.51],
    ['fromDayLowPct', 1, 1.49],
  ] satisfies Array<[SuddenAnomalyMetric, number, number]>)(
    'does not highlight %s below its threshold',
    (metric, baselineValue, changedValue) => {
      const baseline = advanceSuddenAnomalyState(
        EMPTY_STATE,
        [reading(metric, baselineValue)],
        1_000,
      )
      const unchanged = advanceSuddenAnomalyState(
        baseline,
        [reading(metric, changedValue)],
        2_000,
      )

      expect(activeSuddenAnomalyKeys(unchanged, 2_000)).not.toContain(
        suddenAnomalyKey('700.HK', metric),
      )
    },
  )

  it('uses the first valid value only as a baseline', () => {
    const baseline = advanceSuddenAnomalyState(
      EMPTY_STATE,
      [reading('changePct', 8)],
      1_000,
    )

    expect(activeSuddenAnomalyKeys(baseline, 1_000)).toEqual(new Set())
    expect(baseline.baselines).toEqual({
      [suddenAnomalyKey('700.HK', 'changePct')]: 8,
    })
  })

  it.each([
    ['missing', null, false],
    ['delayed', 3, true],
    ['NaN', Number.NaN, false],
    ['infinite', Number.POSITIVE_INFINITY, false],
  ])(
    'resets the baseline for %s data and suppresses the first recovered value',
    (_caseName, invalidValue, delayed) => {
      const key = suddenAnomalyKey('700.HK', 'changePct')
      const baseline = advanceSuddenAnomalyState(
        EMPTY_STATE,
        [reading('changePct', 1)],
        1_000,
      )
      const reset = advanceSuddenAnomalyState(
        baseline,
        [reading('changePct', invalidValue, delayed)],
        2_000,
      )
      const recovered = advanceSuddenAnomalyState(
        reset,
        [reading('changePct', 5)],
        3_000,
      )

      expect(reset.baselines).not.toHaveProperty(key)
      expect(reset.expiresAt).not.toHaveProperty(key)
      expect(activeSuddenAnomalyKeys(recovered, 3_000)).not.toContain(key)
      expect(recovered.baselines[key]).toBe(5)
    },
  )

  it('expires a highlight at exactly ten seconds', () => {
    const baseline = advanceSuddenAnomalyState(
      EMPTY_STATE,
      [reading('changePct', 1)],
      0,
    )
    const triggered = advanceSuddenAnomalyState(
      baseline,
      [reading('changePct', 1.5)],
      1_000,
    )
    const key = suddenAnomalyKey('700.HK', 'changePct')

    expect(activeSuddenAnomalyKeys(triggered, 10_999)).toContain(key)
    expect(activeSuddenAnomalyKeys(triggered, 11_000)).not.toContain(key)
  })

  it('extends the expiry to ten seconds after a second qualifying change', () => {
    const baseline = advanceSuddenAnomalyState(
      EMPTY_STATE,
      [reading('changePct', 1)],
      0,
    )
    const firstTrigger = advanceSuddenAnomalyState(
      baseline,
      [reading('changePct', 1.5)],
      1_000,
    )
    const secondTrigger = advanceSuddenAnomalyState(
      firstTrigger,
      [reading('changePct', 2)],
      6_000,
    )
    const key = suddenAnomalyKey('700.HK', 'changePct')

    expect(activeSuddenAnomalyKeys(secondTrigger, 15_999)).toContain(key)
    expect(activeSuddenAnomalyKeys(secondTrigger, 16_000)).not.toContain(key)
  })

  it('removes baselines and highlights for symbols that leave the page', () => {
    const baseline = advanceSuddenAnomalyState(
      EMPTY_STATE,
      [reading('changePct', 1, false, 'abc.us')],
      0,
    )
    const triggered = advanceSuddenAnomalyState(
      baseline,
      [reading('changePct', 1.5, false, 'abc.us')],
      1_000,
    )
    const cleared = advanceSuddenAnomalyState(triggered, [], 2_000)

    expect(cleared).toEqual(EMPTY_STATE)
    expect(suddenAnomalyKey('abc.us', 'changePct')).toBe(
      suddenAnomalyKey('ABC.US', 'changePct'),
    )
  })
})

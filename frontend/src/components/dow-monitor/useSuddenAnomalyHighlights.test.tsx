import { act, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import {
  suddenAnomalyKey,
  type SuddenAnomalyMetric,
  type SuddenAnomalySymbolReading,
} from './suddenAnomalyHighlights'
import { useSuddenAnomalyHighlights } from './useSuddenAnomalyHighlights'

const METRICS: SuddenAnomalyMetric[] = [
  'changePct',
  'momentum1m',
  'volumeSpeed',
  'depthPressurePct',
  'toDayHighPct',
  'fromDayLowPct',
]

function readings(changePct: number): SuddenAnomalySymbolReading[] {
  return [{
    symbol: '700.HK',
    metrics: Object.fromEntries(METRICS.map(metric => [
      metric,
      {
        value: metric === 'changePct' ? changePct : null,
        delayed: false,
      },
    ])) as SuddenAnomalySymbolReading['metrics'],
  }]
}

describe('useSuddenAnomalyHighlights', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    vi.setSystemTime(0)
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  it('clears a triggered highlight exactly ten seconds later without new data', () => {
    const key = suddenAnomalyKey('700.HK', 'changePct')
    const { result, rerender } = renderHook(
      ({ changePct }) => useSuddenAnomalyHighlights(readings(changePct)),
      { initialProps: { changePct: 1 } },
    )

    expect(result.current.has(key)).toBe(false)

    act(() => {
      vi.setSystemTime(1_000)
      rerender({ changePct: 1.5 })
    })
    expect(result.current.has(key)).toBe(true)

    act(() => {
      vi.advanceTimersByTime(9_999)
    })
    expect(result.current.has(key)).toBe(true)

    act(() => {
      vi.advanceTimersByTime(1)
    })
    expect(result.current.has(key)).toBe(false)
  })

  it('reschedules expiry from a second qualifying change', () => {
    const key = suddenAnomalyKey('700.HK', 'changePct')
    const { result, rerender } = renderHook(
      ({ changePct }) => useSuddenAnomalyHighlights(readings(changePct)),
      { initialProps: { changePct: 1 } },
    )

    act(() => {
      vi.setSystemTime(1_000)
      rerender({ changePct: 1.5 })
      vi.advanceTimersByTime(5_000)
      rerender({ changePct: 2 })
    })
    act(() => {
      vi.advanceTimersByTime(9_999)
    })
    expect(result.current.has(key)).toBe(true)

    act(() => {
      vi.advanceTimersByTime(1)
    })
    expect(result.current.has(key)).toBe(false)
  })

  it('cleans up its single expiry timer on unmount', () => {
    const { rerender, unmount } = renderHook(
      ({ changePct }) => useSuddenAnomalyHighlights(readings(changePct)),
      { initialProps: { changePct: 1 } },
    )

    act(() => {
      vi.setSystemTime(1_000)
      rerender({ changePct: 1.5 })
    })
    expect(vi.getTimerCount()).toBe(1)

    unmount()

    expect(vi.getTimerCount()).toBe(0)
  })
})

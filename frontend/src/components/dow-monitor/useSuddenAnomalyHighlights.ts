import { useCallback, useEffect, useRef, useState } from 'react'

import {
  activeSuddenAnomalyKeys,
  advanceSuddenAnomalyState,
  type SuddenAnomalySymbolReading,
  type SuddenAnomalyTrackerState,
} from './suddenAnomalyHighlights'

const EMPTY_STATE: SuddenAnomalyTrackerState = {
  baselines: {},
  expiresAt: {},
}

function equalSets(left: ReadonlySet<string>, right: ReadonlySet<string>): boolean {
  if (left.size !== right.size) return false
  for (const value of left) {
    if (!right.has(value)) return false
  }
  return true
}

export function useSuddenAnomalyHighlights(
  readings: SuddenAnomalySymbolReading[],
): ReadonlySet<string> {
  const trackerRef = useRef<SuddenAnomalyTrackerState>(EMPTY_STATE)
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const [activeKeys, setActiveKeys] = useState<ReadonlySet<string>>(() => new Set())

  const publishAndSchedule = useCallback(function publish(nowMs: number) {
    if (timerRef.current != null) {
      clearTimeout(timerRef.current)
      timerRef.current = null
    }

    const expiresAt = Object.fromEntries(
      Object.entries(trackerRef.current.expiresAt)
        .filter(([, expiry]) => expiry > nowMs),
    )
    trackerRef.current = {
      baselines: trackerRef.current.baselines,
      expiresAt,
    }

    const nextKeys = activeSuddenAnomalyKeys(trackerRef.current, nowMs)
    setActiveKeys(previous => equalSets(previous, nextKeys) ? previous : nextKeys)

    const nextExpiry = Math.min(...Object.values(expiresAt))
    if (Number.isFinite(nextExpiry)) {
      timerRef.current = setTimeout(
        () => publish(Date.now()),
        Math.max(0, nextExpiry - nowMs),
      )
    }
  }, [])

  useEffect(() => {
    const nowMs = Date.now()
    trackerRef.current = advanceSuddenAnomalyState(
      trackerRef.current,
      readings,
      nowMs,
    )
    publishAndSchedule(nowMs)
  }, [publishAndSchedule, readings])

  useEffect(() => () => {
    if (timerRef.current != null) clearTimeout(timerRef.current)
  }, [])

  return activeKeys
}

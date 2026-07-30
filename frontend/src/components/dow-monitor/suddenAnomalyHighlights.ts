export type SuddenAnomalyMetric =
  | 'changePct'
  | 'momentum1m'
  | 'volumeSpeed'
  | 'depthPressurePct'
  | 'toDayHighPct'
  | 'fromDayLowPct'

export interface SuddenAnomalyMetricReading {
  value: number | null
  delayed: boolean
}

export interface SuddenAnomalySymbolReading {
  symbol: string
  metrics: Record<SuddenAnomalyMetric, SuddenAnomalyMetricReading>
}

export interface SuddenAnomalyTrackerState {
  baselines: Record<string, number>
  expiresAt: Record<string, number>
}

export const SUDDEN_ANOMALY_THRESHOLDS: Record<SuddenAnomalyMetric, number> = {
  changePct: 0.50,
  momentum1m: 0.40,
  volumeSpeed: 1.00,
  depthPressurePct: 40,
  toDayHighPct: 0.50,
  fromDayLowPct: 0.50,
}

export const SUDDEN_ANOMALY_DURATION_MS = 10_000

export const SUDDEN_ANOMALY_METRICS = Object.keys(
  SUDDEN_ANOMALY_THRESHOLDS,
) as SuddenAnomalyMetric[]

export function suddenAnomalyKey(
  symbol: string,
  metric: SuddenAnomalyMetric,
): string {
  return `${symbol.toUpperCase()}::${metric}`
}

export function advanceSuddenAnomalyState(
  previous: SuddenAnomalyTrackerState,
  readings: SuddenAnomalySymbolReading[],
  nowMs: number,
): SuddenAnomalyTrackerState {
  const currentKeys = new Set(
    readings.flatMap(reading =>
      SUDDEN_ANOMALY_METRICS.map(metric => suddenAnomalyKey(reading.symbol, metric))),
  )
  const baselines = Object.fromEntries(
    Object.entries(previous.baselines)
      .filter(([key]) => currentKeys.has(key)),
  )
  const expiresAt = Object.fromEntries(
    Object.entries(previous.expiresAt)
      .filter(([key, expiry]) => currentKeys.has(key) && expiry > nowMs),
  )

  for (const reading of readings) {
    for (const metric of SUDDEN_ANOMALY_METRICS) {
      const key = suddenAnomalyKey(reading.symbol, metric)
      const next = reading.metrics[metric]
      if (next.delayed || next.value == null || !Number.isFinite(next.value)) {
        delete baselines[key]
        delete expiresAt[key]
        continue
      }

      const prior = baselines[key]
      if (
        Number.isFinite(prior)
        && Math.abs(next.value - prior) >= SUDDEN_ANOMALY_THRESHOLDS[metric]
      ) {
        expiresAt[key] = nowMs + SUDDEN_ANOMALY_DURATION_MS
      }
      baselines[key] = next.value
    }
  }

  return { baselines, expiresAt }
}

export function activeSuddenAnomalyKeys(
  state: SuddenAnomalyTrackerState,
  nowMs: number,
): Set<string> {
  return new Set(
    Object.entries(state.expiresAt)
      .filter(([, expiry]) => expiry > nowMs)
      .map(([key]) => key),
  )
}

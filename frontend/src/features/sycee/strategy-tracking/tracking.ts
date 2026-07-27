import type { StrategyBacktestResult } from '../strategy-compare/api.ts'
import type {
  StrategyObservation,
  StrategyObservationInput,
  StrategyTrack,
} from './api.ts'

export function normalizeSymbolDraft(value: string): string[] {
  return Array.from(new Set(
    value
      .split(/[\s,，;；]+/)
      .map(item => item.trim().toUpperCase())
      .filter(Boolean),
  ))
}

export function latestObservation(track: StrategyTrack): StrategyObservation | null {
  return track.observations.reduce<StrategyObservation | null>(
    (latest, item) => (!latest || item.end_date > latest.end_date ? item : latest),
    null,
  )
}

export function trackingSummary(tracks: StrategyTrack[]) {
  return {
    tracking: tracks.filter(track => track.status === 'tracking').length,
    paused: tracks.filter(track => track.status === 'paused').length,
    closed: tracks.filter(track => track.status === 'closed').length,
    observations: tracks.reduce((total, track) => total + track.observations.length, 0),
  }
}

function finiteStat(stats: Record<string, unknown>, ...keys: string[]): number | null {
  for (const key of keys) {
    const value = stats[key]
    if (typeof value === 'number' && Number.isFinite(value)) return value
  }
  return null
}

export function observationFromResult(
  result: StrategyBacktestResult,
  endDate: string,
): StrategyObservationInput {
  const endingEquity = [...result.equity_curve]
    .reverse()
    .find(point => Number.isFinite(point.value))?.value ?? null
  const tradeCount = finiteStat(result.stats, 'n_trades', 'trade_count')
  return {
    end_date: endDate,
    run_id: result.run_id,
    total_return: finiteStat(result.stats, 'total_return'),
    annual_return: finiteStat(result.stats, 'annual_return'),
    sharpe: finiteStat(result.stats, 'sharpe'),
    max_drawdown: finiteStat(result.stats, 'max_drawdown'),
    win_rate: finiteStat(result.stats, 'win_rate'),
    trade_count: tradeCount == null ? null : Math.max(0, Math.trunc(tradeCount)),
    ending_equity: endingEquity,
    elapsed_ms: Number.isFinite(result.elapsed_ms) ? Math.max(0, result.elapsed_ms) : 0,
  }
}

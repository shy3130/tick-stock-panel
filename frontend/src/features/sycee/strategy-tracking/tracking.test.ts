import test from 'node:test'
import assert from 'node:assert/strict'

import type { StrategyBacktestResult } from '../strategy-compare/api.ts'
import type { StrategyTrack } from './api.ts'
import {
  latestObservation,
  normalizeSymbolDraft,
  observationFromResult,
  trackingSummary,
} from './tracking.ts'

const track = {
  id: 'strategy_track_1',
  strategy_id: 'trend',
  strategy_name: '趋势',
  symbols: ['600519.SH'],
  start_date: '2026-01-01',
  initial_capital: 1_000_000,
  max_positions: 10,
  commission_pct: 0.0002,
  stamp_tax_pct: 0.001,
  slippage_bps: 5,
  params: {},
  overrides: {},
  note: '',
  status: 'tracking' as const,
  observations: [
    { id: 'o1', end_date: '2026-02-01', observed_at: '', run_id: 'r1', total_return: 0.1, annual_return: null, sharpe: null, max_drawdown: null, win_rate: null, trade_count: 2, ending_equity: 1_100_000, elapsed_ms: 10 },
    { id: 'o2', end_date: '2026-03-01', observed_at: '', run_id: 'r2', total_return: 0.2, annual_return: null, sharpe: null, max_drawdown: null, win_rate: null, trade_count: 3, ending_equity: 1_200_000, elapsed_ms: 12 },
  ],
  created_at: '',
  updated_at: '',
} satisfies StrategyTrack

test('normalizes a mixed symbol list without duplicates', () => {
  assert.deepEqual(
    normalizeSymbolDraft('600519.sh, 000001.SZ；600519.SH\n510300.SH'),
    ['600519.SH', '000001.SZ', '510300.SH'],
  )
})

test('summarizes lifecycle state and finds the latest dated observation', () => {
  assert.equal(latestObservation(track)?.id, 'o2')
  assert.deepEqual(trackingSummary([
    track,
    { ...track, id: 'paused', status: 'paused', observations: [] },
    { ...track, id: 'closed', status: 'closed', observations: [] },
  ]), { tracking: 1, paused: 1, closed: 1, observations: 2 })
})

test('maps finite public backtest metrics into a stored observation', () => {
  const result = {
    run_id: 'abc123',
    stats: {
      total_return: 0.16,
      annual_return: 0.25,
      sharpe: 1.4,
      max_drawdown: -0.09,
      win_rate: 0.55,
      n_trades: 12.8,
    },
    equity_curve: [
      { date: '2026-01-01', value: 1_000_000 },
      { date: '2026-03-31', value: 1_160_000 },
    ],
    strategy_info: { id: 'trend', name: '趋势' },
    elapsed_ms: 830,
    error: null,
  } satisfies StrategyBacktestResult

  assert.deepEqual(observationFromResult(result, '2026-03-31'), {
    end_date: '2026-03-31',
    run_id: 'abc123',
    total_return: 0.16,
    annual_return: 0.25,
    sharpe: 1.4,
    max_drawdown: -0.09,
    win_rate: 0.55,
    trade_count: 12,
    ending_equity: 1_160_000,
    elapsed_ms: 830,
  })
})

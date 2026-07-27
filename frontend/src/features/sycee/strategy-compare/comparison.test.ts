import test from 'node:test'
import assert from 'node:assert/strict'

import type { ComparableStrategy } from './api.ts'
import {
  buildMetricRow,
  buildStrategyDefaults,
  normalizeEquityCurve,
  validateComparison,
  type ComparisonResult,
  type ComparisonSettings,
} from './comparison.ts'
import { buildComparisonCsv } from './comparisonCsv.ts'
import { buildStrategyBacktestQuery } from './comparisonStream.ts'

const settings: ComparisonSettings = {
  symbols: ['000001.SZ'],
  start: '2026-01-01',
  end: '2026-03-31',
  initialCapital: 1_000_000,
  maxPositions: 10,
  commissionPct: 0.0002,
  stampTaxPct: 0.001,
  slippageBps: 5,
}

const strategy: ComparableStrategy = {
  id: 'matrix',
  name: '矩阵策略',
  description: '',
  source: 'builtin',
  execution_backend: 'matrix_native',
  params: [{ id: 'period', default: 20 }],
  params_defaults: {},
  basic_filter: { exclude_st: true },
  scoring: { momentum: 1 },
  entry_signals: ['breakout'],
  exit_signals: ['signal_breakdown'],
  stop_loss: -0.06,
  take_profit: null,
  trailing_stop: null,
  trailing_take_profit_activate: null,
  trailing_take_profit_drawdown: null,
  max_hold_days: 15,
}

test('validates strategy count and shared settings boundaries', () => {
  assert.equal(validateComparison(['one'], settings), '请至少选择 2 个策略')
  assert.equal(validateComparison(['1', '2', '3', '4', '5', '6'], settings), '最多同时比较 5 个策略')
  assert.equal(validateComparison(['1', '2'], { ...settings, symbols: [] }), '股票池不能为空')
  assert.equal(validateComparison(['1', '2'], { ...settings, start: '2026-04-01' }), '开始日期不能晚于结束日期')
  assert.equal(validateComparison(['1', '2'], settings), null)
})

test('builds strategy defaults without overriding matrix-owned signals', () => {
  const defaults = buildStrategyDefaults(strategy)
  assert.deepEqual(defaults.params, { period: 20 })
  assert.equal('entry_signals' in defaults.overrides, false)
  assert.equal('exit_signals' in defaults.overrides, false)
  assert.deepEqual(defaults.overrides.basic_filter, { exclude_st: true })
})

test('normalizes equity and maps public backtest statistics', () => {
  const item: ComparisonResult = {
    strategyId: 'one',
    strategyName: '策略一',
    result: {
      run_id: 'run-1',
      stats: {
        total_return: 0.2,
        annual_return: 0.35,
        sharpe: 1.5,
        max_drawdown: -0.08,
        win_rate: 0.6,
        n_trades: 12,
      },
      equity_curve: [
        { date: '2026-01-01T00:00:00', value: 100 },
        { date: '2026-01-02T00:00:00', value: 110 },
      ],
      strategy_info: { id: 'one', name: '策略一' },
      elapsed_ms: 321,
      error: null,
    },
  }
  assert.deepEqual(normalizeEquityCurve(item).points, [
    { date: '2026-01-01', value: 1 },
    { date: '2026-01-02', value: 1.1 },
  ])
  assert.deepEqual(buildMetricRow(item), {
    strategyId: 'one',
    strategyName: '策略一',
    totalReturn: 0.2,
    annualReturn: 0.35,
    sharpe: 1.5,
    maxDrawdown: -0.08,
    winRate: 0.6,
    tradeCount: 12,
    elapsedMs: 321,
  })
})

test('exports an Excel-friendly CSV and neutralizes formula cells', () => {
  const csv = buildComparisonCsv([{
    strategyId: '=unsafe',
    strategyName: '策略,一',
    totalReturn: 0.2,
    annualReturn: 0.35,
    sharpe: 1.5,
    maxDrawdown: -0.08,
    winRate: 0.6,
    tradeCount: 12,
    elapsedMs: 321,
  }], settings)
  assert.ok(csv.startsWith('\uFEFF策略ID'))
  assert.match(csv, /'=unsafe/)
  assert.match(csv, /"策略,一"/)
  assert.match(csv, /000001\.SZ/)
})

test('builds the isolated stream request with shared execution assumptions', () => {
  const defaults = buildStrategyDefaults(strategy)
  const query = new URLSearchParams(buildStrategyBacktestQuery({
    strategyId: strategy.id,
    ...settings,
    params: defaults.params,
    overrides: defaults.overrides,
  }))
  assert.equal(query.get('strategy_id'), 'matrix')
  assert.equal(query.get('symbols'), '000001.SZ')
  assert.equal(query.get('entry_fill'), 'open_t+1')
  assert.equal(query.get('max_exposure_pct'), '1')
  assert.equal(query.get('asset_type'), 'stock')
})

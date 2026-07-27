import test from 'node:test'
import assert from 'node:assert/strict'

import type { StrategyBacktestResult, StrategyBacktestTrade } from '@/lib/api'
import { buildBacktestTradeExportFilename, buildBacktestTradesCsv } from './backtestTradeCsv.ts'

function makeTrade(overrides: Partial<StrategyBacktestTrade> = {}): StrategyBacktestTrade {
  return {
    symbol: '000001.SZ',
    name: '平安银行',
    entry_date: '2026-01-05',
    exit_date: '2026-01-09',
    entry_price: 10.25,
    exit_price: 10.8,
    pnl_pct: 0.0536585,
    duration: 4,
    exit_reason: 'signal',
    ...overrides,
  }
}

function makeResult(trades: StrategyBacktestTrade[], name = '趋势策略'): StrategyBacktestResult {
  return {
    run_id: 'run-001',
    config: { start: '2026-01-01', end: '2026-06-30' },
    stats: {},
    equity_curve: [],
    drawdown_curve: [],
    trades,
    per_symbol_stats: [],
    strategy_info: {
      id: 'trend_strategy',
      name,
      description: '',
      entry_signals: [],
      exit_signals: [],
      stop_loss: null,
      take_profit: null,
      trailing_stop: null,
      trailing_take_profit_activate: null,
      trailing_take_profit_drawdown: null,
      score_min: null,
      score_max: null,
      max_hold_days: null,
      source: 'custom',
    },
    elapsed_ms: 10,
    error: null,
  }
}

test('builds an Excel-compatible CSV with all trade fields', () => {
  const csv = buildBacktestTradesCsv(makeResult([
    makeTrade({
      shares: 1200,
      lots: 12,
      position_pct: 0.125,
      entry_value: 12300,
      exit_value: 12960,
      pnl_amount: 660,
      entry_score: 82.5,
      entry_signal_date: '2026-01-02',
      exit_signal_date: '2026-01-09',
      blocked_exit_days: 0,
      entry_signal_id: 'signal_ma_golden_5_20',
      exit_signal_id: 'signal_ma_dead_5_20',
    }),
  ]))

  assert.ok(csv.startsWith('\uFEFF回测ID,策略ID,策略名称,标的代码'))
  assert.ok(csv.includes('\r\nrun-001,trend_strategy,趋势策略,000001.SZ,平安银行'))
  assert.ok(csv.includes(',1200,12,0.125,12300,12960,660,0.0536585'))
  assert.equal(csv.split('\r\n').length, 2)
})

test('escapes CSV text and neutralizes spreadsheet formulas without changing negative numbers', () => {
  const csv = buildBacktestTradesCsv(makeResult([
    makeTrade({ name: '=HYPERLINK("https://example.com","测试,名称")', pnl_amount: -20 }),
  ]))

  assert.ok(csv.includes('"\'=HYPERLINK(""https://example.com"",""测试,名称"")"'))
  assert.ok(csv.includes(',-20,0.0536585'))
})

test('uses empty cells for missing optional values and non-finite numbers', () => {
  const csv = buildBacktestTradesCsv(makeResult([
    makeTrade({ name: undefined, entry_score: Number.NaN, pnl_amount: undefined }),
  ]))
  const row = csv.split('\r\n')[1]

  assert.ok(row.includes(',000001.SZ,,,'))
  assert.ok(row.endsWith(',0.0536585'))
})

test('builds a filesystem-safe filename from strategy and date range', () => {
  const filename = buildBacktestTradeExportFilename(makeResult([], '趋势/突破:增强版'))

  assert.equal(filename, '回测交易明细_趋势_突破_增强版_2026-01-01_2026-06-30.csv')
})

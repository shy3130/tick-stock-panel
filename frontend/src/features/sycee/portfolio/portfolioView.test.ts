import test from 'node:test'
import assert from 'node:assert/strict'

import type { Portfolio } from './api.ts'
import { buildPortfolioView } from './portfolioView.ts'

const portfolio: Portfolio = {
  trades: [],
  positions: [
    {
      symbol: '600519.SH',
      name: '贵州茅台',
      quantity: 10,
      average_cost: 1500,
      cost_value: 15000,
      realized_pnl: 500,
      first_trade_date: '2026-01-02',
      last_trade_date: '2026-01-02',
    },
    {
      symbol: '000001.SZ',
      name: '平安银行',
      quantity: 1000,
      average_cost: 10,
      cost_value: 10000,
      realized_pnl: 0,
      first_trade_date: '2026-01-02',
      last_trade_date: '2026-01-02',
    },
  ],
  summary: {
    position_count: 2,
    trade_count: 2,
    cost_value: 25000,
    realized_pnl: 500,
  },
}

test('calculates market value and floating profit only for priced positions', () => {
  const view = buildPortfolioView(portfolio, {
    '600519.SH': {
      symbol: '600519.SH',
      name: '贵州茅台',
      price: 1600,
      change_pct: 0.01,
      date: '2026-07-27',
      is_live: true,
    },
  })

  assert.equal(view.positions[0].market_value, 16000)
  assert.equal(view.positions[0].unrealized_pnl, 1000)
  assert.equal(view.positions[0].return_pct, 1 / 15)
  assert.equal(view.positions[0].is_live, true)
  assert.equal(view.positions[1].current_price, null)
  assert.deepEqual(view.summary, {
    cost_value: 25000,
    priced_cost_value: 15000,
    market_value: 16000,
    unrealized_pnl: 1000,
    realized_pnl: 500,
    unpriced_count: 1,
  })
})

test('uses the quote name when the position has no saved name', () => {
  const unnamed: Portfolio = {
    ...portfolio,
    positions: [{ ...portfolio.positions[0], name: '' }],
    summary: { ...portfolio.summary, position_count: 1, cost_value: 15000 },
  }
  const view = buildPortfolioView(unnamed, {
    '600519.SH': {
      symbol: '600519.SH',
      name: '贵州茅台',
      price: 1500,
      change_pct: null,
      date: '2026-07-26',
      is_live: false,
    },
  })

  assert.equal(view.positions[0].name, '贵州茅台')
  assert.equal(view.summary.unpriced_count, 0)
})

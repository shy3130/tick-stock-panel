import test from 'node:test'
import assert from 'node:assert/strict'

import type { TradeReviewItem } from './api.ts'
import { filterTradeReviewItems } from './reviewView.ts'

const baseTrade = {
  id: 'trade_00000000000000000000000000000001',
  symbol: '600519.SH',
  name: '贵州茅台',
  side: 'sell' as const,
  quantity: 10,
  price: 1500,
  fees: 5,
  trade_date: '2026-01-10',
  note: '',
  created_at: '2026-01-10T00:00:00Z',
  updated_at: '2026-01-10T00:00:00Z',
}

function item(overrides: Partial<TradeReviewItem> = {}): TradeReviewItem {
  return {
    trade: baseTrade,
    attribution: {
      cost_basis: 14000,
      realized_pnl: 995,
      return_pct: 0.07107143,
      holding_days: 8,
      pnl_result: 'profit',
    },
    review: {
      id: 'trade_review_00000000000000000000000000000001',
      trade_id: baseTrade.id,
      strategy_id: 'trend_breakout',
      entry_reason: '',
      expectation: '',
      invalidation: '',
      exit_reason: '止盈',
      conclusion: '退出稍早',
      mistake_tags: ['early_exit'],
      created_at: '2026-01-10T00:00:00Z',
      updated_at: '2026-01-10T00:00:00Z',
    },
    ...overrides,
  }
}

test('filters reviews by strategy, mistake and pnl result', () => {
  const loss = item({
    trade: { ...baseTrade, id: 'trade_00000000000000000000000000000002' },
    attribution: {
      cost_basis: 1000,
      realized_pnl: -100,
      return_pct: -0.1,
      holding_days: 3,
      pnl_result: 'loss',
    },
    review: {
      ...item().review!,
      id: 'trade_review_00000000000000000000000000000002',
      trade_id: 'trade_00000000000000000000000000000002',
      strategy_id: 'ma_cross',
      mistake_tags: ['late_exit'],
    },
  })
  const items = [item(), loss]

  assert.deepEqual(
    filterTradeReviewItems(items, {
      strategyId: 'trend_breakout',
      mistakeTag: 'early_exit',
      pnlResult: 'profit',
    }),
    [items[0]],
  )
})

test('keeps unreviewed and orphaned rows only when filters allow them', () => {
  const unreviewed = item({ review: null })
  const orphaned = item({ trade: null, attribution: null })

  assert.equal(filterTradeReviewItems([unreviewed, orphaned], {
    strategyId: '', mistakeTag: '', pnlResult: 'all',
  }).length, 2)
  assert.equal(filterTradeReviewItems([unreviewed, orphaned], {
    strategyId: 'trend_breakout', mistakeTag: '', pnlResult: 'all',
  }).length, 1)
  assert.equal(filterTradeReviewItems([unreviewed, orphaned], {
    strategyId: '', mistakeTag: '', pnlResult: 'planned',
  }).length, 0)
})

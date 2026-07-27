import test from 'node:test'
import assert from 'node:assert/strict'

import type { DailyBriefingSources } from './api.ts'
import { buildDailyBriefing } from './briefing.ts'

const now = new Date('2026-07-27T10:00:00+08:00')

function sources(): DailyBriefingSources {
  return {
    portfolio: {
      trades: [],
      positions: [{ symbol: '600519.SH', name: '贵州茅台', quantity: 100, average_cost: 1400, cost_value: 140000, realized_pnl: 0, first_trade_date: '2026-01-01', last_trade_date: '2026-01-01' }],
      summary: { position_count: 1, trade_count: 1, cost_value: 140000, realized_pnl: 1200 },
    },
    quotes: {
      '600519.SH': { symbol: '600519.SH', name: '贵州茅台', price: 1500, change_pct: 1.5, date: '2026-07-27', is_live: true },
    },
    watchlist: [{ symbol: '000001.SZ', name: '平安银行' }],
    alerts: [
      { ts: now.getTime() - 60_000, source: 'strategy', type: 'sell_signal', symbol: '600519.sh', name: '贵州茅台', message: '触发离场', severity: 'warn' },
      { ts: now.getTime() - 120_000, source: 'price', type: 'price', symbol: '000001.SZ', message: '突破价格' },
      { ts: now.getTime() - 180_000, source: 'price', type: 'price', symbol: '300750.SZ', message: '无关提醒' },
    ],
    overview: {
      as_of: '2026-07-27',
      breadth: { total: 5000, up: 3200, down: 1700, flat: 100, up_pct: 64, down_pct: 34 },
      limit: { limit_up: 52, broken: 11, limit_down: 3 },
      emotion: { score: 68, label: '偏强' },
      concept_rank: { leading: [{ name: '机器人', count: 20, avg_pct: 3.2 }] },
      industry_rank: { leading: [{ name: '电子', count: 80, avg_pct: 2.1 }] },
    },
    recaps: [{ id: 'r1', as_of: '2026-07-27', summary: '市场放量走强。', created_at: '2026-07-27T08:00:00Z' }],
    tracks: [{
      id: 't1', strategy_id: 'trend', strategy_name: '趋势策略', symbols: ['600519.SH'], start_date: '2026-01-01', initial_capital: 1000000, max_positions: 10, commission_pct: 0.0002, stamp_tax_pct: 0.001, slippage_bps: 5, params: {}, overrides: {}, note: '', status: 'tracking', observations: [], created_at: '', updated_at: '',
    }],
    research: [{
      id: 'e1', title: '白酒需求验证', subject_type: 'stock', subject: '600519.SH', thesis: '', evidence: [], counter_evidence: [], invalidation: '', plan: '检查渠道库存', status: 'tracking', tags: [], created_at: '2026-07-20T00:00:00Z', updated_at: '2026-07-20T00:00:00Z', origin: 'manual', captures: [],
    }],
    unavailable: [],
  }
}

test('builds a personalized briefing from holdings, watchlist and public outputs', () => {
  const briefing = buildDailyBriefing(sources(), 'morning', now)

  assert.equal(briefing.asOf, '2026-07-27')
  assert.equal(briefing.portfolio.marketValue, 150000)
  assert.equal(briefing.portfolio.unrealizedPnl, 10000)
  assert.equal(briefing.portfolio.floatingReturn, 10000 / 140000)
  assert.deepEqual(briefing.alerts.map(alert => [alert.symbol, alert.scope]), [
    ['600519.sh', 'holding'],
    ['000001.SZ', 'watchlist'],
  ])
  assert.equal(briefing.staleTrackCount, 1)
  assert.equal(briefing.openResearchCount, 1)
  assert.ok(briefing.focus.some(item => item.id === 'stale-tracks'))
  assert.ok(briefing.focus.some(item => item.id === 'research-e1'))
})

test('uses a same-day alert window for the evening report', () => {
  const input = sources()
  const previousEvening = new Date('2026-07-26T22:00:00+08:00').getTime()
  input.alerts.unshift({ ts: previousEvening, source: 'price', type: 'price', symbol: '600519.SH', message: '昨晚提醒' })

  const evening = buildDailyBriefing(input, 'evening', now)
  const morning = buildDailyBriefing(input, 'morning', now)

  assert.equal(evening.alerts.some(alert => alert.message === '昨晚提醒'), false)
  assert.equal(morning.alerts.some(alert => alert.message === '昨晚提醒'), true)
})

test('degrades explicitly when sources or portfolio quotes are unavailable', () => {
  const input = sources()
  input.quotes = {}
  input.unavailable = ['市场概览']
  input.overview = null

  const briefing = buildDailyBriefing(input, 'morning', now)

  assert.equal(briefing.portfolio.unpricedCount, 1)
  assert.deepEqual(briefing.unavailable, ['市场概览'])
  assert.ok(briefing.focus.some(item => item.id === 'unpriced-positions'))
})

test('does not present an older recap as the current market summary', () => {
  const input = sources()
  input.recaps[0].as_of = '2026-07-24'

  const briefing = buildDailyBriefing(input, 'morning', now)

  assert.equal(briefing.asOf, '2026-07-27')
  assert.equal(briefing.market.recapSummary, null)
})

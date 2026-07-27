import test from 'node:test'
import assert from 'node:assert/strict'

import { dailyBriefingFilename, dailyBriefingMarkdown } from './briefingMarkdown.ts'
import type { DailyBriefing } from './briefing.ts'

const briefing = {
  mode: 'morning',
  generatedAt: '2026-07-27T02:00:00.000Z',
  asOf: '2026-07-27',
  unavailable: ['市场复盘'],
  market: { label: '偏强', score: 68, up: 3200, down: 1700, flat: 100, upPct: 64, limitUp: 52, limitDown: 3, broken: 11, leaders: [{ name: '机器人', avgPct: 3.2, kind: '概念' }], recapSummary: null },
  portfolio: { positions: [{ symbol: '600519.SH', name: '贵州|茅台', quantity: 100, currentPrice: 1500, marketValue: 150000, unrealizedPnl: 10000, returnPct: 0.071428, dailyChangePct: 1.5, quoteDate: '2026-07-27', isLive: true }], marketValue: 150000, unrealizedPnl: 10000, floatingReturn: 0.071428, realizedPnl: 0, unpricedCount: 0, liveCount: 1 },
  alerts: [],
  alertCounts: { holding: 0, watchlist: 0 },
  tracks: [],
  staleTrackCount: 0,
  research: [],
  openResearchCount: 0,
  watchlistCount: 1,
  focus: [{ id: 'f1', tone: 'neutral', title: '检查持仓', detail: '确认止损\n位置' }],
} satisfies DailyBriefing

test('exports a readable markdown briefing with escaped table content', () => {
  const markdown = dailyBriefingMarkdown(briefing)

  assert.match(markdown, /^# Sycee 晨报 · 2026-07-27/)
  assert.match(markdown, /贵州\\\|茅台/)
  assert.match(markdown, /确认止损 位置/)
  assert.match(markdown, /本次未能读取：市场复盘/)
  assert.equal(dailyBriefingFilename(briefing), 'sycee-morning-2026-07-27.md')
})

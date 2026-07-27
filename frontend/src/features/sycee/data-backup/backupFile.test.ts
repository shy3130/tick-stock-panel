import test from 'node:test'
import assert from 'node:assert/strict'

import { backupFilename, backupSummary, parseBackupFile } from './backupFile.ts'

const document = {
  format: 'sycee-user-data' as const,
  version: 1 as const,
  exported_at: '2026-07-27T08:30:45+00:00',
  data: {
    portfolio: { version: 1 as const, trades: [{ id: 'trade_1' }] },
    portfolio_sell_alert: { version: 1 as const, config: { enabled: false } },
    trade_reviews: { version: 1 as const, reviews: [{ id: 'review_1' }] },
    research_ledger: { version: 1 as const, entries: [{ id: 'research_1' }] },
    strategy_tracking: { version: 1 as const, tracks: [{ id: 'strategy_track_1' }] },
  },
}

test('parses a complete Sycee backup and summarizes its records', () => {
  const parsed = parseBackupFile(JSON.stringify(document))

  assert.deepEqual(backupSummary(parsed), {
    trades: 1,
    reviews: 1,
    researchEntries: 1,
    strategyTracks: 1,
    sellAlertSaved: true,
  })
})

test('rejects unrelated or incomplete JSON files', () => {
  assert.throws(() => parseBackupFile('{broken'), /有效 JSON/)
  assert.throws(() => parseBackupFile(JSON.stringify({ ...document, format: 'other' })), /受支持/)
  assert.throws(() => parseBackupFile(JSON.stringify({
    ...document,
    data: { ...document.data, trade_reviews: {} },
  })), /内容不完整/)
})

test('builds a stable filename from the UTC export time', () => {
  assert.equal(backupFilename(document.exported_at), 'sycee-data-20260727T083045Z.json')
})

test('accepts an older version 1 backup without strategy tracking data', () => {
  const legacy = JSON.parse(JSON.stringify(document))
  delete legacy.data.strategy_tracking

  assert.equal(backupSummary(parseBackupFile(JSON.stringify(legacy))).strategyTracks, 0)
})

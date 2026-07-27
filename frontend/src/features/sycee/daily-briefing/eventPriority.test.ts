import test from 'node:test'
import assert from 'node:assert/strict'

import { eventDirection, prioritizeEvents, scoreEvent, type ScopedBriefingAlert } from './eventPriority.ts'

const now = new Date('2026-07-27T10:00:00+08:00')

function alert(changes: Partial<ScopedBriefingAlert> = {}): ScopedBriefingAlert {
  return {
    ts: now.getTime() - 30 * 60 * 1000,
    rule_id: 'rule-1',
    source: 'strategy',
    type: 'sell_signal',
    symbol: '600519.SH',
    name: '贵州茅台',
    message: '触发离场',
    severity: 'warn',
    scope: 'holding',
    conditions: [{ field: 'close', op: '<', value: 1400 }],
    signals: ['signal_ma20_breakdown'],
    ...changes,
  }
}

test('scores a recent held-position exit with transparent contributions', () => {
  const result = scoreEvent(alert(), now)

  assert.equal(result.direction, 'risk')
  assert.equal(result.score, 87)
  assert.deepEqual(result.reasons.map(item => [item.key, item.points]), [
    ['scope', 35],
    ['severity', 15],
    ['direction', 20],
    ['conditions', 4],
    ['signals', 3],
    ['recency', 10],
  ])
})

test('keeps opportunity and risk evidence in separate groups', () => {
  const groups = prioritizeEvents([
    alert(),
    alert({ rule_id: 'rule-2', source: 'price', message: '跌破止损位' }),
    alert({ rule_id: 'rule-3', type: 'buy_signal', message: '重新进入' }),
  ], now)

  assert.equal(groups.length, 2)
  assert.equal(groups[0].direction, 'risk')
  assert.equal(groups[0].evidence.length, 2)
  assert.equal(groups[0].score, 93)
  assert.ok(groups[0].reasons.some(item => item.key === 'confirmation' && item.points === 6))
  assert.equal(groups[1].direction, 'opportunity')
})

test('does not count repeated events from the same rule as independent confirmation', () => {
  const groups = prioritizeEvents([
    alert(),
    alert({ ts: now.getTime() - 60 * 60 * 1000 }),
  ], now)

  assert.equal(groups[0].evidence.length, 2)
  assert.equal(groups[0].score, 87)
  assert.equal(groups[0].reasons.some(item => item.key === 'confirmation'), false)
})

test('classifies known strategy lifecycle event types without parsing message text', () => {
  assert.equal(eventDirection('pool_exit'), 'risk')
  assert.equal(eventDirection('new_entry'), 'opportunity')
  assert.equal(eventDirection('price'), 'observe')
})

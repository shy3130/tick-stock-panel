import test from 'node:test'
import assert from 'node:assert/strict'

import type {
  ExistingMonitorRule,
  PortfolioSellAlertRule,
  PortfolioSellAlertStatus,
} from './api.ts'
import { planSellAlertReconciliation } from './sellAlertRules.ts'

const desiredRule: PortfolioSellAlertRule = {
  id: 'sycee_pf_sell_1234',
  name: '持仓卖出提醒',
  enabled: true,
  type: 'strategy',
  asset_type: 'stock',
  scope: 'symbols',
  symbols: ['000001.SZ', '600519.SH'],
  sector: null,
  strategy_id: 'trend_breakout',
  direction: 'exit',
  notify_events: ['sell_signal'],
  conditions: [],
  logic: 'and',
  cooldown_seconds: 3600,
  severity: 'warn',
  webhook_channels: ['feishu'],
  message: '',
}

function status(rule: PortfolioSellAlertRule | null): PortfolioSellAlertStatus {
  return {
    config: {
      enabled: rule !== null,
      strategy_id: 'trend_breakout',
      webhook_channels: ['feishu'],
      rule_id: desiredRule.id,
    },
    state: rule ? 'ready' : 'disabled',
    position_count: rule?.symbols.length ?? 0,
    symbols: rule?.symbols ?? [],
    desired_rule: rule,
  }
}

function savedRule(overrides: Partial<ExistingMonitorRule> = {}): ExistingMonitorRule {
  return {
    ...desiredRule,
    created_at: '2026-07-27T00:00:00Z',
    ...overrides,
  }
}

test('creates the managed rule without changing unrelated user rules', () => {
  const userRule = savedRule({ id: 'my_manual_rule', name: '我的手工规则' })
  const action = planSellAlertReconciliation(status(desiredRule), [userRule])

  assert.deepEqual(action, { type: 'upsert', rule: desiredRule })
  assert.equal(userRule.name, '我的手工规则')
})

test('accepts a normalized server rule with extra fields as synchronized', () => {
  assert.deepEqual(
    planSellAlertReconciliation(status(desiredRule), [savedRule()]),
    { type: 'none' },
  )
})

test('updates only the managed rule when portfolio symbols change', () => {
  const stale = savedRule({ symbols: ['600519.SH'] })
  assert.deepEqual(
    planSellAlertReconciliation(status(desiredRule), [stale]),
    { type: 'upsert', rule: desiredRule },
  )
})

test('deletes only the owned rule when the alert is disabled or has no positions', () => {
  const userRule = savedRule({ id: 'my_manual_rule' })
  assert.deepEqual(
    planSellAlertReconciliation(status(null), [userRule, savedRule()]),
    { type: 'delete', ruleId: desiredRule.id },
  )
  assert.deepEqual(
    planSellAlertReconciliation(status(null), [userRule]),
    { type: 'none' },
  )
})

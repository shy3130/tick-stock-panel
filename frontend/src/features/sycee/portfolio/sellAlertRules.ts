import type {
  ExistingMonitorRule,
  PortfolioSellAlertRule,
  PortfolioSellAlertStatus,
} from './api.ts'

export type SellAlertReconcileAction =
  | { type: 'none' }
  | { type: 'upsert'; rule: PortfolioSellAlertRule }
  | { type: 'delete'; ruleId: string }

const MANAGED_FIELDS = [
  'id',
  'name',
  'enabled',
  'type',
  'asset_type',
  'scope',
  'symbols',
  'sector',
  'strategy_id',
  'direction',
  'notify_events',
  'conditions',
  'logic',
  'cooldown_seconds',
  'severity',
  'webhook_channels',
  'message',
] as const

function managedSnapshot(rule: ExistingMonitorRule | PortfolioSellAlertRule): Record<string, unknown> {
  return Object.fromEntries(MANAGED_FIELDS.map(field => [field, rule[field]]))
}

export function planSellAlertReconciliation(
  status: PortfolioSellAlertStatus,
  rules: ExistingMonitorRule[],
): SellAlertReconcileAction {
  const ruleId = status.config.rule_id
  const existing = ruleId ? rules.find(rule => rule.id === ruleId) : undefined
  if (!status.desired_rule) {
    return existing ? { type: 'delete', ruleId } : { type: 'none' }
  }
  if (!existing) return { type: 'upsert', rule: status.desired_rule }
  return JSON.stringify(managedSnapshot(existing)) === JSON.stringify(managedSnapshot(status.desired_rule))
    ? { type: 'none' }
    : { type: 'upsert', rule: status.desired_rule }
}

export function sellAlertActionKey(action: SellAlertReconcileAction): string {
  return JSON.stringify(action)
}

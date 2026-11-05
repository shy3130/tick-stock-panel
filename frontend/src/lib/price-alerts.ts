import type { MonitorRule } from '@/lib/api'
import { fmtAssetPrice } from '@/lib/format'

export type PriceAlertDirection = 'up' | 'down'

export interface PointPriceAlert {
  rule: MonitorRule
  direction: PriceAlertDirection
  target: number
}

export interface MonitorPriceLine {
  value: number
  label: string
  color: string
}

export function parsePointPriceAlert(rule: MonitorRule, symbol?: string): PointPriceAlert | null {
  if (
    rule.type !== 'price'
    || rule.scope !== 'symbols'
    || rule.symbols.length !== 1
    || (symbol != null && rule.symbols[0] !== symbol)
    || rule.conditions.length !== 1
  ) return null

  const condition = rule.conditions[0]
  if (condition.field !== 'close' || !['>=', '<='].includes(condition.op)) return null
  if (typeof condition.value !== 'number' || !Number.isFinite(condition.value) || condition.value <= 0) return null

  return {
    rule,
    direction: condition.op === '>=' ? 'up' : 'down',
    target: condition.value,
  }
}

export function inferPriceAlertDirection(
  target: number,
  currentPrice: number | null | undefined,
): PriceAlertDirection {
  return currentPrice != null && Number.isFinite(currentPrice) && target < currentPrice ? 'down' : 'up'
}

export function buildPriceAlertMessage(
  name: string,
  symbol: string,
  direction: PriceAlertDirection,
  target: number,
  assetType?: string,
): string {
  return `${name || symbol}股价${direction === 'up' ? '上穿' : '下穿'} ${fmtAssetPrice(target, assetType)}`
}

export function buildMonitorPriceLines(rules: MonitorRule[], symbol: string): MonitorPriceLine[] {
  return rules.flatMap(rule => {
    if (!rule.enabled) return []
    const alert = parsePointPriceAlert(rule, symbol)
    if (!alert) return []
    const isUp = alert.direction === 'up'
    return [{
      value: alert.target,
      label: `${isUp ? '上穿' : '下穿'} ${fmtAssetPrice(alert.target, rule.asset_type)}`,
      color: isUp ? '#C74040' : '#2D9B65',
    }]
  })
}

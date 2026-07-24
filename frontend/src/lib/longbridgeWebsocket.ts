const PRIORITY_LABELS: Record<string, string> = {
  holdings_and_traded: '持仓及历史交易股',
  industry_leaders: '三市场行业龙头',
  strategy_candidates: '动态策略候选池',
}

const SUBSCRIPTION_TYPE_LABELS: Record<string, string> = {
  quote: '实时报价',
  depth: '买卖盘口',
  trades: '逐笔成交',
  brokers: '港股经纪队列',
  candlestick_1m: '1分钟K线',
}

export function isLongbridgeWebsocketProvider(provider?: string): boolean {
  return provider?.trim().toLowerCase() === 'clickhouse'
}

export function longbridgePriorityLabels(values: string[]): string[] {
  return values.map(value => PRIORITY_LABELS[value] ?? value)
}

export function longbridgeSubscriptionTypeLabels(values: string[]): string[] {
  return values.map(value => SUBSCRIPTION_TYPE_LABELS[value] ?? value)
}

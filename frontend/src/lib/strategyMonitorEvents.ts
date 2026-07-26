import type { StrategyNotifyEvent } from './api'

export interface StrategyEventMeta {
  label: string
  action: string
  className: string
}

const META: Record<string, StrategyEventMeta> = {
  buy_signal: {
    label: '买入',
    action: '买入信号',
    className: 'text-danger',
  },
  sell_signal: {
    label: '卖出',
    action: '卖出信号',
    className: 'text-bear',
  },
  pool_entry: {
    label: '进入',
    action: '进入选股结果',
    className: 'text-emerald-400',
  },
  pool_exit: {
    label: '移出',
    action: '移出选股结果',
    className: 'text-warning',
  },
  new_entry: {
    label: '进入',
    action: '进入选股结果',
    className: 'text-emerald-400',
  },
  dropped: {
    label: '移出',
    action: '移出选股结果',
    className: 'text-warning',
  },
}

export const DEFAULT_STRATEGY_NOTIFY_EVENTS: StrategyNotifyEvent[] = [
  'buy_signal',
  'sell_signal',
]

export const LEGACY_STRATEGY_NOTIFY_EVENTS: StrategyNotifyEvent[] = [
  'pool_entry',
  'pool_exit',
]

export const STRATEGY_NOTIFY_EVENT_OPTIONS: {
  key: StrategyNotifyEvent
  label: string
  group: 'signal' | 'pool'
}[] = [
  { key: 'buy_signal', label: '买入信号', group: 'signal' },
  { key: 'sell_signal', label: '卖出信号', group: 'signal' },
  { key: 'pool_entry', label: '进入选股结果', group: 'pool' },
  { key: 'pool_exit', label: '移出选股结果', group: 'pool' },
]

export function strategyEventMeta(type: string): StrategyEventMeta {
  return META[type] ?? {
    label: '事件',
    action: '策略事件',
    className: 'text-secondary',
  }
}

export function strategyName(message: string): string {
  return message.match(/策略「([^」]+)」/)?.[1] ?? ''
}

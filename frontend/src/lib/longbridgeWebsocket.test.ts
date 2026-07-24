import { describe, expect, it } from 'vitest'

import {
  isLongbridgeWebsocketProvider,
  longbridgePriorityLabels,
  longbridgeSubscriptionTypeLabels,
} from './longbridgeWebsocket'

describe('Longbridge WebSocket settings model', () => {
  it('switches the monitoring UI away from legacy polling for ClickHouse realtime', () => {
    expect(isLongbridgeWebsocketProvider('clickhouse')).toBe(true)
    expect(isLongbridgeWebsocketProvider('tickflow')).toBe(false)
    expect(isLongbridgeWebsocketProvider(undefined)).toBe(false)
  })

  it('uses Chinese labels for the deployed subscription policy', () => {
    expect(longbridgePriorityLabels([
      'holdings_and_traded',
      'industry_leaders',
      'strategy_candidates',
    ])).toEqual(['持仓及历史交易股', '三市场行业龙头', '动态策略候选池'])
    expect(longbridgeSubscriptionTypeLabels([
      'quote',
      'depth',
      'trades',
      'brokers',
      'candlestick_1m',
    ])).toEqual(['实时报价', '买卖盘口', '逐笔成交', '港股经纪队列', '1分钟K线'])
  })
})

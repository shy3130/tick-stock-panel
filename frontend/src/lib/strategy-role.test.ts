import { describe, expect, it } from 'vitest'

import { isBacktestableStrategy, strategyRoleLabel } from './strategy-role'

describe('strategy roles', () => {
  it('labels risk and excludes it from buy-entry backtests', () => {
    expect(strategyRoleLabel('risk')).toBe('风险')
    expect(isBacktestableStrategy({ strategy_role: 'risk' })).toBe(false)
  })

  it('keeps buy and early-buy strategies backtestable', () => {
    expect(isBacktestableStrategy({ strategy_role: 'buy' })).toBe(true)
    expect(isBacktestableStrategy({ strategy_role: 'early_buy' })).toBe(true)
  })
})

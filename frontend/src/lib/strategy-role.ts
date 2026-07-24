export type StrategyRole = 'buy' | 'early_buy' | 'risk'

export const strategyRoleLabel = (role: StrategyRole) =>
  ({ buy: '买点', early_buy: '提前买点', risk: '风险' } as const)[role]

export const isBacktestableStrategy = (strategy: { strategy_role?: StrategyRole }) =>
  strategy.strategy_role !== 'risk'

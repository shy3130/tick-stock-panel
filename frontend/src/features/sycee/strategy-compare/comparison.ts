import type { ComparableStrategy, StrategyBacktestResult } from './api'

export const MIN_COMPARISON_STRATEGIES = 2
export const MAX_COMPARISON_STRATEGIES = 5

export interface ComparisonSettings {
  symbols: string[]
  start: string
  end: string
  initialCapital: number
  maxPositions: number
  commissionPct: number
  stampTaxPct: number
  slippageBps: number
}

export interface ComparisonResult {
  strategyId: string
  strategyName: string
  result: StrategyBacktestResult
}

export interface ComparisonMetricRow {
  strategyId: string
  strategyName: string
  totalReturn: number | null
  annualReturn: number | null
  sharpe: number | null
  maxDrawdown: number | null
  winRate: number | null
  tradeCount: number | null
  elapsedMs: number
}

export interface NormalizedCurve {
  strategyId: string
  strategyName: string
  points: { date: string; value: number }[]
}

const toSignalId = (signal: string) => (
  signal.startsWith('signal_') || signal.startsWith('csg_') ? signal : `signal_${signal}`
)

export function buildStrategyDefaults(strategy: ComparableStrategy): {
  params: Record<string, unknown>
  overrides: Record<string, unknown>
} {
  const params = { ...strategy.params_defaults }
  for (const definition of strategy.params) {
    if (!(definition.id in params)) params[definition.id] = definition.default
  }

  const overrides: Record<string, unknown> = {
    basic_filter: { ...strategy.basic_filter },
    entry_signals: strategy.entry_signals.map(toSignalId),
    exit_signals: strategy.exit_signals.map(toSignalId),
    scoring: { ...strategy.scoring },
    stop_loss: strategy.stop_loss,
    take_profit: strategy.take_profit,
    trailing_stop: strategy.trailing_stop,
    trailing_take_profit_activate: strategy.trailing_take_profit_activate,
    trailing_take_profit_drawdown: strategy.trailing_take_profit_drawdown,
    score_min: null,
    score_max: null,
    max_hold_days: strategy.max_hold_days,
  }
  if (strategy.execution_backend === 'matrix_native') {
    delete overrides.entry_signals
    delete overrides.exit_signals
  }
  return { params, overrides }
}

function finiteStat(stats: Record<string, unknown>, ...keys: string[]): number | null {
  for (const key of keys) {
    const value = stats[key]
    if (typeof value === 'number' && Number.isFinite(value)) return value
  }
  return null
}

export function buildMetricRow(item: ComparisonResult): ComparisonMetricRow {
  const stats = item.result.stats
  return {
    strategyId: item.strategyId,
    strategyName: item.strategyName,
    totalReturn: finiteStat(stats, 'total_return'),
    annualReturn: finiteStat(stats, 'annual_return'),
    sharpe: finiteStat(stats, 'sharpe'),
    maxDrawdown: finiteStat(stats, 'max_drawdown'),
    winRate: finiteStat(stats, 'win_rate'),
    tradeCount: finiteStat(stats, 'n_trades', 'trade_count'),
    elapsedMs: item.result.elapsed_ms,
  }
}

export function normalizeEquityCurve(item: ComparisonResult): NormalizedCurve {
  const usable = item.result.equity_curve.filter(point => (
    point.date && Number.isFinite(point.value) && point.value > 0
  ))
  const baseline = usable[0]?.value
  return {
    strategyId: item.strategyId,
    strategyName: item.strategyName,
    points: baseline == null ? [] : usable.map(point => ({
      date: point.date.slice(0, 10),
      value: point.value / baseline,
    })),
  }
}

export function validateComparison(
  strategyIds: string[],
  settings: ComparisonSettings,
): string | null {
  if (strategyIds.length < MIN_COMPARISON_STRATEGIES) return '请至少选择 2 个策略'
  if (strategyIds.length > MAX_COMPARISON_STRATEGIES) return '最多同时比较 5 个策略'
  if (settings.symbols.length === 0) return '股票池不能为空'
  if (!settings.start || !settings.end) return '请选择完整的回测区间'
  if (settings.start > settings.end) return '开始日期不能晚于结束日期'
  if (!Number.isFinite(settings.initialCapital) || settings.initialCapital <= 0) return '初始资金必须大于 0'
  if (!Number.isInteger(settings.maxPositions) || settings.maxPositions <= 0) return '最大持仓数必须是正整数'
  if (!Number.isFinite(settings.commissionPct) || settings.commissionPct < 0) return '佣金不能小于 0'
  if (!Number.isFinite(settings.stampTaxPct) || settings.stampTaxPct < 0) return '印花税不能小于 0'
  if (!Number.isFinite(settings.slippageBps) || settings.slippageBps < 0) return '滑点不能小于 0'
  return null
}

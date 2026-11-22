import type {
  FactorBacktestResult,
  FactorBatchItem,
  FactorBatchResult,
  ResearchCandidateCreate,
  StrategyBacktestResult,
} from '@/lib/api'

const FACTOR_CONFIG_FIELDS = [
  'symbols', 'start', 'end', 'n_groups', 'rebalance', 'weight', 'fees_pct',
  'slippage_bps', 'asset_type',
] as const

const STRATEGY_CONFIG_FIELDS = [
  'strategy_id', 'symbols', 'start', 'end', 'params', 'overrides', 'matching',
  'entry_fill', 'exit_fill', 'fees_pct', 'commission_pct', 'stamp_tax_pct',
  'slippage_bps', 'max_positions', 'max_exposure_pct', 'initial_capital',
  'position_sizing', 'mode', 'holding_days', 'asset_type', 'minute_fill',
  'regime_filter', 'price_basis', 'entry_filters', 'filter_data_version',
] as const

function pickConfig(source: Record<string, any>, fields: readonly string[]) {
  const result: Record<string, unknown> = {}
  for (const field of fields) {
    if (source[field] !== undefined) result[field] = source[field]
  }
  return result
}

export function factorResultCandidate(
  result: FactorBacktestResult,
  label: string,
): ResearchCandidateCreate {
  const factorName = String(result.config.factor_name)
  return {
    kind: 'factor',
    name: `${label}候选`,
    source_id: factorName,
    config: {
      factor_name: factorName,
      ...pickConfig(result.config, FACTOR_CONFIG_FIELDS),
    },
    metrics: {
      ic_mean: result.ic_mean,
      ic_std: result.ic_std,
      ir: result.ir,
      ic_win_rate: result.ic_win_rate,
      long_short_return: result.long_short_stats?.total_return ?? null,
      long_short_max_drawdown: result.long_short_stats?.max_drawdown ?? null,
      n_symbols: result.n_symbols,
      n_dates: result.n_dates,
      elapsed_ms: result.elapsed_ms,
    },
    data_as_of: String(result.config.end || '') || null,
  }
}

export function factorBatchCandidate(
  batch: FactorBatchResult,
  item: FactorBatchItem,
): ResearchCandidateCreate {
  return {
    kind: 'factor',
    name: `${item.label}候选`,
    source_id: item.factor_name,
    config: {
      factor_name: item.factor_name,
      ...pickConfig(batch.config, FACTOR_CONFIG_FIELDS),
    },
    metrics: {
      ic_mean: item.ic_mean,
      ir: item.ir,
      ic_win_rate: item.ic_win_rate,
      long_short_return: item.long_short_return,
      long_short_max_drawdown: item.long_short_max_drawdown,
      n_symbols: item.n_symbols,
      n_dates: item.n_dates,
      elapsed_ms: item.elapsed_ms,
    },
    data_as_of: String(batch.config.end || '') || null,
  }
}

export function strategyResultCandidate(result: StrategyBacktestResult): ResearchCandidateCreate {
  const stats = result.stats ?? {}
  const sourceId = String(result.config.strategy_id || result.strategy_info?.id || '')
  return {
    kind: 'strategy',
    name: `${result.strategy_info?.name || sourceId}候选`,
    source_id: sourceId,
    config: pickConfig({ ...result.config, strategy_id: sourceId }, STRATEGY_CONFIG_FIELDS),
    metrics: {
      total_return: stats.total_return ?? null,
      annual_return: stats.annual_return ?? null,
      max_drawdown: stats.max_drawdown ?? null,
      sharpe: stats.sharpe ?? null,
      sortino: stats.sortino ?? null,
      win_rate: stats.win_rate ?? null,
      n_trades: stats.n_trades ?? null,
      profit_factor: stats.profit_factor ?? null,
      avg_return: stats.avg_return ?? null,
      median_return: stats.median_return ?? null,
      elapsed_ms: result.elapsed_ms,
    },
    data_as_of: String(result.config.end || '') || null,
  }
}

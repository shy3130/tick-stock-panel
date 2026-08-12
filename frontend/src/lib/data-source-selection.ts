export interface ProviderPreferencePatch {
  daily_data_provider?: string
  adj_factor_provider?: string
  realtime_data_provider?: string
  minute_data_provider?: string
  financial_data_provider?: string
}

const ROUTED_DATASETS = {
  realtime: 'realtime_data_provider',
  minute: 'minute_data_provider',
  financial: 'financial_data_provider',
} as const

const PROVIDER_STATUS_DATASETS = [
  ['instruments', '证券主表'],
  ['daily', '日K'],
  ['adj_factor', '复权因子'],
  ['financial', '财务'],
  ['realtime', '实时'],
  ['minute', '分钟'],
] as const

export function buildProviderStatusIndicators(datasets: string[]) {
  const supportedDatasets = new Set(datasets)
  return PROVIDER_STATUS_DATASETS.map(([dataset, label]) => ({
    dataset,
    label,
    supported: supportedDatasets.has(dataset),
  }))
}

export function resolveInitialProviderSelection({
  current,
  active,
  preferencesLoaded,
  initialized,
}: {
  current: string
  active: string
  preferencesLoaded: boolean
  initialized: boolean
}) {
  return preferencesLoaded && !initialized ? active : current
}

export function buildProviderPreferencePatch(
  provider: string,
  datasets: string[],
): ProviderPreferencePatch {
  if (provider === 'tickflow') {
    return {
      daily_data_provider: 'tickflow',
      adj_factor_provider: 'same_as_daily',
      realtime_data_provider: 'tickflow',
      minute_data_provider: 'tickflow',
      financial_data_provider: 'tickflow',
    }
  }

  const supported = new Set(datasets)
  const patch: ProviderPreferencePatch = {}

  if (supported.has('daily')) {
    patch.daily_data_provider = provider
  }
  if (supported.has('adj_factor')) {
    patch.adj_factor_provider = supported.has('daily') ? 'same_as_daily' : provider
  }
  for (const [dataset, preference] of Object.entries(ROUTED_DATASETS)) {
    if (supported.has(dataset)) {
      patch[preference] = provider
    }
  }

  return patch
}

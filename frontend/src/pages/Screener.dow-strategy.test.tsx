import { render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

vi.mock('@tanstack/react-query', () => ({
  useQuery: () => ({
    data: undefined,
    isError: false,
    isLoading: false,
    isPending: false,
    isSuccess: false,
    refetch: vi.fn(),
  }),
  useMutation: () => ({
    data: undefined,
    isError: false,
    isPending: false,
    mutate: vi.fn(),
  }),
  useQueryClient: () => ({
    fetchQuery: vi.fn(),
    invalidateQueries: vi.fn(),
  }),
}))

vi.mock('@/lib/useSharedQueries', () => ({
  useDataStatus: () => ({ data: { enriched: { earliest_date: '', latest_date: '' } } }),
  usePreferences: () => ({ data: { screener_auto_run: false } }),
  useCapabilities: () => ({ data: { capabilities: {}, label: '' } }),
  useQuoteStatus: () => ({ data: { running: false } }),
}))

vi.mock('@/lib/useSharedMutations', () => ({
  useWatchlistBatchAdd: () => ({ isPending: false, mutate: vi.fn() }),
}))

vi.mock('@/lib/useStrategyPool', () => ({
  useStrategyPool: () => ({
    pool: [],
    addToPool: vi.fn(),
    removeFromPool: vi.fn(),
    reorderPool: vi.fn(),
    prune: vi.fn(),
  }),
}))

vi.mock('@/lib/market-scope', () => ({
  useMarketScope: () => ({ market: 'hk', setMarket: vi.fn() }),
}))

vi.mock('@/components/PageHeader', () => ({
  PageHeader: ({ title, right }: { title: string; right?: React.ReactNode }) => (
    <header><h1>{title}</h1>{right}</header>
  ),
}))

vi.mock('@/components/screener/StrategyCard', () => ({
  StrategyCard: () => null,
  loadCardSize: () => 'normal',
  cardWrapCls: () => '',
}))

vi.mock('@/components/screener/ScreenerTable', () => ({ ScreenerTable: () => null }))
vi.mock('@/components/screener/StrategySettingsDialog', () => ({ StrategySettingsDialog: () => null }))
vi.mock('@/components/screener/StrategyPoolDialog', () => ({ StrategyPoolDialog: () => null }))
vi.mock('@/components/screener/StrategyBuilderDialog', () => ({ StrategyBuilderDialog: () => null }))
vi.mock('@/components/screener/StrategyStoreDialog', () => ({ StrategyStoreDialog: () => null }))
vi.mock('@/components/ListColumnCustomizer', () => ({ ListColumnCustomizer: () => null }))

import { Screener } from './Screener'

describe('Screener Dow strategy integration', () => {
  it('keeps the multi-timeframe Dow strategy card visible for the HK stock market', () => {
    render(<Screener />)

    expect(screen.getByText('道氏趋势 · 多周期')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '执行选股' })).toBeInTheDocument()
  })
})

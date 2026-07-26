import { useQueries, useQuery } from '@tanstack/react-query'
import type {
  CollectionGapPage,
  CollectionMonitorFilters,
  CollectionMonitorOverview,
  CollectionTaskPage,
  MarketCollectionEvidence,
  MarketKey,
} from './types'

const API_ROOT = '/api/collection-monitor'
const REFRESH_INTERVAL_MS = 30_000
const MARKETS: readonly MarketKey[] = ['cn', 'hk', 'us']

export class CollectionMonitorRequestError extends Error {
  constructor(readonly status: number) {
    super(status === 503 ? 'collection_monitoring_evidence_unavailable' : 'collection_monitor_request_failed')
  }
}

function queryString(values: Record<string, string | number | undefined>) {
  const query = new URLSearchParams()
  for (const [key, value] of Object.entries(values)) {
    if (value !== undefined && value !== '') query.set(key, String(value))
  }
  return query.toString()
}

async function readJson<T>(url: string): Promise<T> {
  const response = await fetch(url, { headers: { Accept: 'application/json' } })
  if (!response.ok) throw new CollectionMonitorRequestError(response.status)
  return response.json() as Promise<T>
}

const queryOptions = {
  refetchInterval: REFRESH_INTERVAL_MS,
  refetchIntervalInBackground: true,
  retry: false,
} as const

export function useCollectionMonitor(filters: CollectionMonitorFilters) {
  const overview = useQuery({
    queryKey: ['collection-monitor', 'overview', filters.date],
    queryFn: () => readJson<CollectionMonitorOverview>(
      `${API_ROOT}/overview?${queryString({ date: filters.date })}`,
    ),
    ...queryOptions,
  })

  const markets = useQueries({
    queries: MARKETS.map(market => ({
      queryKey: ['collection-monitor', 'market', market, filters.date],
      queryFn: () => readJson<MarketCollectionEvidence>(
        `${API_ROOT}/markets/${market}?${queryString({ date: filters.date })}`,
      ),
      ...queryOptions,
    })),
  })

  const tasks = useQuery({
    queryKey: [
      'collection-monitor',
      'tasks',
      filters.date,
      filters.status,
      filters.technology,
      filters.market,
      filters.dataset,
      filters.mode,
    ],
    queryFn: () => readJson<CollectionTaskPage>(
      `${API_ROOT}/tasks?${queryString({
        date: filters.date,
        status: filters.status,
        technology: filters.technology,
        market: filters.market,
        dataset: filters.dataset,
        mode: filters.mode,
        limit: 100,
        offset: 0,
      })}`,
    ),
    ...queryOptions,
  })

  const gaps = useQuery({
    queryKey: ['collection-monitor', 'gaps', filters.market, filters.dataset, filters.date],
    queryFn: () => readJson<CollectionGapPage>(
      `${API_ROOT}/gaps?${queryString({
        market: filters.market,
        dataset: filters.dataset,
        date: filters.date,
        limit: 100,
        offset: 0,
      })}`,
    ),
    ...queryOptions,
  })

  return { overview, markets, tasks, gaps }
}

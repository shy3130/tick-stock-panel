import { useEffect, useMemo, useRef } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import type { KlineRow } from './api'
import { fetchDailyBatchChunked } from './klineBatchChunked'
import { QK } from './queryKeys'

const FRESH_MS = 5 * 60_000
type Batch = { data: Record<string, KlineRow[]> }

export function useWatchlistDailyBatch(symbols: string[], days: number, enabled: boolean) {
  const client = useQueryClient()
  const key = QK.watchlistKlineBatch(days)
  const scope = useMemo(() => JSON.stringify([days, symbols]), [days, symbols])
  const attempted = useRef<string | null>(null)
  const query = useQuery({
    queryKey: key,
    enabled: enabled && symbols.length > 0,
    staleTime: FRESH_MS,
    placeholderData: prev => prev,
    queryFn: async (): Promise<Batch> => {
      attempted.current = scope
      const cached = client.getQueryState<Batch>(key)
      const fresh = cached && !cached.isInvalidated && Date.now() - cached.dataUpdatedAt < FRESH_MS
      const previous = fresh ? cached.data?.data : undefined
      const missing = symbols.filter(symbol => !previous?.[symbol])
      const patch = await fetchDailyBatchChunked(missing, days)
      // 明确记住已查过的空结果，避免停牌或缺数据时反复请求。
      return { data: Object.fromEntries(symbols.map(symbol => [symbol, patch.data[symbol] ?? previous?.[symbol] ?? []])) }
    },
  })

  useEffect(() => {
    if (!enabled || query.isFetching || attempted.current === scope) return
    if (symbols.some(symbol => !query.data?.data[symbol])) void query.refetch()
  }, [enabled, query.isFetching, query.data, query.refetch, scope, symbols])

  return query
}

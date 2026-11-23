import { useQuery, useQueryClient } from '@tanstack/react-query'
import { api, type WatchlistPrepareResult } from './api'
import { QK } from './queryKeys'

/** 缺数时单独提交补齐，普通列表/SSE 读取不重复创建任务；失败等待用户重试。 */
export function useWatchlistPrepare(rows: { symbol: string; close?: number | null }[] | undefined) {
  const client = useQueryClient()
  const missing = [...new Set((rows ?? []).filter(row => row.close == null).map(row => row.symbol))].sort()
  return useQuery({
    queryKey: QK.watchlistPrepare(missing),
    queryFn: async () => {
      const results: WatchlistPrepareResult[] = []
      for (let i = 0; i < missing.length; i += 50) {
        results.push(...(await api.watchlistPrepare(missing.slice(i, i + 50))).results)
      }
      if (results.some(result => result.status === 'ready')) {
        await Promise.all([
          client.invalidateQueries({ queryKey: ['watchlist-enriched'] }),
          client.invalidateQueries({ queryKey: ['kline-batch'] }),
          client.invalidateQueries({ queryKey: ['kline'] }),
        ])
      }
      return results
    },
    enabled: missing.length > 0,
    staleTime: 60_000,
    retry: false,
    refetchOnWindowFocus: false,
    refetchOnMount: false,
  })
}

import { useEffect, useRef } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from './api'
import { DAILY_DATA_QUERY_PREFIXES, MINUTE_DATA_QUERY_PREFIXES, QK } from './queryKeys'
import { useDocumentVisible } from './refreshPolicy'

/** SSE 通知与低频只读版本校准共用出口，行情关闭时仍接收补齐结果。 */
export function useDataVersions() {
  const client = useQueryClient()
  const visible = useDocumentVisible()
  const query = useQuery({
    queryKey: QK.dataVersions,
    queryFn: api.dataVersions,
    enabled: visible,
    staleTime: 10_000,
    refetchInterval: visible ? 30_000 : false,
    retry: false,
  })
  const previous = useRef<Record<string, string> | undefined>(undefined)
  useEffect(() => {
    const versions = query.data?.versions
    if (!versions) return
    for (const [kind, prefixes] of [['daily', DAILY_DATA_QUERY_PREFIXES], ['minute', MINUTE_DATA_QUERY_PREFIXES]] as const) {
      if (versions[kind] && previous.current?.[kind] !== versions[kind]) {
        void client.invalidateQueries({
          predicate: q => prefixes.has(String(q.queryKey[0])),
          refetchType: visible ? 'active' : 'none',
        })
      }
    }
    previous.current = versions
  }, [query.data, client, visible])
}

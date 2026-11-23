import type { QueryClient } from '@tanstack/react-query'
import { EXT_DATA_QUERY_PREFIXES } from './queryKeys'

/** 写入成功后也要失效已缓存的空序列，避免切回列表仍然看不到新数据。 */
export function invalidateExtDataQueries(client: QueryClient) {
  return client.invalidateQueries({
    predicate: query => EXT_DATA_QUERY_PREFIXES.has(String(query.queryKey[0]))
      || (query.queryKey[0] === 'kline' && Boolean(query.queryKey[4])),
  })
}

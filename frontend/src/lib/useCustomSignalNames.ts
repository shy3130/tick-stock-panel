import { useMemo } from 'react'
import { useQuery } from '@tanstack/react-query'
import { api } from '@/lib/api'
import { QK } from '@/lib/queryKeys'

/**
 * 信号定义 → 输出列名 → 用户命名 的映射。
 *
 * 复用 QK.customSignals 查询 (与 SignalPicker/信号库共享缓存, 不额外发请求)。
 * 告警/监控等展示侧应把返回值传给 cnSignal 第二参数 — 不传时 csg_/csgi_/
 * 迁移的 signal_* 会显示原始列名而非用户命名。
 */
export function useCustomSignalNames(): Record<string, string> {
  const query = useQuery({ queryKey: QK.customSignals, queryFn: api.customSignalsList })
  return useMemo(() => {
    const names: Record<string, string> = {}
    for (const s of query.data?.signals ?? []) {
      if (s.id && s.name) {
        // 显式 column (迁移自内置的 signal_*) 优先于默认 csg_ 前缀
        names[s.column ?? `csg_${s.id}`] = s.name
        if (s.timeframe === 'intraday') names[`csgi_${s.id}`] = s.name
      }
    }
    return names
  }, [query.data])
}

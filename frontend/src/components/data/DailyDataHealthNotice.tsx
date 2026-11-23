import { AlertTriangle } from 'lucide-react'
import { Link } from 'react-router-dom'
import { type DailyDataRevisions, type IntegrityAsset } from '@/lib/api'
import { useDailyIntegrity } from '@/lib/useSharedQueries'
import { DAILY_DATA_QUERY_PREFIXES } from '@/lib/queryKeys'

const LABELS: Record<IntegrityAsset, string> = { stock: '股票', etf: 'ETF', index: '指数' }

export function DailyDataHealthNotice() {
  const health = useDailyIntegrity({ poll: true })
  const qc = useQueryClient()
  const previous = useRef<string | null>(null)
  const revisions = health.data?.data_revisions
  const version = revisions ? JSON.stringify(revisions) : null
  useEffect(() => {
    if (version === null) return
    if (version !== previous.current && (previous.current !== null || Object.values(revisions ?? {}).some(v => v !== '0'))) {
      // 后台自动修补/跨设备更新也要失效历史查询, 不依赖用户停留在数据页。
      void qc.invalidateQueries({ predicate: q => DAILY_DATA_QUERY_PREFIXES.has(String(q.queryKey[0])) })
    }
    previous.current = version
  }, [version, qc])
  const assets = health.data?.blocking_assets ?? []
  if (!assets.length) return null
  return (
    <div role="alert" className="mx-3 mt-2 flex flex-wrap items-center gap-2 rounded-btn border border-warning/30 bg-warning/10 px-3 py-2 text-xs text-warning">
      <AlertTriangle className="h-3.5 w-3.5 shrink-0" />
      <span>{assets.map(a => LABELS[a]).join('、')}日K存在未完成的修补或重算，受影响的计算暂不可用。</span>
      <Link className="ml-auto shrink-0 underline underline-offset-2" to="/data">前往数据页处理</Link>
    </div>
  )
}

export function DailyResultStaleNotice({ result, assetType = 'stock' }: { result: unknown; assetType?: IntegrityAsset }) {
  const health = useDailyIntegrity()
  if (result === null || typeof result !== 'object') return null
  const revisions = (result as { daily_data_revisions?: DailyDataRevisions }).daily_data_revisions
  const current = health.data?.data_revisions?.[assetType]
  if (!current || current === (revisions?.[assetType] ?? '0')) return null
  return (
    <div role="status" className="flex items-start gap-2 rounded-btn border border-warning/30 bg-warning/10 px-3 py-2 text-xs leading-relaxed text-warning">
      <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
      <span>日K数据版本已变化，下方结果仍基于旧数据，请完成修补后重新运行验证。</span>
    </div>
  )
}
import { useEffect, useRef } from 'react'
import { useQueryClient } from '@tanstack/react-query'

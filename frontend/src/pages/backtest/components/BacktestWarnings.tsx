import { AlertTriangle, Database } from 'lucide-react'
import type { BacktestDataSnapshot } from '@/lib/api'

interface Props {
  warnings?: string[]
  dataSnapshot?: BacktestDataSnapshot
}

const warningLabel = (warning: string) => {
  if (warning.startsWith('survivorship_bias:')) {
    return '当前股票池不能证明是历史时点股票池，结果可能存在幸存者偏差。'
  }
  if (warning.startsWith('legacy_vectorbt_engine:')) {
    return '旧信号回测与主 Polars/NumPy 引擎语义不同，结果不可直接横向比较。'
  }
  if (warning === 'candidate_return_curve') {
    return '当前曲线是候选样本按退出日等权复利，不是可交易账户净值。'
  }
  return warning
}

export function BacktestWarnings({ warnings = [], dataSnapshot }: Props) {
  const generation = dataSnapshot?.canonical_generation
  const cutoff = dataSnapshot?.data_cutoff
  const hasSnapshot = Boolean(generation || cutoff)
  const visibleWarnings = [...new Set(warnings.filter(Boolean))]

  if (visibleWarnings.length === 0 && !generation && !cutoff) return null

  return (
    <div className="space-y-2">
      {visibleWarnings.length > 0 && (
        <div className="rounded-btn border border-warning/30 bg-warning/5 px-3 py-2 text-[11px] leading-5 text-secondary">
          <div className="mb-0.5 flex items-center gap-1.5 font-medium text-warning">
            <AlertTriangle className="h-3.5 w-3.5" />
            方法论提醒
          </div>
          <ul className="list-disc space-y-0.5 pl-5">
            {visibleWarnings.map(warning => <li key={warning}>{warningLabel(warning)}</li>)}
          </ul>
        </div>
      )}
      {hasSnapshot && (
        <div className="flex flex-wrap items-center gap-x-3 gap-y-1 rounded-btn border border-border bg-elevated/20 px-3 py-2 text-[10px] text-muted">
          <Database className="h-3 w-3" />
          <span>数据截止 <b className="font-mono text-secondary">{String(cutoff ?? '—')}</b></span>
          <span>generation <b className="font-mono text-secondary">{String(generation ?? '未冻结')}</b></span>
          {dataSnapshot?.snapshot_hash ? (
            <span>快照 <b className="font-mono text-secondary">{String(dataSnapshot.snapshot_hash).slice(0, 12)}</b></span>
          ) : null}
        </div>
      )}
    </div>
  )
}

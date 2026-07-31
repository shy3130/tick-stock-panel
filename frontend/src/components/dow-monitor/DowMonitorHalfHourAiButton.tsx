import { useState } from 'react'

import { cn } from '@/lib/cn'

import { DowMonitorAiAnalysisDialog } from './DowMonitorAiAnalysisDialog'
import { formatServerTimestamp } from './formatServerTimestamp'
import type { DowMonitorHalfHourAiSummary } from './types'


const STATUS_LABELS: Record<DowMonitorHalfHourAiSummary['status'], string> = {
  pending: '等待首个检查点',
  running: '分析中',
  completed: '已更新',
  failed: '分析失败',
  insufficient_data: '数据不足',
  unavailable: '暂不可用',
}

export function DowMonitorHalfHourAiButton({
  symbol,
  latest,
  compact = false,
}: {
  symbol: string
  latest?: DowMonitorHalfHourAiSummary
  compact?: boolean
}) {
  const [open, setOpen] = useState(false)
  const current = latest ?? {
    analysis_id: null,
    status: 'unavailable' as const,
    window_end: null,
    title: null,
    summary: null,
  }
  const actionable = Boolean(current.analysis_id)
  const checkpoint = formatServerTimestamp(current.window_end)
  return (
    <>
      <button
        type="button"
        aria-label={`查看 ${symbol} 半小时分析`}
        disabled={!actionable}
        onClick={(event) => {
          event.stopPropagation()
          setOpen(true)
        }}
        className={cn(
          'min-w-0 rounded-btn border border-border px-2 py-1 text-left disabled:cursor-default disabled:opacity-60',
          compact ? 'w-full' : 'w-44',
        )}
      >
        <span className="block text-[9px] text-muted">
          半小时分析 · {STATUS_LABELS[current.status]}
          {checkpoint ? ` · 北京时间 ${checkpoint.slice(11)}` : ''}
        </span>
        <strong className="block truncate text-[11px] font-medium">
          {current.title || STATUS_LABELS[current.status]}
        </strong>
      </button>
      {open && (
        <DowMonitorAiAnalysisDialog
          symbol={symbol}
          latest={current}
          onClose={() => setOpen(false)}
        />
      )}
    </>
  )
}

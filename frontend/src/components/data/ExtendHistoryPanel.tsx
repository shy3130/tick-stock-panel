import { useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { Loader2 } from 'lucide-react'
import { api } from '@/lib/api'
import { QK } from '@/lib/queryKeys'

export function ExtendHistoryPanel({ caps, isRunning, earliestDate, onStart }: {
  caps: { label: string; capabilities: Record<string, { rpm: number | null; batch: number | null; subscribe: number | null }> } | undefined
  isRunning: boolean
  earliestDate: string | null
  onStart: () => void
}) {
  const qc = useQueryClient()
  const [value, setValue] = useState(6)
  const [unit, setUnit] = useState<'month' | 'year'>('month')
  const hasBatchCap = !!caps?.capabilities?.['kline.daily.batch']

  const extend = useMutation({
    mutationFn: () => api.extendHistory(value, unit),
    onSuccess: () => {
      onStart()
      qc.invalidateQueries({ queryKey: QK.pipelineJobs })
    },
  })

  const offsetDays = unit === 'month' ? value * 30 : value * 365
  const estimate = earliestDate
    ? (() => {
        const d = new Date(earliestDate)
        d.setDate(d.getDate() - offsetDays)
        return d.toISOString().slice(0, 10)
      })()
    : null

  return (
    <div className="px-4 pb-4 pt-3 border-t border-accent/20 space-y-3">
      <div className="text-[10px] text-secondary">向前扩展历史数据</div>

      <div className="flex items-center gap-2">
        <div className="flex items-center">
          <button
            onClick={() => setValue(Math.max(1, value - 1))}
            disabled={!hasBatchCap || isRunning}
            className="h-6 w-6 flex items-center justify-center rounded-l-btn bg-elevated border border-border text-secondary hover:bg-border/50 disabled:opacity-30 transition-colors text-xs"
          >−</button>
          <div className="h-6 w-8 flex items-center justify-center border-y border-border text-[11px] font-mono tabular-nums text-foreground bg-base">
            {value}
          </div>
          <button
            onClick={() => setValue(Math.min(unit === 'year' ? 10 : 36, value + 1))}
            disabled={!hasBatchCap || isRunning}
            className="h-6 w-6 flex items-center justify-center rounded-r-btn bg-elevated border border-border text-secondary hover:bg-border/50 disabled:opacity-30 transition-colors text-xs"
          >+</button>
        </div>

        <div className="flex rounded-btn border border-border overflow-hidden">
          {(['month', 'year'] as const).map(u => (
            <button
              key={u}
              onClick={() => { setUnit(u); if (u === 'year' && value > 10) setValue(1); if (u === 'month' && value > 36) setValue(6) }}
              className={`px-2 py-0.5 text-[10px] font-medium transition-colors ${
                unit === u ? 'bg-accent/15 text-accent' : 'text-secondary hover:bg-elevated'
              }`}
            >{u === 'month' ? '月' : '年'}</button>
          ))}
        </div>
      </div>

      {estimate && (
        <div className="text-[10px] text-muted">
          预计扩展至 <span className="font-mono text-secondary">{estimate}</span>
          {earliestDate && <span> (当前最早: <span className="font-mono text-secondary">{earliestDate}</span>)</span>}
        </div>
      )}

      <button
        onClick={() => extend.mutate()}
        disabled={!hasBatchCap || isRunning || extend.isPending || !earliestDate}
        className="w-full inline-flex items-center justify-center gap-1.5 px-3 py-1.5 rounded-btn bg-accent/90 text-base text-xs font-medium hover:bg-accent disabled:opacity-40 disabled:pointer-events-none transition-colors duration-150"
      >
        {extend.isPending ? (
          <>
            <Loader2 className="h-3 w-3 animate-spin" />
            请求中…
          </>
        ) : (
          <>获取数据</>
        )}
      </button>

      {!hasBatchCap && (
        <span className="text-[10px] text-warning/80 bg-warning/8 rounded px-1.5 py-px font-medium">
          需数据源支持
        </span>
      )}
    </div>
  )
}

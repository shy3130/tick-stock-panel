import { useState, useEffect } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Loader2 } from 'lucide-react'
import { api } from '@/lib/api'
import { QK } from '@/lib/queryKeys'

export function MinuteSyncConfig({ caps, isRunning, onStart }: { caps: { label: string; capabilities: Record<string, { rpm: number | null; batch: number | null; subscribe: number | null }> } | undefined; isRunning: boolean; onStart: () => void }) {
  const qc = useQueryClient()
  const prefs = useQuery({
    queryKey: QK.preferences,
    queryFn: api.preferences,
  })
  const update = useMutation({
    mutationFn: ({ enabled, days }: { enabled: boolean; days: number }) =>
      api.updateMinuteSync(enabled, days),
    onSuccess: () => qc.invalidateQueries({ queryKey: QK.preferences }),
  })

  const hasMinuteCap = !!caps?.capabilities?.['kline.minute.batch']
  const hasMinuteMonthCap = !!caps?.capabilities?.['kline.minute.month']
  const enabled = prefs.data?.minute_sync_enabled ?? false
  const days = prefs.data?.minute_sync_days ?? 5
  const [localDays, setLocalDays] = useState(days)

  useEffect(() => { setLocalDays(days) }, [days])

  const handleToggle = () => {
    if (!hasMinuteCap) return
    update.mutate({ enabled: !enabled, days: localDays })
  }

  return (
    <div className="px-4 pb-4 pt-3 border-t border-accent/20 space-y-3">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2.5">
          <button
            onClick={handleToggle}
            disabled={!hasMinuteCap}
            className={`relative inline-flex h-5 w-9 items-center rounded-full transition-colors duration-200 shrink-0 ${
              enabled ? 'bg-accent shadow-[0_0_6px_rgba(61,214,140,0.3)]' : 'bg-elevated'
            } ${!hasMinuteCap ? 'opacity-40 cursor-not-allowed' : 'cursor-pointer'}`}
          >
            <span
              className={`inline-block h-3.5 w-3.5 rounded-full bg-white shadow-sm transition-transform duration-200 ${
                enabled ? 'translate-x-[18px]' : 'translate-x-0.5'
              }`}
            />
          </button>
          <span className="text-xs text-foreground font-medium">
            {enabled ? '自动同步' : '已关闭'}
          </span>
        </div>
        {!hasMinuteCap && (
          <span className="text-[10px] text-warning/80 bg-warning/8 rounded px-1.5 py-px font-medium">
            需数据源支持
          </span>
        )}
      </div>

      <div className="flex items-center justify-between">
        <span className="text-[10px] text-secondary">同步天数</span>
        <div className="flex items-center gap-2">
          <div className="flex items-center">
            <button
              onClick={() => { const v = Math.max(1, localDays - 1); setLocalDays(v); update.mutate({ enabled, days: v }) }}
              disabled={!hasMinuteCap || !enabled || localDays <= 1}
              className="h-6 w-6 flex items-center justify-center rounded-l-btn bg-elevated border border-border text-secondary hover:bg-border/50 disabled:opacity-30 transition-colors text-xs"
            >
              −
            </button>
            <div
              className={`h-6 w-8 flex items-center justify-center border-y border-border text-[11px] font-mono tabular-nums ${
                enabled ? 'text-foreground bg-base' : 'text-muted bg-elevated/50'
              }`}
            >
              {localDays}
            </div>
            <button
              onClick={() => { const v = Math.min(15, localDays + 1); setLocalDays(v); update.mutate({ enabled, days: v }) }}
              disabled={!hasMinuteCap || !enabled || localDays >= 15}
              className="h-6 w-6 flex items-center justify-center rounded-r-btn bg-elevated border border-border text-secondary hover:bg-border/50 disabled:opacity-30 transition-colors text-xs"
            >
              +
            </button>
          </div>
          <span className="text-[10px] text-muted">天</span>
        </div>
      </div>

      <div className="pt-2 border-t border-border space-y-2.5">
        <div className="text-[10px] text-secondary">向前扩展历史数据</div>
        <MinuteExtendControls hasMinuteCap={hasMinuteCap} hasMinuteMonthCap={hasMinuteMonthCap} isRunning={isRunning} onStart={onStart} />
      </div>

      <div className="text-[10px] text-muted">
        A股标的 · 原始数据存储(查询时实时复权)
      </div>
    </div>
  )
}

function MinuteExtendControls({ hasMinuteCap, hasMinuteMonthCap, isRunning, onStart }: { hasMinuteCap: boolean; hasMinuteMonthCap: boolean; isRunning: boolean; onStart: () => void }) {
  const qc = useQueryClient()
  const canUseMonth = hasMinuteMonthCap
  const [unit, setUnit] = useState<'day' | 'month'>('day')
  const [value, setValue] = useState(5)
  const [confirmOpen, setConfirmOpen] = useState(false)

  const dataStatus = useQuery({
    queryKey: QK.dataStatus,
    queryFn: api.dataStatus,
  })
  // 判断本地是否已有分钟K数据:后端 _safe_aggregate_minute 为避免全表扫描,
  // rows 恒为 0,改用 trading_days(分区目录数,真实统计)判断。
  const hasMinuteData = !!(dataStatus.data?.minute?.trading_days)

  const extend = useMutation({
    mutationFn: () => api.extendMinuteHistory(value, unit),
    onSuccess: () => {
      onStart()
      qc.invalidateQueries({ queryKey: QK.pipelineJobs })
      qc.invalidateQueries({ queryKey: QK.dataStatus })
    },
  })

  // 各单位上限:day 15 天,month 6 月(180 天)
  const maxValue = unit === 'month' ? 6 : 15

  const handleFetch = () => {
    if (!hasMinuteData) {
      setConfirmOpen(true)
    } else {
      extend.mutate()
    }
  }

  // 切换单位时把 value clamp 到新单位的上限
  const switchUnit = (u: 'day' | 'month') => {
    if (u === unit) return
    setUnit(u)
    const max = u === 'month' ? 6 : 15
    setValue(v => Math.min(v, max))
  }

  return (
    <>
      <div className="flex items-center gap-2">
        <div className="flex items-center">
          <button
            onClick={() => setValue(Math.max(1, value - 1))}
            disabled={!hasMinuteCap || isRunning || extend.isPending}
            className="h-6 w-6 flex items-center justify-center rounded-l-btn bg-elevated border border-border text-secondary hover:bg-border/50 disabled:opacity-30 transition-colors text-xs"
          >−</button>
          <div className="h-6 w-8 flex items-center justify-center border-y border-border text-[11px] font-mono tabular-nums text-foreground bg-base">
            {value}
          </div>
          <button
            onClick={() => setValue(Math.min(maxValue, value + 1))}
            disabled={!hasMinuteCap || isRunning || extend.isPending || value >= maxValue}
            className="h-6 w-6 flex items-center justify-center rounded-r-btn bg-elevated border border-border text-secondary hover:bg-border/50 disabled:opacity-30 transition-colors text-xs"
          >+</button>
        </div>

        {canUseMonth ? (
          <div className="flex rounded-btn border border-border overflow-hidden">
            {(['day', 'month'] as const).map(u => (
              <button
                key={u}
                onClick={() => switchUnit(u)}
                className={`px-2 py-0.5 text-[10px] font-medium transition-colors ${
                  unit === u ? 'bg-accent/15 text-accent' : 'text-secondary hover:bg-elevated'
                }`}
              >{u === 'day' ? '天' : '月'}</button>
            ))}
          </div>
        ) : (
          <span className="text-[10px] text-muted">天</span>
        )}
      </div>

      <button
        onClick={handleFetch}
        disabled={!hasMinuteCap || isRunning || extend.isPending}
        className="w-full inline-flex items-center justify-center gap-1.5 px-3 py-1.5 rounded-btn bg-accent/90 text-base text-xs font-medium hover:bg-accent disabled:opacity-40 disabled:pointer-events-none transition-colors duration-150"
      >
        {extend.isPending ? (
          <><Loader2 className="h-3 w-3 animate-spin" />请求中…</>
        ) : (
          <>获取数据</>
        )}
      </button>

      {confirmOpen && (
        <div className="fixed inset-0 z-50 flex items-center justify-center">
          <div className="absolute inset-0 bg-black/60 backdrop-blur-sm" onClick={() => setConfirmOpen(false)} />
          <div className="relative rounded-card border border-border bg-surface shadow-2xl mx-4 px-6 py-5 max-w-sm w-full space-y-4">
            <div className="text-sm text-foreground text-center">本地暂无分钟K数据，是否立即获取最近 {value} {unit === 'month' ? '月' : '天'}的分钟K？</div>
            <div className="flex items-center justify-center gap-3">
              <button
                onClick={() => { setConfirmOpen(false); extend.mutate() }}
                disabled={extend.isPending}
                className="px-4 py-1.5 rounded-btn bg-accent/90 text-base text-xs font-medium hover:bg-accent disabled:opacity-40 transition-colors duration-150"
              >
                确定
              </button>
              <button
                onClick={() => setConfirmOpen(false)}
                className="px-4 py-1.5 rounded-btn bg-elevated text-secondary text-xs hover:bg-elevated/80 transition-colors duration-150"
              >
                取消
              </button>
            </div>
          </div>
        </div>
      )}
    </>
  )
}

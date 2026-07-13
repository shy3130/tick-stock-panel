import { useState, useEffect } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { api } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { usePreferences, useCapabilities } from '@/lib/useSharedQueries'
import { isExpertOrAbove } from '@/lib/capability-labels'

/**
 * 五档盘口 sealed(真假涨停) 配置内容(纯内容, 无外框, 由父级 Card 包裹)。
 *
 * - 轮询间隔: Pro 10~120s / Expert 3~120s
 * - 盘后定版时间: 15:01~18:00, 默认 15:02
 * - disabled 时(监控关闭)输入框禁用
 */
// 注: 文件名保留 DepthConfigCard.tsx, 导出 DepthConfigContent(纯内容无外框)
export function DepthConfigContent({ disabled }: { disabled?: boolean }) {
  const qc = useQueryClient()
  const prefs = usePreferences()
  const caps = useCapabilities()

  const hasDepth = !!caps.data?.capabilities?.['depth5.batch']
  const tierLabel = caps.data?.label ?? ''
  const range = isExpertOrAbove(tierLabel) ? { lo: 3, hi: 120 } : { lo: 10, hi: 120 }

  const interval = prefs.data?.depth_polling_interval ?? 10
  const finalizeTime = prefs.data?.depth_finalize_time ?? { hour: 15, minute: 2 }

  const [intervalInput, setIntervalInput] = useState(String(Math.round(interval)))
  const [finalizeHour, setFinalizeHour] = useState(String(finalizeTime.hour))
  const [finalizeMinute, setFinalizeMinute] = useState(String(finalizeTime.minute))

  useEffect(() => { setIntervalInput(String(Math.round(interval))) }, [interval])
  useEffect(() => {
    setFinalizeHour(String(finalizeTime.hour))
    setFinalizeMinute(String(finalizeTime.minute))
  }, [finalizeTime.hour, finalizeTime.minute])

  const saveInterval = useMutation({
    mutationFn: (v: number) => api.updateDepthPollingInterval(v),
    onSuccess: () => qc.invalidateQueries({ queryKey: QK.preferences }),
  })
  const saveFinalize = useMutation({
    mutationFn: ({ hour, minute }: { hour: number; minute: number }) =>
      api.updateDepthFinalizeTime(hour, minute),
    onSuccess: () => qc.invalidateQueries({ queryKey: QK.preferences }),
  })

  // 无能力: 显示升级提示
  if (!hasDepth) {
    return (
      <p className="text-xs text-muted leading-relaxed">
        真假涨停判定依赖五档盘口实时快照,需 <span className="text-accent">Pro 及以上套餐</span>。
        升级后连板梯队将自动区分真封板(显示封单量)与假涨停(归入炸板)。
      </p>
    )
  }

  const inputCls = `w-16 h-7 bg-elevated border border-border rounded text-xs text-center px-1 focus:outline-none focus:border-accent/50 ${disabled ? 'opacity-40 cursor-not-allowed' : ''}`

  return (
    <div className="space-y-3">
      {/* 盘中轮询间隔 */}
      <div className="flex items-center justify-between gap-2">
        <div className={disabled ? 'opacity-50' : ''}>
          <div className="text-xs text-secondary">盘中轮询间隔</div>
          <div className="text-[10px] text-muted">范围 {range.lo}~{range.hi} 秒 · 涨跌停过多时系统自动放慢</div>
        </div>
        <div className="flex items-center gap-1">
          <input
            type="number"
            min={range.lo}
            max={range.hi}
            value={intervalInput}
            disabled={disabled}
            onChange={e => setIntervalInput(e.target.value)}
            onBlur={() => {
              if (disabled) return
              let v = Number(intervalInput)
              if (!Number.isFinite(v)) v = range.lo
              v = Math.max(range.lo, Math.min(range.hi, v))
              saveInterval.mutate(v)
            }}
            className={inputCls}
          />
          <span className="text-xs text-muted">秒</span>
        </div>
      </div>

      {/* 盘后定版时间 */}
      <div className="flex items-center justify-between gap-2">
        <div className={disabled ? 'opacity-50' : ''}>
          <div className="text-xs text-secondary">盘后定版时间</div>
          <div className="text-[10px] text-muted">范围 15:01~18:00 · 收盘后拉取最终盘口定版</div>
        </div>
        <div className="flex items-center gap-1">
          <input
            type="number"
            min={15}
            max={18}
            value={finalizeHour}
            disabled={disabled}
            onChange={e => setFinalizeHour(e.target.value)}
            onBlur={() => {
              if (disabled) return
              let h = Number(finalizeHour)
              if (!Number.isFinite(h)) h = 15
              h = Math.max(15, Math.min(18, h))
              let m = Number(finalizeMinute)
              if (!Number.isFinite(m)) m = 2
              m = Math.max(0, Math.min(59, m))
              if (h * 60 + m < 15 * 60 + 1) { h = 15; m = 1 }
              if (h * 60 + m > 18 * 60) { h = 18; m = 0 }
              saveFinalize.mutate({ hour: h, minute: m })
            }}
            className={`w-12 h-7 bg-elevated border border-border rounded text-xs text-center px-1 focus:outline-none focus:border-accent/50 ${disabled ? 'opacity-40 cursor-not-allowed' : ''}`}
          />
          <span className="text-xs text-muted">:</span>
          <input
            type="number"
            min={0}
            max={59}
            value={finalizeMinute}
            disabled={disabled}
            onChange={e => setFinalizeMinute(e.target.value)}
            onBlur={() => {
              if (disabled) return
              let h = Number(finalizeHour)
              if (!Number.isFinite(h)) h = 15
              h = Math.max(15, Math.min(18, h))
              let m = Number(finalizeMinute)
              if (!Number.isFinite(m)) m = 2
              m = Math.max(0, Math.min(59, m))
              if (h * 60 + m < 15 * 60 + 1) { h = 15; m = 1 }
              if (h * 60 + m > 18 * 60) { h = 18; m = 0 }
              saveFinalize.mutate({ hour: h, minute: m })
            }}
            className={`w-12 h-7 bg-elevated border border-border rounded text-xs text-center px-1 focus:outline-none focus:border-accent/50 ${disabled ? 'opacity-40 cursor-not-allowed' : ''}`}
          />
        </div>
      </div>
    </div>
  )
}

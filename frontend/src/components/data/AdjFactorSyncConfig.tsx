import { useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { Loader2, Download, Info } from 'lucide-react'
import { api } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { MissingCapChip } from '@/lib/capability-labels'

// 除权因子独立同步面板 (数据画像卡齿轮弹窗, 模式与 MinuteSyncConfig 一致)。
// hasCap: 路由矩阵判定除权因子能力可用 (生效源含插件/自定义源);
// providerDisplay: 生效源展示名, 路由为 TickFlow 时为 null。
export function AdjFactorSyncConfig({ hasCap, providerDisplay, onJobStart }: {
  hasCap: boolean
  providerDisplay: string | null
  onJobStart?: (jobId: string) => void
}) {
  const qc = useQueryClient()
  const [starting, setStarting] = useState(false)

  const handleSync = () => {
    if (!hasCap || starting) return
    setStarting(true)
    api.pipelineAdjFactorRun().then((res) => {
      qc.invalidateQueries({ queryKey: QK.pipelineJobs })
      qc.invalidateQueries({ queryKey: QK.dataStatus })
      // 通知主页面跟踪 job 进度 (顶部 ActiveJobCard 显示实时进度+日志)
      if (res.job_id && onJobStart) onJobStart(res.job_id)
    }).finally(() => setStarting(false))
  }

  return (
    <div className="px-4 pb-4 pt-3 border-t border-accent/20 space-y-3">
      {/* 生效数据源 */}
      <div className="flex items-center justify-between">
        <span className="text-xs text-foreground font-medium">生效数据源</span>
        <div className="flex items-center gap-2">
          <span className="text-[11px] text-secondary font-mono">{providerDisplay ?? 'TickFlow'}</span>
          {!hasCap && <MissingCapChip capKey="adj_factor" />}
        </div>
      </div>

      {/* 手动同步 (全历史, 与盘后管道共用单飞任务槽) */}
      <div className="pt-3 border-t border-border space-y-2">
        <div className="flex items-center gap-1.5">
          <Download className="h-3 w-3 text-secondary" />
          <span className="text-[11px] text-secondary font-medium">手动同步</span>
          <span className="text-[10px] text-muted">与盘后管道共用任务槽</span>
        </div>
        <button
          onClick={handleSync}
          disabled={!hasCap || starting}
          className="w-full inline-flex items-center justify-center gap-1.5 px-3 py-2 rounded-btn bg-accent/90 text-foreground text-xs font-medium hover:bg-accent disabled:opacity-40 transition-colors duration-150"
        >
          {starting ? (
            <><Loader2 className="h-3.5 w-3.5 animate-spin" /><span>启动中…</span></>
          ) : (
            <><Download className="h-3.5 w-3.5" /><span>全历史同步除权因子</span></>
          )}
        </button>
        <div className="flex items-start gap-1.5 text-[10px] text-muted leading-relaxed">
          <Info className="h-3 w-3 shrink-0 mt-px" />
          <span>
            拉取除权事件(等比因子 + 分红/送转/配股明细),窗口按需钳到本地日K起点 —
            更早的事件对已存价格零影响;因子发生变化的个股自动重算前复权,
            仅明细回填不触发重算。幂等,可重复执行。
          </span>
        </div>
        {providerDisplay == null && (
          <div className="text-[10px] text-muted leading-relaxed">
            当前路由 TickFlow:只返回等比因子,无事件明细。需要明细时,在
            设置 → 数据源 将「除权因子」切换到提供明细的数据源后重跑。
          </div>
        )}
      </div>
    </div>
  )
}

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { AlertTriangle, CheckCircle2, LoaderCircle, RefreshCw, Square } from 'lucide-react'
import { toast } from '@/components/Toast'
import { api } from '@/lib/api'
import { QK } from '@/lib/queryKeys'

/** Checking/recovering is always available; terminating an owner remains a separate action. */
export function MiningLockControl() {
  const queryClient = useQueryClient()
  const lockQuery = useQuery({
    queryKey: QK.miningLock,
    queryFn: api.miningLockStatus,
    staleTime: 10_000,
    refetchInterval: query => query.state.data?.held && !query.state.data.mine ? 15_000 : false,
  })
  const refreshRuns = () => {
    void queryClient.invalidateQueries({ queryKey: QK.miningRuns })
    void queryClient.invalidateQueries({
      predicate: query => query.queryKey[0] === QK.miningRun('')[0],
    })
  }
  const recover = useMutation({
    mutationFn: api.miningRecoverLock,
    onMutate: () => queryClient.cancelQueries({ queryKey: QK.miningLock }),
    onSuccess: status => {
      queryClient.setQueryData(QK.miningLock, status)
      if (status.mine) {
        refreshRuns()
        toast('挖掘已就绪，可以开始任务', 'success')
      }
    },
  })
  const release = useMutation({
    mutationFn: api.miningForceReleaseLock,
    onSuccess: () => {
      toast('已停止占用进程并恢复挖掘', 'success')
      refreshRuns()
    },
    onSettled: () => queryClient.invalidateQueries({ queryKey: QK.miningLock }),
  })
  const data = lockQuery.data
  const busy = recover.isPending || release.isPending
  const error = recover.error || release.error || lockQuery.error
  const blocked = !!data?.held && !data.mine
  const holder = data?.holder
  const title = error ? '挖掘状态检查未完成'
    : data?.mine ? '挖掘已就绪'
      : blocked ? '挖掘被其他实例占用'
        : data ? '挖掘需要恢复' : '正在检查挖掘状态'
  const Icon = busy || lockQuery.isPending ? LoaderCircle : data?.mine && !error ? CheckCircle2 : AlertTriangle

  return (
    <div className={`mb-3 rounded-card border px-3 py-2.5 ${blocked || error ? 'border-warning/40 bg-warning/10' : 'border-border bg-surface'}`}>
      <div className="flex flex-col items-stretch justify-between gap-3 sm:flex-row sm:items-center">
        <div className="flex min-w-0 flex-1 items-start gap-2 text-xs" role="status" aria-live="polite">
          <Icon className={`mt-0.5 h-4 w-4 shrink-0 ${busy || lockQuery.isPending ? 'animate-spin text-muted' : data?.mine && !error ? 'text-success' : 'text-warning'}`} />
          <div className="min-w-0">
            <div className="font-medium text-foreground">{title}</div>
            <div className="mt-0.5 break-words text-[11px] leading-4 text-secondary">
              {error ? error.message : data?.mine ? '当前后端已取得挖掘权限。'
                : blocked ? <>
                  {holder ? `占用进程 ${holder.pid}${holder.role === 'worker' ? ' · 挖掘工作进程' : holder.role === 'app' ? ' · 后端实例' : ''}。` : ''}
                  {data.store_idle_seconds != null ? `挖掘存储已静默约 ${Math.max(1, Math.round(data.store_idle_seconds / 60))} 分钟。` : ''}
                  {data.force_releasable
                    ? '未发现进行中的挖掘任务，且满足释放条件；确认后可停止该进程并接管。'
                    : data.reason || '暂时无法确认占用进程。请正常退出其他面板实例，再检查恢复。'}
                </> : data ? '锁已空闲，点击下方按钮即可恢复，也可以直接开始挖掘。'
                  : '正在读取状态，也可点击按钮重新检查。'}
            </div>
          </div>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <button
            type="button"
            disabled={busy}
            onClick={() => { release.reset(); recover.mutate() }}
            className="inline-flex min-h-8 items-center gap-1.5 rounded-btn border border-border bg-elevated px-3 py-1.5 text-xs font-medium text-foreground hover:bg-base disabled:opacity-50"
          >
            <RefreshCw className={`h-3.5 w-3.5 ${recover.isPending ? 'animate-spin' : ''}`} />
            {recover.isPending ? '正在检查并恢复…' : '检查并恢复挖掘'}
          </button>
          {blocked && data.force_releasable && holder && !error && (
            <button
              type="button"
              disabled={busy}
              onClick={() => {
                if (window.confirm(
                  `确定停止占用挖掘的进程（PID ${holder.pid}）并由当前后端接管？\n\n` +
                  '如果它是另一个正在使用的面板实例，请优先到该实例正常退出。确认后会再次核验占用状态。',
                )) { recover.reset(); release.mutate(holder.pid) }
              }}
              className="inline-flex min-h-8 items-center gap-1.5 rounded-btn border border-warning/60 bg-warning/20 px-3 py-1.5 text-xs font-medium text-foreground hover:bg-warning/30 disabled:opacity-50"
            >
              {release.isPending ? <LoaderCircle className="h-3.5 w-3.5 animate-spin" /> : <Square className="h-3.5 w-3.5" />}
              强制停止并接管
            </button>
          )}
        </div>
      </div>
    </div>
  )
}

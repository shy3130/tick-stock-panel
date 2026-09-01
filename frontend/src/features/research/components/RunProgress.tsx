import { Loader2, RotateCcw, Square, Star } from 'lucide-react'
import { Link } from 'react-router-dom'
import { Btn, Control } from '@/components/ui/Primitives'
import { cn } from '@/lib/cn'
import type { RunStreamState } from '../hooks/useRunStream'
import { isActiveJob, type JobStatus } from '../model/status'
import { JobStatusBadge } from './StatusBadges'

export function RunProgress({ stream, jobStatus }: { stream: RunStreamState; jobStatus: JobStatus | null }) {
  const status = stream.jobStatus ?? jobStatus
  const running = isActiveJob(status) && stream.connection !== 'closed'
  return (
    <div className="space-y-2 rounded-input border border-border bg-base/40 p-3" aria-live="polite">
      <div className="flex flex-wrap items-center gap-2">
        <JobStatusBadge value={status} />
        {running ? <Loader2 className="h-3.5 w-3.5 text-accent motion-safe:animate-spin" aria-hidden /> : null}
        <span className="text-xs text-secondary">{stream.stage || stream.message || connectionLabel(stream)}</span>
      </div>
      {running ? (
        <div className="h-1.5 overflow-hidden rounded-full bg-elevated">
          <div
            className="h-full bg-accent transition-[width] duration-base ease-smooth"
            style={{ width: `${Math.round(Math.min(1, Math.max(0, stream.ratio ?? 0.08)) * 100)}%` }}
          />
        </div>
      ) : null}
      {stream.interrupted ? (
        <p className="text-xs text-warning">运行已中断（无断点续跑）。请基于此 Run 重跑以生成新的不可变记录。</p>
      ) : null}
    </div>
  )
}

function connectionLabel(stream: RunStreamState): string {
  if (stream.connection === 'reconnecting') return 'SSE 重连中'
  if (stream.connection === 'open') return '实时进度'
  return '无活动流'
}

export function RunActions({
  runId,
  jobStatus,
  favorite,
  label,
  canCancel,
  onCancel,
  onFavorite,
  onLabel,
  onRerun,
  cancelPending,
  patchPending,
}: {
  runId: string
  jobStatus: JobStatus | null
  favorite: boolean
  label: string | null
  canCancel: boolean
  onCancel: () => void
  onFavorite: (favorite: boolean) => void
  onLabel: (label: string) => void
  onRerun: () => void
  cancelPending?: boolean
  patchPending?: boolean
}) {
  return (
    <div className="flex flex-wrap items-center gap-2">
      <Link className="btn-secondary min-h-11 px-3 text-xs sm:min-h-[var(--control-h)]" to={`/research/runs/${encodeURIComponent(runId)}`}>
        打开详情
      </Link>
      <Btn variant="secondary" className="min-h-11 text-xs" onClick={onRerun}>
        <RotateCcw className="h-3.5 w-3.5" aria-hidden />
        基于此 Run 重跑
      </Btn>
      {canCancel && isActiveJob(jobStatus) ? (
        <Btn variant="ghost" className="min-h-11 text-xs text-danger" onClick={onCancel} disabled={cancelPending}>
          <Square className="h-3.5 w-3.5" aria-hidden />
          取消
        </Btn>
      ) : null}
      <Btn
        variant="ghost"
        className={cn('min-h-11 min-w-11 text-xs', favorite && 'text-warning')}
        aria-pressed={favorite}
        aria-label={favorite ? '取消收藏' : '收藏'}
        disabled={patchPending}
        onClick={() => onFavorite(!favorite)}
      >
        <Star className={cn('h-3.5 w-3.5', favorite && 'fill-current')} />
      </Btn>
      <label className="flex min-w-0 flex-1 items-center gap-2 text-xs text-muted">
        标签
        <Control
          className="min-h-11 flex-1 text-xs sm:min-h-[var(--control-h)]"
          defaultValue={label ?? ''}
          onBlur={(event) => {
            const next = event.target.value.trim()
            if (next !== (label ?? '')) onLabel(next)
          }}
        />
      </label>
    </div>
  )
}

import { useEffect, useState } from 'react'
import { Loader2, Square } from 'lucide-react'
import {
  elapsedSince,
  estimateEtaMs,
  formatClock,
  formatRate,
  type ExperimentRuntime,
  type RunConnectionState,
} from '@/lib/runStatus'

export type RunStatusKind = 'pending' | 'running' | 'completed' | 'cancelled' | 'failed'

export function BacktestRunStatus({
  status,
  title,
  runtime,
  completed,
  total,
  startedAt,
  failed,
  extras,
  connectionState,
  onCancel,
  cancelling = false,
  cancelLabel = '取消',
}: {
  status: RunStatusKind
  title: string
  runtime?: ExperimentRuntime | null
  completed?: number
  total?: number
  startedAt?: string | null
  failed?: number
  extras?: Array<{ label: string; value: string }>
  /** SSE 任务连接状态; 轮询型任务 (网格/寻优) 不传则不显示断线提示 */
  connectionState?: RunConnectionState
  onCancel?: () => void
  cancelling?: boolean
  cancelLabel?: string
}) {
  const active = status === 'pending' || status === 'running'
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    if (!active) return
    const id = window.setInterval(() => setNow(Date.now()), 1000)
    return () => window.clearInterval(id)
  }, [active])

  const done = runtime?.completed ?? completed ?? 0
  const all = runtime?.total ?? total ?? 0
  const failCount = runtime?.failed ?? failed ?? 0
  const start = runtime?.started_at ?? startedAt ?? null
  const elapsed = elapsedSince(start, now) ?? runtime?.elapsed_ms ?? 0
  const eta = runtime?.eta_ms ?? estimateEtaMs(elapsed, done, all)
  const rate = formatRate(done, elapsed)
  const percent = all > 0 ? Math.min(100, Math.max(0, Math.round((done / all) * 100))) : 0
  const stage = runtime?.label || (status === 'pending' ? '正在启动' : '运行中')
  const current = runtime?.current?.trim() || ''
  const indeterminate = active && all <= 0

  return (
    <div className="rounded-btn border border-accent/30 bg-accent/5 px-3 py-2.5">
      <div className="flex items-start gap-2.5">
        <Loader2 className={`mt-0.5 h-4 w-4 shrink-0 text-accent ${active ? 'animate-spin' : ''}`} />
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5">
            <div className="text-xs font-medium text-accent">{title}</div>
            <div className="text-[11px] text-secondary">{stage}</div>
            {all > 0 && (
              <span className="ml-auto font-mono text-sm font-semibold text-accent">{percent}%</span>
            )}
            {connectionState === 'reconnecting' && (
              <span className="inline-flex items-center gap-1 text-[11px] text-warning" role="status">
                <span className="status-dot" data-state="warn" />
                连接中断，自动重连中…
              </span>
            )}
            {connectionState === 'closed' && active && (
              <span className="inline-flex items-center gap-1 text-[11px] text-danger" role="status">
                <span className="status-dot" data-state="danger" />
                连接已断开
              </span>
            )}
          </div>
          {current && (
            <div className="mt-0.5 truncate text-[11px] text-foreground" title={current}>
              当前 {current}
            </div>
          )}
          <div
            className="mt-2 h-1.5 overflow-hidden rounded-full bg-base/60"
            role="progressbar"
            aria-label={title}
            aria-valuemin={0}
            aria-valuemax={100}
            aria-valuenow={indeterminate ? undefined : percent}
          >
            <div
              className={`h-full rounded-full bg-accent ${indeterminate ? 'w-1/3 animate-pulse' : 'transition-[width] duration-300 ease-out'}`}
              style={indeterminate ? undefined : { width: `${percent}%` }}
            />
          </div>
          <div className="mt-2 flex flex-wrap gap-x-3 gap-y-1 text-[11px] text-muted">
            {all > 0 && (
              <span>
                进度 <b className="font-mono text-foreground num">{done}</b> / {all}
              </span>
            )}
            {elapsed > 0 && (
              <span>
                已用时 <b className="font-mono text-foreground num">{formatClock(elapsed)}</b>
              </span>
            )}
            {active && eta != null && (
              <span>
                预计剩余 <b className="font-mono text-foreground num">{formatClock(eta)}</b>
              </span>
            )}
            {rate && <span>速度 {rate}</span>}
            {failCount > 0 && <span className="text-danger">失败 {failCount}</span>}
            {extras?.map(item => (
              <span key={item.label}>
                {item.label} {item.value}
              </span>
            ))}
          </div>
        </div>
        {onCancel && active && (
          <button
            type="button"
            onClick={onCancel}
            disabled={cancelling}
            className="inline-flex shrink-0 items-center gap-1 rounded-btn border border-danger/40 bg-danger/10 px-2 py-1 text-[11px] text-danger transition-colors hover:bg-danger/20 disabled:opacity-50"
          >
            {cancelling ? <Loader2 className="h-3 w-3 animate-spin" /> : <Square className="h-3 w-3 fill-current" />}
            {cancelling ? '正在取消…' : cancelLabel}
          </button>
        )}
      </div>
    </div>
  )
}

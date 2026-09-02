import { AlertCircle, Loader2, RefreshCw, ShieldAlert } from 'lucide-react'
import { EmptyState } from '@/components/EmptyState'
import { Btn } from '@/components/ui/Primitives'
import { cn } from '@/lib/cn'
import { isResearchApiError, researchErrorMessage } from '../model/errors'
import type { PreflightReason } from '../model/preflight'

export function LoadingState({ label, compact = false }: { label: string; compact?: boolean }) {
  return (
    <div className={cn('flex items-center justify-center gap-2 text-sm text-muted', compact ? 'py-5' : 'min-h-48')} role="status">
      <Loader2 className="h-4 w-4 motion-safe:animate-spin" />
      {label}
    </div>
  )
}

export function HttpErrorState({ error, onRetry, title = '研究数据读取失败' }: { error: unknown; onRetry?: () => void; title?: string }) {
  const apiError = isResearchApiError(error) ? error : null
  return (
    <section className="rounded-card border border-danger/40 bg-danger/5 p-5 text-center" role="alert">
      <AlertCircle className="mx-auto h-5 w-5 text-danger" aria-hidden />
      <p className="mt-2 text-sm font-medium text-foreground">{title}</p>
      <p className="mt-1 break-words text-xs text-danger">{researchErrorMessage(error)}</p>
      {apiError ? (
        <p className="mt-1 font-mono text-[11px] text-muted">
          {apiError.status || '—'} · {apiError.code}
          {apiError.retryable ? ' · 可重试' : ''}
        </p>
      ) : null}
      {onRetry ? (
        <Btn variant="secondary" className="mt-3 min-h-11 text-xs" onClick={onRetry}>
          <RefreshCw className="h-3.5 w-3.5" aria-hidden />
          重试
        </Btn>
      ) : null}
    </section>
  )
}

export function UnavailableState({
  reasons,
  title = '研究结果不可用',
}: {
  reasons: Array<{ code: string; message: string; observed?: number | null; required?: number | null }>
  title?: string
}) {
  return (
    <section className="rounded-card border border-warning/40 bg-warning/5 p-4" role="status">
      <p className="flex items-center gap-1.5 text-sm font-medium text-warning">
        <ShieldAlert className="h-4 w-4" aria-hidden />
        {title}
      </p>
      <p className="mt-1 text-xs text-secondary">这是领域结论，不是运行失败。系统已完成执行，但数据或样本不足以给出裁决。</p>
      <ul className="mt-3 space-y-1.5 text-xs text-secondary">
        {reasons.length === 0 ? <li>服务端未给出具体原因。</li> : reasons.map((reason) => (
          <li key={`${reason.code}-${reason.message}`} className="rounded-input border border-warning/20 bg-base/40 px-2.5 py-2">
            <span className="font-mono text-[11px] text-muted">{reason.code}</span>
            <p className="mt-0.5 leading-relaxed">{reason.message}</p>
            {reason.observed != null || reason.required != null ? (
              <p className="mt-0.5 font-mono text-[11px] text-muted">
                observed {reason.observed ?? '—'} / required {reason.required ?? '—'}
              </p>
            ) : null}
          </li>
        ))}
      </ul>
    </section>
  )
}

export function GuidedEmpty({ title, hint }: { title: string; hint: string }) {
  return <EmptyState title={title} hint={hint} />
}

export function InlineError({ message }: { message: string }) {
  return (
    <p className="mt-3 flex items-start gap-1.5 rounded-btn bg-danger/10 px-2.5 py-2 text-xs leading-relaxed text-danger" role="alert">
      <AlertCircle className="mt-0.5 h-3.5 w-3.5 shrink-0" aria-hidden />
      {message}
    </p>
  )
}

export function BlockingList({ reasons }: { reasons: PreflightReason[] }) {
  if (reasons.length === 0) return null
  return (
    <ul className="space-y-1.5 text-xs text-warning">
      {reasons.map((reason) => (
        <li key={`${reason.code}-${reason.message}`} className="rounded-input border border-warning/30 bg-warning/5 px-2.5 py-2">
          <span className="font-mono text-[11px] text-muted">{reason.code}</span>
          <p className="mt-0.5 leading-relaxed">{reason.message}</p>
        </li>
      ))}
    </ul>
  )
}

import { CheckCircle2, XCircle, Loader2, AlertCircle } from 'lucide-react'
import { formatDuration } from '@/lib/format'

export function SectionTitle({ icon: Icon, children }: { icon: React.ComponentType<{ className?: string }>; children: React.ReactNode }) {
  return (
    <h2 className="flex items-center gap-2 text-xs font-medium uppercase tracking-widest text-secondary">
      <Icon className="h-3.5 w-3.5" />
      {children}
    </h2>
  )
}

export function HistoryRow({ job, onClick }: { job: any; onClick: () => void }) {
  const statusIcon = {
    succeeded: { icon: CheckCircle2, color: 'text-bear' },
    degraded:  { icon: AlertCircle, color: 'text-warning' },
    failed:    { icon: XCircle, color: 'text-danger' },
    running:   { icon: Loader2, color: 'text-accent', spinning: true },
    pending:   { icon: Loader2, color: 'text-muted', spinning: true },
  }[job.status as 'succeeded'] ?? { icon: AlertCircle, color: 'text-muted' }
  const Icon = statusIcon.icon

  return (
    <button
      onClick={onClick}
      className="w-full px-5 py-3 hover:bg-elevated/50 transition-colors duration-150 ease-smooth text-left flex items-center justify-between gap-4"
    >
      <div className="flex items-center gap-3 min-w-0">
        <Icon className={`h-4 w-4 shrink-0 ${statusIcon.color} ${(statusIcon as any).spinning ? 'animate-spin' : ''}`} />
        <div className="min-w-0">
          <div className="font-mono text-xs text-foreground">{job.id}</div>
          <div className="text-[11px] text-muted">
            {job.started_at ? new Date(job.started_at).toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' }) : '—'}
            {' · '}
            {job.duration_s != null ? formatDuration(job.duration_s) : '...'}
          </div>
        </div>
      </div>
      <div className="text-right shrink-0">
        {job.result && (() => {
          const r = job.result as Record<string, any>
          const parts: string[] = []
          if (r.daily_days != null) parts.push(`日K ${r.daily_days}日`)
          if (r.enriched_days != null) parts.push(`enriched ${r.enriched_days}行`)
          if (r.minute_rows != null) parts.push(`分钟K ${r.minute_rows}行`)
          if (r.earliest_after && r.earliest_before) {
            const a = String(r.earliest_after).slice(0, 10)
            const b = String(r.earliest_before).slice(0, 10)
            const days = r.daily_days ?? r.minute_days ?? 0
            parts.push(days <= 1 ? a : `${a}~${b}`)
          }
          return parts.length > 0 ? (
            <div className="text-xs text-secondary font-mono">{parts.join(' · ')}</div>
          ) : null
        })()}
        {job.error && (
          <div className="text-xs text-danger truncate max-w-xs">{job.error}</div>
        )}
        {job.status === 'degraded' && job.result?.failed_stages?.[0] && (
          <div className="text-xs text-warning truncate max-w-xs">{job.result.failed_stages[0].error}</div>
        )}
      </div>
    </button>
  )
}

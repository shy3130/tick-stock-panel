import { useRef, useMemo, useEffect } from 'react'
import { motion } from 'framer-motion'
import { Loader2, CheckCircle2, XCircle, AlertTriangle } from 'lucide-react'
import { formatDuration, formatLogTime } from '@/lib/format'
import { Pill } from './StatCard'
import type { PipelineJob } from '@/lib/api'

export const STAGE_LABELS: Record<string, string> = {
  init: '初始化',
  resolve_universe: '解析标的池',
  sync_instruments: '同步个股维表',
  sync_daily: '同步日 K',
  sync_adj: '同步除权因子',
  compute_enriched: '计算技术指标',
  sync_minute: '同步分钟 K',
  extend_history: '扩展日K历史',
  extend_minute: '扩展分钟K历史',
  rebuild_enriched: '全量计算',
  repair_enriched_range: '范围补算',
  refresh_views: '刷新视图',
  done: '完成',
}

function LogViewer({ log }: { log: PipelineJob['log'] }) {
  const containerRef = useRef<HTMLDivElement>(null)

  const displayLog = useMemo(() => {
    if (!log.length) return []
    return log.filter((line, i) => {
      const next = log[i + 1]
      if (
        next &&
        line.stage === next.stage &&
        /\d+\/\d+/.test(line.msg) &&
        /\d+\/\d+/.test(next.msg)
      ) {
        return false
      }
      return true
    })
  }, [log])

  useEffect(() => {
    const el = containerRef.current
    if (el) el.scrollTop = el.scrollHeight
  }, [displayLog])

  return (
    <div ref={containerRef} className="rounded-btn bg-base/60 border border-border max-h-48 overflow-y-auto px-3 py-2 font-mono text-[11px] space-y-0.5">
      {displayLog.map((line, i) => (
        <div key={`${line.ts}-${i}`} className="flex gap-2 text-secondary">
          <span className="text-muted shrink-0">{formatLogTime(line.ts)}</span>
          <span className="text-accent/70 shrink-0">[{STAGE_LABELS[line.stage] ?? line.stage}]</span>
          <span>{line.msg}</span>
        </div>
      ))}
      {displayLog.length === 0 && <div className="text-muted">等待启动…</div>}
    </div>
  )
}

export function ActiveJobCard({ job }: { job: PipelineJob }) {
  const statusMap = {
    running:   { icon: Loader2,     color: 'text-accent',   label: '运行中', spinning: true,  border: 'border-accent/40', bg: 'bg-accent/5' },
    pending:   { icon: Loader2,     color: 'text-muted',    label: '排队中', spinning: true,  border: 'border-border',    bg: 'bg-surface' },
    succeeded: { icon: CheckCircle2, color: 'text-bear',     label: '完成',   spinning: false, border: 'border-bear/30',   bg: 'bg-bear/5' },
    degraded:  { icon: AlertTriangle, color: 'text-warning', label: '部分完成', spinning: false, border: 'border-warning/40', bg: 'bg-warning/5' },
    failed:    { icon: XCircle,     color: 'text-danger',   label: '失败',   spinning: false, border: 'border-danger/40', bg: 'bg-danger/5' },
  } as const
  const meta = statusMap[job.status]
  const Icon = meta.icon
  const isDone = job.status === 'succeeded' || job.status === 'degraded' || job.status === 'failed'
  const stageLabel = isDone ? meta.label : (STAGE_LABELS[job.stage] ?? job.stage)

  return (
    <div className={`rounded-card border ${meta.border} ${meta.bg} p-5`}>
      <div className="flex items-center justify-between mb-3">
        <div className="flex items-center gap-2.5">
          <Icon className={`h-5 w-5 ${meta.color} ${meta.spinning ? 'animate-spin' : ''}`} />
          <div>
            <div className="text-sm font-medium text-foreground">
              {meta.label}{!isDone && ` · ${stageLabel}`}
            </div>
            <div className="text-xs text-secondary font-mono mt-0.5">
              {job.id} · {job.duration_s != null ? formatDuration(job.duration_s) : '进行中'}
            </div>
          </div>
        </div>
        {!isDone && (
          <div className="font-mono text-2xl font-bold tracking-tight">
            {job.progress}<span className="text-base text-muted">%</span>
          </div>
        )}
      </div>

      {!isDone && (
        <div className="mb-2">
          <div className="h-1.5 rounded-full bg-elevated overflow-hidden">
            <motion.div
              className="h-full bg-accent"
              animate={{ width: `${job.progress}%` }}
              transition={{ duration: 0.4, ease: [0.16, 1, 0.3, 1] }}
            />
          </div>
          {job.stage_pct > 0 && (
            <div className="flex items-center justify-between mt-1">
              <span className="text-[10px] text-muted">当前阶段</span>
              <span className="text-[10px] font-mono text-secondary">{job.stage_pct}%</span>
            </div>
          )}
        </div>
      )}

      <LogViewer log={job.log} />

      {(job.status === 'succeeded' || job.status === 'degraded') && job.result && (() => {
        const skipped = new Set(job.result.skipped_stages ?? [])
        const failed = new Set((job.result.failed_stages ?? []).map((f) => f.stage))
        // 优先级: 失败 > 跳过 > 实际数值; 命中 failed_stages 的阶段摘要显示"失败"
        const cell = (stage: string | null, v: string) => {
          if (stage && failed.has(stage)) return '失败'
          if (stage && skipped.has(stage)) return '跳过'
          return v
        }
        return (
          <div className="mt-3 grid grid-cols-2 md:grid-cols-5 gap-3 text-xs">
            <Pill label="标的池" value={job.result.universe_size ?? '—'} />
            <Pill label="日 K" value={cell('sync_daily', `${job.result.daily_days ?? 0} 天`)} />
            <Pill label="除权因子" value={cell('sync_adj', `${job.result.adj_factor_symbols ?? 0} 只`)} />
            <Pill label="enriched" value={cell('compute_enriched', `${job.result.enriched_rows ?? job.result.enriched_days ?? 0} 行`)} />
            <Pill label="分钟K" value={cell('sync_minute', `${job.result.minute_rows ?? 0} 行`)} />
          </div>
        )
      })()}
      {job.status === 'degraded' && job.result?.failed_stages?.map((failure) => (
        <div key={failure.stage} className="mt-3 rounded-btn border border-warning/40 bg-warning/5 px-3 py-2 text-xs text-warning">
          {STAGE_LABELS[failure.stage] ?? failure.stage}：{failure.error}
        </div>
      ))}
      {job.status === 'failed' && job.error && (
        <div className="mt-3 rounded-btn border border-danger/40 bg-danger/5 px-3 py-2 text-xs text-danger">
          {job.error}
        </div>
      )}
    </div>
  )
}

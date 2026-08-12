import { useEffect, useRef } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { AlertTriangle, Archive, CheckCircle2, Loader2, Play } from 'lucide-react'
import { toast } from '@/components/Toast'
import { api, type CanonicalHistoryJob, type CanonicalHistoryPublished } from '@/lib/api'
import { fmtDate, formatNumber } from '@/lib/format'
import { QK } from '@/lib/queryKeys'
import { Skeleton } from './Skeleton'

const ACTIVE_JOB_STATUSES: Record<string, true> = { pending: true, running: true }
const HISTORY_STATUS_POLL_MS = 2_000

function countText(value: number | null | undefined) {
  return typeof value === 'number' && Number.isFinite(value) ? formatNumber(value) : '—'
}

function percent(value: number | null | undefined) {
  if (typeof value !== 'number' || !Number.isFinite(value)) return 0
  return Math.min(100, Math.max(0, Math.round(value)))
}

/**
 * A 股 canonical 全历史的外部快照状态与回填入口。
 *
 * 此卡片独立于数据画像卡片的可见性/排序设置，避免新增入口改变用户既有布局。
 */
export function CanonicalHistoryCard() {
  const qc = useQueryClient()
  const completedJobRef = useRef<string | null>(null)
  const status = useQuery({
    queryKey: QK.canonicalHistoryStatus,
    queryFn: api.canonicalHistoryStatus,
    refetchInterval: (query) => {
      const job = query.state.data?.job
      return job && ACTIVE_JOB_STATUSES[job.status] ? HISTORY_STATUS_POLL_MS : false
    },
  })

  const backfill = useMutation({
    mutationFn: () => api.canonicalHistoryBackfill(),
    onSuccess: (result) => {
      toast(result.status === 'running' ? '全历史回填任务已启动' : '全历史回填任务已提交', 'success')
    },
    onSettled: () => {
      qc.invalidateQueries({ queryKey: QK.canonicalHistoryStatus })
    },
  })

  const data = status.data
  const job = data?.job ?? null
  const isJobActive = job !== null && !!ACTIVE_JOB_STATUSES[job.status]
  const progress = percent(job?.progress_pct)

  useEffect(() => {
    if (!job || (job.status !== 'succeeded' && job.status !== 'failed')) return
    const key = `${job.id}:${job.status}`
    if (completedJobRef.current === key) return
    completedJobRef.current = key
    qc.invalidateQueries({ queryKey: QK.dataStatus })
  }, [job?.id, job?.status, qc])

  const isNotPublished = data?.available === false && data.reason === 'not_published'
  const isUnavailable = !status.isLoading && !status.isError && data?.available === false && !isNotPublished
  const canStart = !!data && !status.isError && (data.available || isNotPublished) && !isJobActive && !backfill.isPending
  const notice = status.isError
    ? {
        title: '无法读取外部快照状态',
        detail: '暂时无法确认回填配置与已发布 generation；请稍后重试。',
      }
    : isUnavailable
      ? {
          title: '外部快照回填不可用',
          detail: data?.reason || '当前环境未提供可用的专用外部快照根，无法发起回填。',
        }
      : null
  const badge = isUnavailable
    ? { label: '不可用', className: 'bg-danger/10 text-danger' }
    : isJobActive
      ? { label: job?.status === 'pending' ? '已排队' : '回填中', className: 'bg-accent/10 text-accent' }
      : job?.status === 'failed'
        ? { label: '回填失败', className: 'bg-danger/10 text-danger' }
        : data?.published
          ? { label: '已发布', className: 'bg-accent/10 text-accent' }
          : { label: '待首次回填', className: 'bg-elevated text-secondary' }

  return (
    <section className="panel" aria-labelledby="canonical-history-title">
      <div className="panel-header flex-wrap">
        <div className="flex min-w-0 items-center gap-2">
          <Archive className="h-4 w-4 shrink-0 text-secondary" />
          <div className="min-w-0">
            <h3 id="canonical-history-title" className="section-title">A股全历史快照</h3>
            <p className="mt-0.5 text-[10px] text-muted">canonical enriched · 外部 published generation</p>
          </div>
        </div>
        <div className="ml-auto flex items-center gap-2">
          {!status.isLoading && (
            <span className={`rounded-full px-2 py-0.5 text-[10px] font-medium ${badge.className}`}>
              {badge.label}
            </span>
          )}
          <button
            type="button"
            onClick={() => backfill.mutate()}
            disabled={!canStart}
            className="btn-primary !h-8 text-xs"
            aria-describedby="canonical-history-storage-note"
          >
            {backfill.isPending || isJobActive ? (
              <Loader2 className="h-3.5 w-3.5 animate-spin" />
            ) : (
              <Play className="h-3.5 w-3.5" />
            )}
            {backfill.isPending ? '提交中…' : isJobActive ? '回填进行中…' : '开始全历史回填'}
          </button>
        </div>
      </div>

      <div className="panel-body space-y-3">
        {status.isLoading ? (
          <div className="space-y-2.5" aria-label="正在读取全历史快照状态">
            <Skeleton w="w-40" />
            <div className="grid grid-cols-1 gap-2 sm:grid-cols-3">
              <Skeleton h="h-12" />
              <Skeleton h="h-12" />
              <Skeleton h="h-12" />
            </div>
          </div>
        ) : notice ? (
          <div className="rounded-btn border border-danger/30 bg-danger/5 px-3 py-3" role="status">
            <div className="flex items-start gap-2.5">
              <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-danger" />
              <div className="min-w-0">
                <div className="text-xs font-medium text-foreground">{notice.title}</div>
                <p className="mt-1 break-words text-[11px] leading-relaxed text-secondary">{notice.detail}</p>
              </div>
            </div>
          </div>
        ) : (
          <>
            {data?.published ? (
              <PublishedSnapshot published={data.published} />
            ) : (
              <div className="rounded-btn border border-dashed border-border bg-base/30 px-3 py-3">
                <div className="flex items-start gap-2.5">
                  <Archive className="mt-0.5 h-4 w-4 shrink-0 text-muted" />
                  <div className="min-w-0">
                    <div className="text-xs font-medium text-foreground">尚未发布全历史 generation</div>
                    <p className="mt-1 text-[11px] leading-relaxed text-muted">
                      首次回填会先在 staging 完整校验，成功后才原子发布；失败不会替换已有快照。
                    </p>
                  </div>
                </div>
              </div>
            )}

            {job && <BackfillJob job={job} progress={progress} />}
          </>
        )}

        <p id="canonical-history-storage-note" className="border-t border-border/60 pt-2 text-[10px] leading-relaxed text-muted">
          专用外部快照，不修改/不占用用户 data/；本地当日/近期分区优先覆盖同日历史。
        </p>
      </div>
    </section>
  )
}

function PublishedSnapshot({ published }: { published: CanonicalHistoryPublished }) {
  return (
    <div className="space-y-2.5">
      <div className="flex flex-col gap-1 sm:flex-row sm:items-center sm:justify-between">
        <div className="text-[11px] text-muted">已发布 generation</div>
        <div className="min-w-0 truncate font-mono text-xs text-secondary" title={published.generation}>
          {published.generation || '—'}
        </div>
      </div>
      <div className="grid grid-cols-1 gap-2 sm:grid-cols-3">
        <Metric label="覆盖范围" value={`${fmtDate(published.earliest_date)} — ${fmtDate(published.latest_date)}`} />
        <Metric label="数据规模" value={`${countText(published.row_count)} 行 · ${countText(published.symbols)} 标的`} />
        <Metric label="交易日" value={`${countText(published.trading_days)} 日 · ${fmtDate(published.created_at)} 发布`} />
      </div>
    </div>
  )
}

function BackfillJob({
  job,
  progress,
}: {
  job: CanonicalHistoryJob
  progress: number
}) {
  const isActive = !!ACTIVE_JOB_STATUSES[job.status]
  const isFailed = job.status === 'failed'
  const isSucceeded = job.status === 'succeeded'
  const statusText = job.status === 'pending'
    ? '等待执行'
    : job.status === 'running'
      ? '正在回填'
      : isSucceeded
        ? '最近一次回填完成'
        : '最近一次回填失败'

  return (
    <div className={`rounded-btn border px-3 py-3 ${
      isFailed ? 'border-danger/30 bg-danger/5' : isSucceeded ? 'border-accent/25 bg-accent/[0.03]' : 'border-border bg-base/30'
    }`}>
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          {isActive ? (
            <Loader2 className="h-3.5 w-3.5 animate-spin text-accent" />
          ) : isSucceeded ? (
            <CheckCircle2 className="h-3.5 w-3.5 text-accent" />
          ) : (
            <AlertTriangle className="h-3.5 w-3.5 text-danger" />
          )}
          <span className="text-xs font-medium text-foreground">{statusText}</span>
        </div>
        <span className="font-mono text-[10px] text-muted">回填批次 {job.id || '—'}</span>
      </div>

      {isActive && (
        <div className="mt-3 space-y-1.5">
          <div className="flex items-center justify-between gap-3 text-[10px] text-muted">
            <span>已处理 {countText(job.processed_symbols)} / {countText(job.total_symbols)} 标的</span>
            <span className="font-mono text-secondary">{progress}%</span>
          </div>
          <div
            className="h-1 overflow-hidden rounded-full bg-elevated"
            role="progressbar"
            aria-label="全历史回填进度"
            aria-valuemin={0}
            aria-valuemax={100}
            aria-valuenow={progress}
          >
            <div className="h-full rounded-full bg-accent transition-[width] duration-300" style={{ width: `${progress}%` }} />
          </div>
        </div>
      )}

      <div className="mt-2 grid grid-cols-1 gap-x-4 gap-y-1 text-[10px] sm:grid-cols-3">
        <span className="text-muted">已写入 <span className="font-mono text-secondary">{countText(job.written_rows)} 行</span></span>
        <span className="text-muted">开始 <span className="font-mono text-secondary">{fmtDate(job.started_at)}</span></span>
        <span className="text-muted">结束 <span className="font-mono text-secondary">{fmtDate(job.finished_at)}</span></span>
      </div>

      {job.error && (
        <p className="mt-2 break-words border-t border-danger/20 pt-2 text-[11px] leading-relaxed text-danger" role="alert">
          {job.error}
        </p>
      )}
    </div>
  )
}

function Metric({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-btn border border-border bg-base/30 px-3 py-2">
      <div className="text-[10px] text-muted">{label}</div>
      <div className="mt-0.5 truncate font-mono text-[11px] text-secondary" title={value}>{value}</div>
    </div>
  )
}

import { useEffect, useId, useState } from 'react'
import { useMutation, useQuery, useQueryClient, type UseQueryResult } from '@tanstack/react-query'
import { Link, useSearchParams } from 'react-router-dom'
import { ArrowUpRight, ChevronRight, Copy, Database, Plus, RefreshCw, RotateCcw, Square, Workflow } from 'lucide-react'
import { EmptyState } from '@/components/EmptyState'
import { PageHeader } from '@/components/PageHeader'
import { api, ApiError } from '@/lib/api'
import { cn } from '@/lib/cn'
import { QK } from '@/lib/queryKeys'
import type { EngineJobPreview, EngineJobRecord, EngineJobRequest, EngineJobStatus, RepositorySourceIssue } from '@/lib/engineJobTypes'
import { JobForm } from './JobForm'
import { Report } from './Report'
import { Schedules } from './Schedules'
import { SnapshotForm } from './SnapshotForm'
import { Button, buttonClass, ErrorNotice, inputClass, LoadingLines, workspaceClass, workspaceRowClass, workspaceSidebarClass } from './ui'
import { active, ERROR_HINTS, orderedStages, retryable, stageLabel, STATUS, taskLabel, timeLabel } from './viewModel'

const viewTabClass = 'min-h-7 cursor-pointer whitespace-nowrap rounded-full px-1 text-xs font-medium text-secondary transition-colors hover:text-foreground focus-visible:outline focus-visible:outline-2 focus-visible:outline-accent aria-[current=page]:bg-accent/15 aria-[current=page]:text-accent md:px-2.5 [@media(pointer:coarse)]:min-h-8'
const taskTimeOptions: Intl.DateTimeFormatOptions = {
  year: 'numeric', month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit',
}

function sourceFailureMessage(issue: RepositorySourceIssue) {
  const subject = `${issue.trading_date} 的 ${issue.symbol}`
  if (issue.field === 'ex_factor') {
    return `${subject} 原始除权比率超出可保存精度。展开尝试记录可定位源文件和行号，核实来源后请重新预检。`
  }
  return issue.field === 'quote_ts'
    ? `${subject} 缺少已定版日线。展开尝试记录可查看报价时间问题，补采后请重新预检并提交新任务。`
    : `${subject} 日线数值不符合要求。展开尝试记录可定位字段，修正来源后请重新预检并提交新计划。`
}

function SourceIssues({ issues }: { issues: RepositorySourceIssue[] }) {
  const fileLabels = {
    daily_raw_v1: 'kline_daily 分区',
    enriched_v1: 'kline_daily_enriched 分区',
    event_factors_v1: 'adj_factor/all.parquet',
  }
  const reasons = {
    fractional_precision: '有效小数超过 12 位',
    integral_range: '整数部分超过 26 位',
    invalid_number: '数值缺失或无效',
    quote_date_mismatch: '报价的北京时间日期与分区日期不同',
    quote_before_finalization: '报价早于北京时间 15:30 定版线',
    quote_after_observation: '报价时间晚于本次采集时刻',
  }
  const rows = new Map<string, RepositorySourceIssue[]>()
  for (const issue of issues) {
    const key = [issue.layout, issue.trading_date, issue.file_sha256, issue.row_number].join('/')
    const row = rows.get(key)
    if (row) row.push(issue)
    else rows.set(key, [issue])
  }
  return (
    <ul aria-label="源数据问题" className="space-y-1 text-secondary">
      {Array.from(rows, ([key, fields]) => (
        <li key={key} className="break-words">
          {fields[0].trading_date} · {fields[0].symbol} · 第 {fields[0].row_number} 行
          <ul>
            {fields.map((issue) => (
              <li key={issue.field}><code>{issue.field}</code>，{reasons[issue.reason]}</li>
            ))}
          </ul>
          <span className="block break-all">
            {fileLabels[fields[0].layout]} SHA-256 <code>{fields[0].file_sha256}</code>
          </span>
        </li>
      ))}
    </ul>
  )
}

function unavailable(error: unknown) {
  return error instanceof ApiError && [401, 403, 503].includes(error.status)
}

function Status({ status }: { status: EngineJobStatus }) {
  return (
    <span
      className={cn(
        'inline-flex shrink-0 items-center gap-1 whitespace-nowrap rounded-full px-1.5 py-0.5 text-xs font-medium',
        status === 'succeeded'
          ? 'bg-emerald-500/10 text-emerald-800 dark:text-emerald-300'
          : status === 'failed'
            ? 'bg-danger/10 text-red-700 dark:text-red-300'
            : active(status)
              ? 'bg-accent/10 text-blue-700 dark:text-blue-300'
              : 'bg-elevated text-secondary',
      )}
    >
      <span aria-hidden="true" className="size-1.5 rounded-full bg-current" />
      {STATUS[status] ?? '未知状态'}
    </span>
  )
}

function Stages({ job }: { job: EngineJobRecord }) {
  const stages = orderedStages(job)
  const completed = stages.filter(([, node]) => node.status === 'succeeded').length
  return (
    <details open={active(job.status) || retryable(job.status)} className="group border-y border-border">
      <summary className="flex min-h-8 cursor-pointer list-none items-center justify-between gap-2 px-1 text-[13px] hover:text-foreground focus-visible:outline focus-visible:outline-2 focus-visible:outline-accent [@media(pointer:coarse)]:min-h-9 [&::-webkit-details-marker]:hidden">
        <span className="flex items-center gap-2 font-medium">
          <ChevronRight aria-hidden="true" className="size-4 text-secondary transition-transform group-open:rotate-90 motion-reduce:transition-none" />
          执行阶段
        </span>
        <span className="text-xs text-secondary tabular-nums">{completed} / {stages.length} 已完成</span>
      </summary>
      <ol className="divide-y divide-border border-t border-border/70 px-2">
        {stages.map(([id, node]) => {
          const latest = node.attempts.at(-1)
          const progress = latest?.progress
          return (
            <li key={id} className="py-1.5">
              <div className="flex flex-wrap items-center justify-between gap-2 text-sm">
                <span className="min-w-0 flex-1 break-all">{stageLabel(id)}</span>
                <Status status={node.status} />
              </div>
              {progress && (
                <p className="mt-1 break-words font-mono text-xs text-secondary">
                  {progress.phase} · {progress.done} / {progress.total}
                </p>
              )}
              {latest?.error && (
                <p role="status" className="mt-2 text-sm text-red-700 dark:text-danger">
                  {latest.error.source_issues?.length
                    ? sourceFailureMessage(latest.error.source_issues[0])
                    : ERROR_HINTS[latest.error.code] ?? '阶段未完成，请查看尝试记录。'}
                </p>
              )}
              {node.attempts.length > 0 && (
                <details className="mt-1 text-xs text-secondary">
                  <summary className="min-h-6 w-fit cursor-pointer py-1 focus-visible:outline-accent">
                    尝试记录（{node.attempts.length}）
                  </summary>
                  <ol className="space-y-2 border-l border-border pl-3">
                    {node.attempts.map((attempt) => (
                      <li key={attempt.number} className="space-y-1 py-1">
                        <p>
                          第 {attempt.number} 次 · {STATUS[attempt.status] ?? '未知状态'}
                        </p>
                        <p>开始：{timeLabel(attempt.started_at)}</p>
                        {attempt.finished_at && <p>结束：{timeLabel(attempt.finished_at)}</p>}
                        {attempt.error && (
                          <p className="break-all font-mono text-red-700 dark:text-danger">
                            {attempt.error.code}
                          </p>
                        )}
                        {attempt.error?.source_issues?.length
                          ? <SourceIssues issues={attempt.error.source_issues} />
                          : null}
                      </li>
                    ))}
                  </ol>
                </details>
              )}
            </li>
          )
        })}
      </ol>
    </details>
  )
}

export function JobDetail({
  id,
  job,
  nodeId = '',
  onNode,
  onCopy,
}: {
  id: string
  job: Pick<UseQueryResult<EngineJobRecord>, 'data' | 'error' | 'isPending' | 'isFetching' | 'refetch'>
  nodeId?: string
  onNode?: (nodeId: string) => void
  onCopy?: (request: EngineJobRequest) => void
}) {
  const client = useQueryClient()
  const plan = useQuery({
    queryKey: QK.engineJobPlan(id),
    queryFn: () => api.engineJobPlan(id),
    retry: false,
  })
  const nodes = plan.data?.request.nodes ?? []
  // Explicit deep links never fall back to a different node's report.
  const selectedNode = nodeId || nodes[0]?.node_id || ''
  const capture = nodes.find((node) => node.node_id === selectedNode)?.capture
  const snapshotReady = !!capture && job.data?.nodes[`${selectedNode}.snapshot`]?.status === 'succeeded'
  const snapshot = useQuery({
    queryKey: QK.engineJobSnapshot(id, selectedNode),
    queryFn: () => api.engineJobSnapshot(id, selectedNode),
    enabled: snapshotReady && !unavailable(job.error),
    retry: false,
  })
  const reportReady = !!selectedNode && job.data?.nodes[`${selectedNode}.report`]?.status === 'succeeded'
  const report = useQuery({
    queryKey: QK.engineJobReport(id, selectedNode),
    queryFn: () => api.engineJobReport(id, selectedNode),
    enabled: reportReady && !unavailable(job.error),
    retry: false,
  })
  const action = useMutation({
    mutationFn: (kind: 'cancel' | 'retry') =>
      kind === 'cancel' ? api.cancelEngineJob(id) : api.retryEngineJob(id),
    onSuccess: async (result) => {
      client.setQueryData(QK.engineJob(id), result)
      await client.invalidateQueries({ queryKey: QK.engineJobsRoot })
    },
  })
  if (job.isPending) return <LoadingLines label="正在读取研究任务" />
  if (!job.data || unavailable(job.error))
    return (
      <div className="p-4 sm:p-5">
        <ErrorNotice error={job.error}>
          <Button onClick={() => void job.refetch()} disabled={job.isFetching}>
            重新读取任务
          </Button>
        </ErrorNotice>
      </div>
    )
  const state = job.data
  const needsNewObservation = nodes.some((node) => (node.capture ?? node.prepare)?.datasets.some((source, index) => {
    const stage = state.nodes[`${node.node_id}.sync_${String(index).padStart(2, '0')}`]
    return source.kind && source.kind !== 'local_jsonl' && stage && stage.status !== 'succeeded' && stage.attempts.length > 0
  }))
  const maxAttempts = plan.data?.effective_budget?.max_attempts ?? plan.data?.request.budget?.max_attempts
  const exhausted =
    maxAttempts !== undefined &&
    Object.values(state.nodes).some(
      (node) => node.status !== 'succeeded' && node.attempts.length >= maxAttempts,
    )
  const disabled = action.isPending || unavailable(plan.error) || unavailable(action.error)
  const publishedCapture = snapshotReady && !!snapshot.data && !snapshot.error
  const backtestScope = snapshot.data?.backtest
  return (
    <article className="w-full min-w-0 max-w-6xl space-y-3 p-3 sm:px-4" aria-labelledby="research-task-title">
      <div className="flex flex-wrap items-center gap-x-3">
        <div className="mr-auto flex min-w-0 flex-wrap items-center gap-2">
          <h2 id="research-task-title" className="text-[16px] font-semibold">
            {taskLabel(state)}
          </h2>
          <span role="status">
            <Status status={state.status} />
          </span>
        </div>
        <div className="flex flex-wrap gap-1.5">
          {active(state.status) && (
            <Button
              variant="danger"
              disabled={disabled || state.status === 'cancelling'}
              onClick={() => action.mutate('cancel')}
            >
              <Square aria-hidden="true" className="h-3.5 w-3.5" />
              {state.status === 'cancelling' ? '正在取消…' : action.isPending ? '正在请求…' : '取消任务'}
            </Button>
          )}
          {retryable(state.status) && (
            <Button disabled={disabled || !plan.data || exhausted || needsNewObservation} onClick={() => action.mutate('retry')}>
              <RotateCcw aria-hidden="true" className="h-4 w-4" />
              {action.isPending ? '正在请求…' : '按原输入重试'}
            </Button>
          )}
          {onCopy && <Button variant="quiet" disabled={!plan.data || disabled} onClick={() => plan.data && onCopy(plan.data.request)}>
            <Copy aria-hidden="true" className="size-3.5" />
            复制为新任务
          </Button>}
        </div>
        <div className="flex w-full flex-wrap items-baseline gap-x-3 text-xs text-secondary">
          <p className="tabular-nums">
            更新于 <time dateTime={state.updated_at}>{timeLabel(state.updated_at)}</time>
          </p>
          <details className="group min-w-0">
            <summary className="inline-flex min-h-6 cursor-pointer list-none items-center gap-1 rounded-btn hover:text-foreground focus-visible:outline focus-visible:outline-accent [&::-webkit-details-marker]:hidden">
              <ChevronRight aria-hidden="true" className="size-3.5 transition-transform group-open:rotate-90 motion-reduce:transition-none" />
              任务信息
            </summary>
            <p className="break-all">任务标识 <code>{id}</code></p>
            <p className="mt-1">创建于 <time dateTime={state.created_at}>{timeLabel(state.created_at)}</time></p>
          </details>
        </div>
      </div>
      {job.error && (
        <ErrorNotice error={job.error}>
          <p>当前显示上次读取的状态。</p>
          <Button disabled={job.isFetching} onClick={() => void job.refetch()}>
            重新读取任务
          </Button>
        </ErrorNotice>
      )}
      {plan.error && (
        <ErrorNotice error={plan.error}>
          <Button disabled={plan.isFetching} onClick={() => void plan.refetch()}>
            重新读取计划
          </Button>
        </ErrorNotice>
      )}
      {action.error && (
        <ErrorNotice error={action.error}>
          {unavailable(action.error) && (
            <Button
              disabled={job.isFetching || plan.isFetching}
              onClick={() => {
                void Promise.all([job.refetch(), plan.refetch()]).then((results) => {
                  if (results.every((result) => !result.isError)) action.reset()
                })
              }}
            >
              重新检查服务与权限
            </Button>
          )}
        </ErrorNotice>
      )}
      {state.status === 'cancelling' && (
        <p role="status" className="text-sm leading-6 text-secondary">
          已请求取消，正在等待执行进程停止。已发布的结果会保留。
        </p>
      )}
      {exhausted && retryable(state.status) && (
        <p className="text-sm leading-6 text-secondary">
          未完成阶段已达到尝试上限。可以复制为新任务，检查后重新运行。
        </p>
      )}
      {needsNewObservation && retryable(state.status) && (
        <p className="text-sm leading-6 text-secondary">上次行情采集没有形成可复用的数据。使用“复制为新任务”重新采集。</p>
      )}
      {state.error && (
        <ErrorNotice
          error={ERROR_HINTS[state.error.code] ?? `任务未完成（${state.error.code}），请检查执行阶段。`}
        />
      )}
      {!publishedCapture && <Stages job={state} />}
      {(nodes.length > 1 || (nodeId && plan.data && !nodes.some((node) => node.node_id === nodeId))) && (
        <div className="space-y-2">
          <label htmlFor="research-node" className="text-sm font-medium">
            研究步骤
          </label>
          <select
            id="research-node"
            className={inputClass}
            value={selectedNode}
            onChange={(event) => onNode?.(event.target.value)}
          >
            {nodeId && !nodes.some((node) => node.node_id === nodeId) && (
              <option value={nodeId}>不存在的研究步骤</option>
            )}
            {nodes.map((node) => (
              <option value={node.node_id} key={node.node_id}>
                {node.node_id}
              </option>
            ))}
          </select>
        </div>
      )}
      {capture ? (
        <section aria-label="采集快照" className="space-y-2 text-sm">
          {!snapshotReady ? <p className="text-secondary">此步骤尚未发布快照。</p>
            : snapshot.error ? <ErrorNotice error={snapshot.error}>
              <Button disabled={snapshot.isFetching} onClick={() => void snapshot.refetch()}>重新读取快照</Button>
            </ErrorNotice>
            : snapshot.data ? <>
              <h3 className="text-sm font-semibold text-foreground">已发布数据快照</h3>
              {backtestScope && (
                <div className="flex flex-wrap items-center justify-between gap-x-3 gap-y-1.5 rounded-btn border border-border bg-base/45 px-2.5 py-1.5">
                  <div className="min-w-0 sm:flex sm:flex-wrap sm:items-baseline sm:gap-x-3">
                    <p className="break-words font-semibold tabular-nums">
                      {backtestScope.symbols.slice(0, 2).join('、')}
                      {backtestScope.symbols.length > 2 && ` 等 ${backtestScope.symbols.length} 只股票`}
                    </p>
                    <p className="text-xs tabular-nums text-secondary">
                      {backtestScope.start} 至 {backtestScope.end}
                    </p>
                  </div>
                  <Link
                    to={`/backtest?${new URLSearchParams({ snapshotJob: id, snapshotNode: selectedNode })}`}
                    className={cn(buttonClass('primary'), 'shrink-0')}
                  >
                    用此快照回测
                    <ArrowUpRight aria-hidden="true" className="size-3.5" />
                  </Link>
                </div>
              )}
              {!backtestScope && capture.datasets.some(item => item.kind === 'tsp_repository_factors') && (
                <p className="text-secondary">策略回测需要同一范围的原始日K和完整复权因子。可按下方覆盖信息补齐数据，再生成快照。</p>
              )}
              <ul aria-label="快照数据覆盖" className="divide-y divide-border">
                {snapshot.data.datasets.map((dataset) => (
                  <li key={dataset.version_id} className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-0.5 py-1.5">
                    <span className="min-w-0 break-words font-medium sm:w-24" title={dataset.dataset_id}>
                      {dataset.dataset_id === 'daily-raw' ? '原始日K' : dataset.dataset_id === 'factors' ? '复权因子' : dataset.dataset_id}
                    </span>
                    <p className="order-last w-full min-w-0 break-words text-xs text-secondary tabular-nums sm:order-none sm:w-auto sm:flex-1">
                      <span title={dataset.source_ids.join('、')}>
                        {dataset.source_ids.map(source => source === 'local_archive' ? '本地行情' : source).join('、')}
                      </span>
                      {' · '}{dataset.coverage.row_count} 行
                      {' · '}{({ latest_reconstruction: '按采集时点重建', historical_versions: '保留历史版本', unknown: '历史可见性未知' })[dataset.knowledge_basis]}
                    </p>
                    <span className={cn('ml-auto text-right text-xs tabular-nums', dataset.coverage.status === 'complete' ? 'text-secondary' : 'text-red-700 dark:text-red-300')}>
                      {({ complete: '覆盖完整', partial: '覆盖不完整', empty: '无行情', error: '采集失败' })[dataset.coverage.status]}
                      {' · '}{dataset.coverage.completed_units}/{dataset.coverage.requested_units} 个覆盖单位
                    </span>
                    {dataset.coverage.issues?.[0] && <p className="order-last w-full break-words text-xs text-secondary">
                      {dataset.coverage.issues[0].instrument_id && `${dataset.coverage.issues[0].instrument_id} · `}{dataset.coverage.issues[0].detail}
                    </p>}
                  </li>
                ))}
              </ul>
              <details className="group text-xs text-secondary">
                <summary className="inline-flex min-h-6 cursor-pointer list-none items-center gap-1 hover:text-foreground focus-visible:outline focus-visible:outline-accent [&::-webkit-details-marker]:hidden">
                  <ChevronRight aria-hidden="true" className="size-3.5 transition-transform group-open:rotate-90 motion-reduce:transition-none" />
                  版本与存储位置
                </summary>
                <p className="mt-1 break-all">快照标识 <code>{snapshot.data.snapshot_id}</code></p>
                <p className="mt-1 break-all">工作区路径 <code>research/engine/{snapshot.data.path}</code></p>
              </details>
            </> : <LoadingLines label="正在校验并读取快照" />}
        </section>
      ) : reportReady ? (
        <>
          {report.isPending && <LoadingLines label="正在校验并读取报告" />}
          {report.error ? (
            <ErrorNotice error={report.error}>
              <Button disabled={report.isFetching} onClick={() => void report.refetch()}>
                重新读取报告
              </Button>
            </ErrorNotice>
          ) : (
            report.data && <Report key={`${report.data.plan_id}/${report.data.node_id}`} report={report.data} />
          )}
        </>
      ) : (
        <div className="py-5 text-sm leading-6 text-secondary">
          {nodeId && plan.data && !nodes.some((node) => node.node_id === nodeId)
            ? '链接中的研究步骤不存在，请从研究步骤中重新选择。'
            : state.status === 'failed' || state.status === 'interrupted' || state.status === 'cancelled'
              ? '此研究步骤尚未发布报告。可以检查失败阶段，再按原输入重试。'
              : '报告将在生成并校验成功后显示。进度完成不代表结果已发布。'}
        </div>
      )}
      {publishedCapture && <Stages job={state} />}
    </article>
  )
}

export function EngineResearch() {
  const client = useQueryClient()
  const [params, setParams] = useSearchParams()
  const [form, setForm] = useState<{ kind: 'job'; initial?: EngineJobRequest } | { kind: 'snapshot' } | null>(null)
  const [scheduleTemplate, setScheduleTemplate] = useState<EngineJobPreview | null>(null)
  const [taskListOpen, setTaskListOpen] = useState(false)
  const taskListId = useId()
  const showingSchedules = params.get('view') === 'schedules'
  const jobs = useQuery({
    queryKey: QK.engineJobs(100),
    queryFn: () => api.engineJobs(100),
    enabled: !showingSchedules,
    retry: false,
    refetchInterval: (query) =>
      unavailable(query.state.error)
        ? false
        : query.state.data?.items.some((job) => active(job.status))
          ? 3000
          : 10000,
  })
  const listed = unavailable(jobs.error) ? [] : (jobs.data?.items ?? [])
  const selected = params.get('job') || listed[0]?.job_id || ''
  const listedSelection = listed.find((item) => item.job_id === selected)
  const selectedQuery = useQuery({
    queryKey: QK.engineJob(selected),
    queryFn: () => api.engineJob(selected),
    enabled: !!selected && !showingSchedules,
    retry: false,
    refetchInterval: (query) =>
      unavailable(query.state.error)
        ? false
        : active(query.state.data?.status ?? 'succeeded') || active(listedSelection?.status ?? 'succeeded')
          ? 2500
          : false,
  })
  // Both panes use the newest acknowledged record, even when polls arrive out of order.
  const latest =
    selectedQuery.data && listedSelection && listedSelection.updated_at > selectedQuery.data.updated_at
      ? listedSelection
      : selectedQuery.data
  const items = listed.map((item) => (item.job_id === selected && latest ? latest : item))
  useEffect(() => {
    if (!showingSchedules && !params.get('job') && selected)
      setParams(
        (previous) => {
          if (previous.get('job')) return previous
          const next = new URLSearchParams(previous)
          next.set('job', selected)
          return next
        },
        { replace: true },
      )
  }, [params, selected, setParams, showingSchedules])
  function select(id: string, nodeId = '') {
    setParams((previous) => {
      const next = new URLSearchParams(previous)
      next.delete('view')
      next.delete('schedule')
      next.set('job', id)
      if (nodeId) next.set('node', nodeId)
      else next.delete('node')
      return next
    })
    setForm(null)
    setTaskListOpen(false)
  }
  function schedules(id = '', template: EngineJobPreview | null = null) {
    setForm(null)
    setScheduleTemplate(template)
    setParams(previous => {
      const next = new URLSearchParams(previous)
      next.set('view', 'schedules')
      if (id) next.set('schedule', id)
      else next.delete('schedule')
      return next
    })
  }
  function showJobs(edit: boolean) {
    setScheduleTemplate(null)
    setForm(edit ? { kind: 'job' } : null)
    setParams(previous => {
      const next = new URLSearchParams(previous)
      next.delete('view')
      next.delete('schedule')
      return next
    })
  }
  function created(job: EngineJobRecord) {
    client.setQueryData(QK.engineJob(job.job_id), job)
    void client.invalidateQueries({ queryKey: QK.engineJobsRoot })
    select(job.job_id)
  }
  return (
    <div className="flex h-full min-h-0 min-w-0 flex-col bg-base text-foreground">
      <PageHeader
        title="引擎研究"
        className="shrink-0 max-md:flex-wrap max-md:gap-1 max-md:px-3 max-md:[&>div:first-child]:min-h-10 max-md:[&>div:first-child]:pl-[3.25rem]"
        right={
          <div className="flex min-w-0 flex-1 flex-wrap items-center justify-between gap-1.5 max-md:basis-full md:gap-2">
            <nav aria-label="引擎研究视图" className="flex shrink-0 items-center rounded-full bg-elevated/60 p-0.5">
              <button type="button" aria-current={!showingSchedules ? 'page' : undefined} className={viewTabClass}
                onClick={() => { if (showingSchedules) showJobs(false) }}>
                研究任务
              </button>
              <button type="button" aria-current={showingSchedules ? 'page' : undefined} className={viewTabClass}
                onClick={() => { if (!showingSchedules) schedules() }}>
                触发计划
              </button>
            </nav>
            {!showingSchedules && !form && (
              <div role="group" aria-label="新建研究" className="ml-auto flex flex-wrap justify-end gap-1.5 max-md:[&_button]:px-1.5 max-md:[&_button]:text-xs">
                <Button disabled={unavailable(jobs.error)} onClick={() => showJobs(true)}>
                  <Plus aria-hidden="true" className="hidden size-3.5 min-[400px]:block" />
                  新建任务
                </Button>
                <Button variant="primary" disabled={unavailable(jobs.error)} onClick={() => {
                  showJobs(false)
                  setForm({ kind: 'snapshot' })
                }}>
                  <Database aria-hidden="true" className="hidden size-3.5 min-[400px]:block" />
                  生成日K快照
                </Button>
              </div>
            )}
          </div>
        }
      />
      <div className="min-h-0 flex-1 p-2 md:px-5">
      {showingSchedules ? <Schedules id={params.get('schedule') || ''} template={scheduleTemplate}
        onSelect={id => schedules(id)} onTemplate={() => showJobs(true)} onJob={id => select(id)} /> :
      <div className={workspaceClass}>
        <aside
          aria-label="研究任务列表"
          className={cn(workspaceSidebarClass, form && 'hidden lg:block')}
        >
          <div className="flex items-center justify-between gap-2 px-2.5 py-1">
            <h2 className="hidden items-center gap-2 text-sm font-semibold lg:flex">
              最近任务 <span className="text-xs font-normal text-secondary tabular-nums">{items.length} 项</span>
            </h2>
            <Button variant="quiet" compact className="-ml-2 lg:hidden"
              aria-expanded={taskListOpen} aria-controls={taskListId} onClick={() => setTaskListOpen(open => !open)}>
              <ChevronRight aria-hidden="true" className={cn('size-3.5', taskListOpen && 'rotate-90')} />
              最近任务 <span className="text-secondary tabular-nums">{items.length} 项</span>
            </Button>
            <Button variant="quiet" compact disabled={jobs.isFetching} aria-busy={jobs.isFetching}
              onClick={() => void client.invalidateQueries({ queryKey: QK.engineJobsRoot })}>
              <RefreshCw aria-hidden="true" className={cn('size-3.5', jobs.isFetching && 'animate-spin motion-reduce:animate-none')} />
              刷新
            </Button>
          </div>
          {jobs.error && (
            <div className="px-3 pb-3">
              <ErrorNotice error={jobs.error}>
                {items.length > 0 && <p>保留上次读取的列表。</p>}
                <Button disabled={jobs.isFetching} onClick={() => void jobs.refetch()}>
                  重新读取列表
                </Button>
              </ErrorNotice>
            </div>
          )}
          <div id={taskListId} className={cn(!taskListOpen && 'hidden lg:block')}>
          {jobs.isPending ? (
            <LoadingLines label="正在读取任务列表" />
          ) : items.length ? (
            <ul className="max-h-56 space-y-0.5 overflow-y-auto px-1.5 pb-1.5 lg:max-h-none lg:overflow-visible">
              {items.map((job) => {
                const nodes = Object.values(job.nodes)
                return (
                  <li key={job.job_id}>
                    <button
                      type="button"
                      aria-current={!form && selected === job.job_id ? 'true' : undefined}
                      onClick={() => select(job.job_id)}
                      className={workspaceRowClass}
                    >
                      <span className="flex items-center justify-between gap-2">
                        <span className="text-sm font-medium">{taskLabel(job)}</span>
                        <Status status={job.status} />
                      </span>
                      <span className="mt-0.5 flex flex-wrap items-center gap-x-1.5 text-xs text-secondary tabular-nums">
                        <code title={job.job_id}>{job.job_id.slice(0, 10)}</code>
                        <time dateTime={job.created_at} title={timeLabel(job.created_at)}>
                          {timeLabel(job.created_at, taskTimeOptions)}
                        </time>
                      </span>
                      <span className="block text-xs text-secondary tabular-nums">
                        {nodes.filter((node) => node.status === 'succeeded').length} / {nodes.length}{' '}
                        个阶段已完成
                      </span>
                    </button>
                  </li>
                )
              })}
            </ul>
          ) : (
            !jobs.error && (
              <p className="px-4 pb-5 text-sm leading-6 text-secondary">
                暂无任务记录。
              </p>
            )
          )}
          </div>
        </aside>
        <section aria-label="研究详情" className="min-h-0 min-w-0 lg:overflow-y-auto">
          {form?.kind === 'snapshot' ? (
            <SnapshotForm onCreated={created} onClose={() => setForm(null)} />
          ) : form ? (
            <JobForm initial={form.initial} onCreated={created} onClose={() => setForm(null)} onSchedule={preview => schedules('', preview)} />
          ) : selected ? (
            <JobDetail
              key={selected}
              id={selected}
              job={{ ...selectedQuery, data: latest }}
              nodeId={params.get('node') ?? ''}
              onNode={(node) => select(selected, node)}
              onCopy={(initial) => setForm({ kind: 'job', initial })}
            />
          ) : jobs.isPending ? (
            <LoadingLines label="正在读取研究工作台" />
          ) : (
            !jobs.error && (
              <EmptyState
                icon={Workflow}
                title="还没有研究任务"
                hint="从本地日K生成快照，或者新建任务并选择已有回测归档。"
              />
            )
          )}
        </section>
      </div>}
      </div>
    </div>
  )
}

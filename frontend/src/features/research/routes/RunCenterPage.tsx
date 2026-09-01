import { useMemo, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { PageHeader } from '@/components/PageHeader'
import { ControlSelect } from '@/components/ui/Primitives'
import { HttpErrorState, LoadingState } from '../components/QueryState'
import { RunActions } from '../components/RunProgress'
import { JobStatusBadge, PromotionBadge, VerdictBadge } from '../components/StatusBadges'
import { useRunList } from '../hooks/useResearchQueries'
import { useCancelRun, useCreateRun, usePatchRun } from '../hooks/useRunMutations'
import { fmtDateTime, fmtDuration } from '../lib/format'
import { scopeLabel, windowLabel } from '../model/run'
import type { RunScope } from '../model/status'

export function RunCenterPage() {
  const navigate = useNavigate()
  const [jobStatus, setJobStatus] = useState('')
  const [verdict, setVerdict] = useState('')
  const [scopeType, setScopeType] = useState('')
  const [favoriteOnly, setFavoriteOnly] = useState(false)
  const list = useRunList({
    job_status: jobStatus || undefined,
    verdict: verdict || undefined,
    scope_type: scopeType || undefined,
    favorite: favoriteOnly || undefined,
    limit: 50,
  })
  const items = useMemo(() => list.data?.pages.flatMap((page) => page.items) ?? [], [list.data])
  const cancel = useCancelRun()
  const patch = usePatchRun()
  const create = useCreateRun()

  return (
    <div className="workspace-page h-full min-h-0 overflow-auto">
      <PageHeader title="运行中心" subtitle="单一 run_id；不可变事实；仅 label / favorite 可 PATCH" />
      <div className="workspace-content space-y-3">
        <div className="workspace-toolbar">
          <ControlSelect className="min-h-11 text-xs" value={jobStatus} onChange={(event) => setJobStatus(event.target.value)}>
            <option value="">全部工程状态</option>
            <option value="pending">排队</option>
            <option value="running">运行中</option>
            <option value="interrupted">已中断</option>
            <option value="completed">已完成</option>
            <option value="failed">失败</option>
            <option value="cancelled">已取消</option>
          </ControlSelect>
          <ControlSelect className="min-h-11 text-xs" value={verdict} onChange={(event) => setVerdict(event.target.value)}>
            <option value="">全部裁决</option>
            <option value="accepted">接受</option>
            <option value="rejected">拒绝</option>
            <option value="unavailable">不可用</option>
            <option value="inconclusive">无结论</option>
          </ControlSelect>
          <ControlSelect className="min-h-11 text-xs" value={scopeType} onChange={(event) => setScopeType(event.target.value)}>
            <option value="">全部范围</option>
            <option value="symbols">标的</option>
            <option value="full_market">全市场</option>
          </ControlSelect>
          <label className="inline-flex min-h-11 items-center gap-2 text-xs text-secondary">
            <input type="checkbox" className="h-4 w-4 accent-accent" checked={favoriteOnly} onChange={(event) => setFavoriteOnly(event.target.checked)} />
            仅收藏
          </label>
        </div>
        {list.isPending ? <LoadingState label="读取运行历史" /> : null}
        {list.isError ? <HttpErrorState error={list.error} onRetry={() => void list.refetch()} /> : null}
        {list.data && items.length === 0 ? <p className="text-sm text-muted">没有匹配的运行。从因子工作台创建第一次研究。</p> : null}
        <div className="data-table-scroll">
          <table className="data-table min-w-[64rem]">
            <thead>
              <tr>
                <th>Run ID</th>
                <th>因子</th>
                <th>Scope</th>
                <th>Window</th>
                <th>Status</th>
                <th>Verdict</th>
                <th>晋级</th>
                <th>Samples</th>
                <th>Baseline</th>
                <th>Created</th>
                <th>Duration</th>
                <th>操作</th>
              </tr>
            </thead>
            <tbody>
              {items.map((run) => (
                <tr key={run.run_id}>
                  <td>
                    <Link className="font-mono text-xs text-accent hover:underline" to={`/research/runs/${encodeURIComponent(run.run_id)}`}>{run.run_id}</Link>
                    {run.label ? <p className="text-[11px] text-muted">{run.label}</p> : null}
                  </td>
                  <td>
                    <Link className="text-sm hover:text-accent" to={`/research/factors/${encodeURIComponent(run.factor_id)}`}>{run.factor_title ?? run.factor_id}</Link>
                  </td>
                  <td className="font-mono text-xs">{scopeLabel(run.scope)}</td>
                  <td className="font-mono text-[11px]">{windowLabel(run.window)}</td>
                  <td><JobStatusBadge value={run.job_status} /></td>
                  <td><VerdictBadge value={run.verdict} /></td>
                  <td><PromotionBadge value={run.promotion_status} /></td>
                  <td className="num">{run.sample_count ?? '—'}</td>
                  <td className="font-mono text-xs">{run.baseline ?? '—'}</td>
                  <td className="font-mono text-xs">{fmtDateTime(run.created_at)}</td>
                  <td className="font-mono text-xs">{fmtDuration(run.duration_ms)}</td>
                  <td>
                    <RunActions
                      runId={run.run_id}
                      jobStatus={run.job_status}
                      favorite={run.favorite}
                      label={run.label}
                      canCancel
                      cancelPending={cancel.isPending && cancel.variables === run.run_id}
                      patchPending={patch.isPending}
                      onCancel={() => cancel.mutate(run.run_id)}
                      onFavorite={(favorite) => patch.mutate({ runId: run.run_id, favorite })}
                      onLabel={(label) => patch.mutate({ runId: run.run_id, label })}
                      onRerun={() => {
                        if (!run.scope) return
                        void create.mutateAsync({
                          factor_id: run.factor_id,
                          scope: run.scope as RunScope,
                          parameters: run.parameters,
                          source_run_id: run.run_id,
                        }).then((created) => navigate(`/research/runs/${encodeURIComponent(created.run_id)}`))
                      }}
                    />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {list.hasNextPage ? (
          <button type="button" className="btn-secondary min-h-11 text-xs" onClick={() => void list.fetchNextPage()} disabled={list.isFetchingNextPage}>
            {list.isFetchingNextPage ? '加载中…' : '加载更多'}
          </button>
        ) : null}
      </div>
    </div>
  )
}

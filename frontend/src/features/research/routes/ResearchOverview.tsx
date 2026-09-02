import { Link } from 'react-router-dom'
import { PageHeader } from '@/components/PageHeader'
import { TSuitabilityPanel } from '@/components/research/TSuitabilityPanel'
import { Panel, PanelBody, PanelHeader } from '@/components/ui/Primitives'
import { useFactorCatalog, useRunList } from '../hooks/useResearchQueries'
import { fmtDateTime } from '../lib/format'
import { DataStatusBadge, EngineeringBadge, JobStatusBadge, PromotionBadge, VerdictBadge } from '../components/StatusBadges'
import { HttpErrorState, LoadingState } from '../components/QueryState'

export function ResearchOverview() {
  const catalog = useFactorCatalog({})
  const runs = useRunList({ limit: 10 })
  const items = catalog.data?.items ?? []
  const runItems = runs.data?.pages.flatMap((page) => page.items) ?? []
  const runnable = items.filter((item) => item.engineering_status !== 'planned' && item.latest_data_status === 'ready').length
  const unfinished = items.filter((item) => item.engineering_status !== 'completed').length
  const promoted = items.filter((item) => item.promotion_status === 'promoted').length
  const unavailable = items.filter((item) => item.latest_verdict === 'unavailable')
  const queue = runItems.filter((item) => item.job_status === 'pending' || item.job_status === 'running')

  return (
    <div className="workspace-page h-full min-h-0 overflow-auto">
      <PageHeader title="研究工作台" subtitle="工程、数据、裁决、晋级四套状态独立；[x] 不表示 accepted" />
      <div className="workspace-content">
        {catalog.isPending ? <LoadingState label="读取因子目录" /> : null}
        {catalog.isError ? <HttpErrorState error={catalog.error} onRetry={() => void catalog.refetch()} /> : null}
        {catalog.data ? (
          <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-4">
            <Stat label="因子" value={items.length} hint="公开目录" />
            <Stat label="可运行" value={runnable} hint="工程非计划且数据可用" />
            <Stat label="未完成" value={unfinished} hint="工程未收口" />
            <Stat label="Promoted" value={promoted} hint="晋级状态，不是裁决" />
          </div>
        ) : null}
        <div className="mt-3 grid gap-3 xl:grid-cols-3">
          <Panel className="xl:col-span-2">
            <PanelHeader>
              <h2 className="section-title">因子状态矩阵</h2>
              <Link className="text-xs text-accent hover:underline" to="/research/factors">打开目录</Link>
            </PanelHeader>
            <PanelBody>
              <div className="data-table-scroll">
                <table className="data-table min-w-[36rem]">
                  <thead>
                    <tr>
                      <th>因子</th>
                      <th>工程</th>
                      <th>数据</th>
                      <th>裁决</th>
                      <th>晋级</th>
                    </tr>
                  </thead>
                  <tbody>
                    {items.map((item) => (
                      <tr key={item.id}>
                        <td>
                          <Link className="text-sm text-foreground hover:text-accent" to={`/research/factors/${encodeURIComponent(item.id)}`}>{item.title}</Link>
                        </td>
                        <td><EngineeringBadge value={item.engineering_status} /></td>
                        <td><DataStatusBadge value={item.latest_data_status} /></td>
                        <td><VerdictBadge value={item.latest_verdict} /></td>
                        <td><PromotionBadge value={item.promotion_status} /></td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </PanelBody>
          </Panel>
          <Panel>
            <PanelHeader><h2 className="section-title">队列与最近运行</h2></PanelHeader>
            <PanelBody className="space-y-3">
              <p className="text-xs text-secondary">排队/运行中 {queue.length} 条。全市场任务走独立 worker，不在此重算。</p>
              {runs.isError ? <HttpErrorState error={runs.error} onRetry={() => void runs.refetch()} /> : null}
              <ul className="space-y-2 text-xs">
                {runItems.slice(0, 8).map((run) => (
                  <li key={run.run_id} className="flex items-center justify-between gap-2">
                    <Link className="min-w-0 truncate font-mono text-accent" to={`/research/runs/${encodeURIComponent(run.run_id)}`}>{run.run_id}</Link>
                    <JobStatusBadge value={run.job_status} />
                    <span className="shrink-0 text-muted">{fmtDateTime(run.created_at)}</span>
                  </li>
                ))}
                {runItems.length === 0 && !runs.isPending ? <li className="text-muted">还没有研究运行。</li> : null}
              </ul>
            </PanelBody>
          </Panel>
        </div>
        <div className="mt-3 grid gap-3 xl:grid-cols-2">
          <Panel>
            <PanelHeader><h2 className="section-title">待处理 unavailable</h2></PanelHeader>
            <PanelBody>
              {unavailable.length === 0 ? <p className="text-xs text-muted">没有最新裁决为 unavailable 的因子。</p> : (
                <ul className="space-y-1 text-xs">
                  {unavailable.map((item) => (
                    <li key={item.id}>
                      <Link className="text-accent hover:underline" to={`/research/factors/${encodeURIComponent(item.id)}`}>{item.title}</Link>
                      <span className="ml-2 text-muted">{item.latest_data_status ?? '数据未知'}</span>
                    </li>
                  ))}
                </ul>
              )}
            </PanelBody>
          </Panel>
          <div className="min-w-0">
            <TSuitabilityPanel />
          </div>
        </div>
      </div>
    </div>
  )
}

function Stat({ label, value, hint }: { label: string; value: number; hint: string }) {
  return (
    <div className="panel px-3 py-3">
      <p className="text-[11px] text-muted">{label}</p>
      <p className="metric-value">{value}</p>
      <p className="mt-1 text-[11px] text-secondary">{hint}</p>
    </div>
  )
}

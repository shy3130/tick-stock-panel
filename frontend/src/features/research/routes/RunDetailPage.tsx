import { useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { PageHeader } from '@/components/PageHeader'
import { cn } from '@/lib/cn'
import { HypothesisLinkPanel } from '../components/HypothesisLinkPanel'
import { ProvenanceInspector } from '../components/ProvenanceInspector'
import { GuidedEmpty, HttpErrorState, LoadingState, UnavailableState } from '../components/QueryState'
import {
  ArmTable,
  CalendarEffectView,
  EventTable,
  HorizonMatrix,
  ProfileResult,
  RetrievalView,
  RiskCharts,
  ShapeDistributionView,
  SummaryFacts,
  WarningList,
} from '../components/ResultViews'
import { RunActions, RunProgress } from '../components/RunProgress'
import { DataStatusBadge, JobStatusBadge, ProfileBadge, PromotionBadge, VerdictBadge } from '../components/StatusBadges'
import { useRunDetail, useRunEvents, useRunSeries } from '../hooks/useResearchQueries'
import { useCancelRun, useCreateRun, usePatchRun } from '../hooks/useRunMutations'
import { useRunStream } from '../hooks/useRunStream'
import { scopeLabel, windowLabel } from '../model/run'
import type { RunScope } from '../model/status'

const TABS = [
  { id: 'summary', label: '摘要' },
  { id: 'arms', label: 'Arms 与基线' },
  { id: 'horizon', label: 'Horizon' },
  { id: 'risk', label: '风险与净值' },
  { id: 'events', label: '事件' },
  { id: 'lineage', label: '数据谱系' },
] as const

type TabId = (typeof TABS)[number]['id']

export function RunDetailPage() {
  const { runId = '' } = useParams()
  const navigate = useNavigate()
  const [tab, setTab] = useState<TabId>('summary')
  const detail = useRunDetail(runId || undefined)
  const run = detail.data
  const stream = useRunStream(runId, run?.job_status ?? null)
  const events = useRunEvents(runId || undefined)
  const series = useRunSeries(runId || undefined, tab === 'risk' || tab === 'summary')
  const cancel = useCancelRun()
  const patch = usePatchRun()
  const create = useCreateRun()
  const eventRows = events.data?.pages.flatMap((page) => page.items) ?? []

  return (
    <div className="workspace-page h-full min-h-0 overflow-auto">
      <PageHeader
        title={run ? run.label || run.run_id : '运行详情'}
        subtitle="前端只展示后端指标，不重算收益、风险、baseline 或 verdict"
        titleExtra={run ? <JobStatusBadge value={stream.jobStatus ?? run.job_status} /> : null}
      />
      <div className="workspace-content min-w-0 space-y-3">
        {!runId ? (
          <GuidedEmpty title="未指定运行" hint="从运行中心打开一条 Durable Run，再查看摘要、谱系与关联假设。" />
        ) : null}
        {runId && detail.isPending ? <LoadingState label="读取运行" /> : null}
        {runId && detail.isError ? <HttpErrorState error={detail.error} onRetry={() => void detail.refetch()} /> : null}
        {runId && !detail.isPending && !detail.isError && !run ? (
          <GuidedEmpty title="没有这条运行" hint="运行可能尚未创建，或 run_id 无效。" />
        ) : null}
        {run ? (
          <>
            <div className="flex min-w-0 flex-wrap items-center gap-2">
              <Link className="max-w-full truncate text-xs text-accent hover:underline" to={`/research/factors/${encodeURIComponent(run.factor_id)}`}>{run.factor_title ?? run.factor_id}</Link>
              <VerdictBadge value={run.verdict} />
              <DataStatusBadge value={run.data_status} />
              <PromotionBadge value={run.promotion_status} />
              <ProfileBadge value={run.result_profile} />
              <span className="min-w-0 break-all font-mono text-[11px] text-muted">{scopeLabel(run.scope)} · {windowLabel(run.window)}</span>
            </div>
            <RunProgress stream={stream} jobStatus={run.job_status} />
            <RunActions
              runId={run.run_id}
              jobStatus={stream.jobStatus ?? run.job_status}
              favorite={run.favorite}
              label={run.label}
              canCancel
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
            <WarningList run={run} />
            <div className="workspace-toolbar overflow-x-auto" role="tablist" aria-label="运行详情">
              {TABS.map((item) => (
                <button
                  key={item.id}
                  type="button"
                  role="tab"
                  aria-selected={tab === item.id}
                  className={cn(
                    'min-h-11 shrink-0 rounded-btn px-3 text-xs font-medium',
                    tab === item.id ? 'bg-accent/15 text-accent' : 'text-secondary hover:bg-elevated',
                  )}
                  onClick={() => setTab(item.id)}
                >
                  {item.label}
                </button>
              ))}
            </div>
            {tab === 'summary' ? (
              <div className="min-w-0 space-y-3">
                {run.verdict === 'unavailable' ? (
                  <UnavailableState reasons={run.unavailable_reasons} />
                ) : (
                  <>
                    <SummaryFacts run={run} />
                    <ProfileResult run={run} />
                  </>
                )}
                <HypothesisLinkPanel runId={run.run_id} linked={run.hypotheses} />
              </div>
            ) : null}
            {tab === 'arms' ? (
              run.result?.profile === 'retrieval' ? <RetrievalView items={run.result.items} />
                : run.result?.profile === 'shape_distribution' ? <ShapeDistributionView bins={run.result.bins} />
                  : run.result?.profile === 'calendar_effect' ? <CalendarEffectView windows={run.result.windows} />
                    : <ArmTable arms={run.arms} />
            ) : null}
            {tab === 'horizon' ? <HorizonMatrix rows={run.horizons} /> : null}
            {tab === 'risk' ? (
              series.isPending ? <LoadingState label="读取净值曲线" compact />
                : series.isError ? <HttpErrorState error={series.error} onRetry={() => void series.refetch()} title="曲线读取失败" />
                  : <RiskCharts risk={run.risk} series={series.data ?? []} />
            ) : null}
            {tab === 'events' ? (
              events.isPending ? <LoadingState label="读取事件" compact />
                : events.isError ? <HttpErrorState error={events.error} onRetry={() => void events.refetch()} title="事件读取失败" />
                  : (
                    <EventTable
                      rows={eventRows}
                      hasMore={events.hasNextPage}
                      pending={events.isFetchingNextPage}
                      onLoadMore={() => void events.fetchNextPage()}
                    />
                  )
            ) : null}
            {tab === 'lineage' ? <ProvenanceInspector provenance={run.provenance} artifacts={run.artifacts} /> : null}
          </>
        ) : null}
      </div>
    </div>
  )
}

import { useNavigate, useParams } from 'react-router-dom'
import { Play } from 'lucide-react'
import { PageHeader } from '@/components/PageHeader'
import { Btn } from '@/components/ui/Primitives'
import { FactorHeader } from '../components/FactorHeader'
import { ParameterForm } from '../components/ParameterForm'
import { PreflightInspector } from '../components/PreflightInspector'
import { BlockingList, HttpErrorState, InlineError, LoadingState } from '../components/QueryState'
import { ScopeEditor } from '../components/ScopeEditor'
import { useCreateRun } from '../hooks/useRunMutations'
import { useWorkbench } from '../hooks/useWorkbench'
import { isResearchApiError } from '../model/errors'
import { reasonsFromErrorDetails } from '../model/preflight'

export function FactorWorkbenchPage() {
  const { factorId = '' } = useParams()
  const navigate = useNavigate()
  const workbench = useWorkbench(factorId)
  const createRun = useCreateRun()
  const { detail, detailQuery, form, scope, parameters, structureError, preflightQuery, preflightCurrent, canRun } = workbench

  const blockedReasons = createRun.error && isResearchApiError(createRun.error) && createRun.error.isPreflightBlocked
    ? reasonsFromErrorDetails(createRun.error.details)
    : []

  const submit = async () => {
    if (!detail || !canRun) return
    try {
      const created = await createRun.mutateAsync({
        factor_id: detail.id,
        scope,
        parameters,
        source_run_id: null,
      })
      navigate(`/research/runs/${encodeURIComponent(created.run_id)}`)
    } catch {
      // typed error rendered below
    }
  }

  return (
    <div className="workspace-page h-full min-h-0 overflow-auto">
      <PageHeader
        title={detail?.title ?? '因子工作台'}
        subtitle="通用 Workbench：七种控件 + 独立 scope + 预检 revision 失效"
        right={
          <Btn variant="primary" className="min-h-11 text-xs" disabled={!canRun || createRun.isPending} onClick={() => void submit()}>
            <Play className="h-3.5 w-3.5" aria-hidden />
            {createRun.isPending ? '创建中' : '创建运行'}
          </Btn>
        }
      />
      <div className="workspace-content">
        {detailQuery.isPending ? <LoadingState label="读取因子定义" /> : null}
        {detailQuery.isError ? <HttpErrorState error={detailQuery.error} onRetry={() => void detailQuery.refetch()} title="因子不存在或无法读取" /> : null}
        {detail ? (
          <div className="grid grid-cols-1 gap-3 md:grid-cols-2 xl:grid-cols-[minmax(16rem,20rem)_minmax(0,1fr)_minmax(18rem,24rem)]">
            <div className="min-w-0">
              <FactorHeader detail={detail} />
            </div>
            <div className="min-w-0 space-y-3">
              <div className="panel p-3">
                <ScopeEditor detail={detail} scope={scope} onChange={workbench.setScope} />
              </div>
              <div className="panel p-3">
                <p className="section-kicker">Parameters</p>
                <h3 className="section-title mb-3">因子参数</h3>
                <ParameterForm form={form} values={parameters} onChange={workbench.setParam} />
              </div>
              {structureError ? <InlineError message={structureError} /> : null}
              {blockedReasons.length > 0 ? (
                <div className="panel p-3">
                  <p className="mb-2 text-xs text-warning">POST /runs 复核 preflight 后 409，未创建 Run。</p>
                  <BlockingList reasons={blockedReasons} />
                </div>
              ) : null}
              {createRun.isError && blockedReasons.length === 0 ? <InlineError message={createRun.error.message} /> : null}
            </div>
            <div className="min-w-0">
              <PreflightInspector
                result={preflightQuery.data}
                stale={!preflightCurrent}
                pending={preflightQuery.isFetching}
                error={preflightQuery.isError ? preflightQuery.error : null}
                onRetry={() => void preflightQuery.refetch()}
                structureError={structureError}
              />
            </div>
          </div>
        ) : null}
      </div>
    </div>
  )
}

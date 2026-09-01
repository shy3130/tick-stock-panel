import { useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { Loader2, Plus } from 'lucide-react'
import { Panel, PanelBody, PanelHeader } from '@/components/ui/Primitives'
import { cn } from '@/lib/cn'
import { useHypothesisList } from '../hooks/useResearchQueries'
import { useLinkRunHypothesis } from '../hooks/useRunMutations'
import type { ResearchHypothesis } from '../model/notebook'
import { GuidedEmpty, HttpErrorState, InlineError, LoadingState } from './QueryState'

export function HypothesisLinkPanel({
  runId,
  linked,
}: {
  runId: string
  linked: ResearchHypothesis[]
}) {
  const list = useHypothesisList()
  const link = useLinkRunHypothesis()
  const [hypothesisId, setHypothesisId] = useState('')
  const linkedIds = useMemo(() => {
    const ids: Record<string, true> = {}
    for (const item of linked) ids[item.id] = true
    return ids
  }, [linked])
  const options = (list.data?.items ?? []).filter((item) => !linkedIds[item.id])

  return (
    <Panel>
      <PanelHeader>
        <div className="min-w-0">
          <p className="section-kicker">Evidence</p>
          <h3 className="section-title">关联假设</h3>
          <p className="mt-1 text-xs leading-relaxed text-muted">只关联已有假设。证据通过已验证的运行关联入口写入，不会把本 Run 伪装成旧 Run Card。</p>
        </div>
      </PanelHeader>
      <PanelBody className="space-y-3">
        {linked.length === 0 ? (
          <p className="text-xs text-muted">尚未关联研究假设。关联后会在假设证据链写入 factor_run，不会把本 Run 伪装成旧 Run Card。</p>
        ) : (
          <ul className="space-y-2" aria-label="已关联假设">
            {linked.map((item) => (
              <li key={item.id} className="min-w-0 rounded-input border border-border bg-base/40 px-2.5 py-2">
                <Link
                  to="/research/evidence"
                  className="block truncate text-xs font-medium text-accent hover:underline"
                >
                  {item.title || item.id}
                </Link>
                <p className="mt-0.5 truncate font-mono text-[11px] text-muted">{item.id} · {item.status}</p>
              </li>
            ))}
          </ul>
        )}

        {list.isPending ? <LoadingState label="读取可关联假设" compact /> : null}
        {list.isError ? <HttpErrorState error={list.error} onRetry={() => void list.refetch()} title="假设列表读取失败" /> : null}
        {list.data && list.data.items.length === 0 ? (
          <GuidedEmpty title="还没有研究假设" hint="先到证据页建立一个可验证命题，再回到这里关联当前运行。" />
        ) : null}

        {options.length > 0 ? (
          <form
            className="flex min-w-0 flex-col gap-2 sm:flex-row sm:items-end"
            onSubmit={(event) => {
              event.preventDefault()
              if (!hypothesisId) return
              link.mutate({ runId, hypothesisId }, { onSuccess: () => setHypothesisId('') })
            }}
          >
            <label className="grid min-w-0 flex-1 gap-1.5 text-xs text-secondary">
              已有假设
              <select
                value={hypothesisId}
                onChange={(event) => setHypothesisId(event.target.value)}
                className="control w-full min-w-0 text-xs"
              >
                <option value="">选择要关联的假设</option>
                {options.map((item) => (
                  <option key={item.id} value={item.id}>{item.title || item.id}</option>
                ))}
              </select>
            </label>
            <button
              type="submit"
              disabled={link.isPending || !hypothesisId}
              className={cn('btn-primary min-h-11 shrink-0 text-xs')}
            >
              {link.isPending ? <Loader2 className="h-3.5 w-3.5 motion-safe:animate-spin" /> : <Plus className="h-3.5 w-3.5" />}
              关联
            </button>
          </form>
        ) : null}
        {link.isError ? <InlineError message={link.error instanceof Error ? link.error.message : '关联失败'} /> : null}
      </PanelBody>
    </Panel>
  )
}

import { CheckCircle2, CircleDashed, Loader2, ShieldAlert } from 'lucide-react'
import { Badge, Panel, PanelBody, PanelHeader } from '@/components/ui/Primitives'
import { fmtHash } from '../lib/format'
import type { PreflightResult } from '../model/preflight'
import { DATA_STATUS_META, parseDataStatus } from '../model/status'
import { BlockingList, HttpErrorState } from './QueryState'

export function PreflightInspector({
  result,
  stale,
  pending,
  error,
  onRetry,
  structureError,
}: {
  result: PreflightResult | undefined
  stale: boolean
  pending: boolean
  error: unknown
  onRetry: () => void
  structureError: string | null
}) {
  return (
    <Panel>
      <PanelHeader>
        <div>
          <p className="section-kicker">Preflight</p>
          <h3 className="section-title">预检与谱系</h3>
        </div>
        {pending ? <Loader2 className="h-4 w-4 text-muted motion-safe:animate-spin" aria-label="预检中" /> : null}
      </PanelHeader>
      <PanelBody className="space-y-3">
        {structureError ? (
          <p className="flex items-start gap-1.5 text-xs text-warning">
            <ShieldAlert className="mt-0.5 h-3.5 w-3.5 shrink-0" aria-hidden />
            {structureError}
          </p>
        ) : null}
        {error ? (
          <HttpErrorState error={error} onRetry={onRetry} title="预检请求失败" />
        ) : stale || !result ? (
          <p className="flex items-start gap-1.5 text-xs text-muted">
            <CircleDashed className="mt-0.5 h-3.5 w-3.5 shrink-0" aria-hidden />
            参数已变化，旧预检已失效。运行按钮保持禁用，直到新预检完成且 ready。
          </p>
        ) : result.ready ? (
          <p className="flex items-start gap-1.5 text-xs text-success">
            <CheckCircle2 className="mt-0.5 h-3.5 w-3.5 shrink-0" aria-hidden />
            预检通过。创建运行时服务端会再执行一次 preflight；若仍 blocked 将返回 409 且不建 Run。
          </p>
        ) : (
          <div className="space-y-2">
            <p className="text-xs text-warning">预检未就绪（HTTP 200 + ready=false），不是系统错误。</p>
            <BlockingList reasons={result.blocking_reasons} />
          </div>
        )}
        {result && !stale ? (
          <>
            {result.sources.length > 0 ? (
              <div className="data-table-scroll">
                <table className="data-table">
                  <thead>
                    <tr>
                      <th>源</th>
                      <th>状态</th>
                      <th>Generation</th>
                      <th>区间</th>
                    </tr>
                  </thead>
                  <tbody>
                    {result.sources.map((source) => {
                      const dataStatus = parseDataStatus(source.status)
                      const tone = dataStatus ? DATA_STATUS_META[dataStatus].tone : 'muted'
                      return (
                        <tr key={`${source.kind}-${source.generation ?? ''}`}>
                          <td className="font-mono text-xs">{source.kind}</td>
                          <td><Badge tone={tone}>{source.status}</Badge></td>
                          <td className="font-mono text-[11px]">{source.generation ?? '—'}{source.manifest_sha256 ? ` · ${fmtHash(source.manifest_sha256)}` : ''}</td>
                          <td className="font-mono text-[11px]">{source.available_from || source.available_to ? `${source.available_from ?? '—'} → ${source.available_to ?? '—'}` : '—'}</td>
                        </tr>
                      )
                    })}
                  </tbody>
                </table>
              </div>
            ) : null}
            {result.cohort ? (
              <dl className="grid grid-cols-3 gap-2 text-center text-xs">
                <div className="rounded-input border border-border bg-base/40 px-2 py-2">
                  <dt className="text-muted">请求</dt>
                  <dd className="metric-value text-base">{result.cohort.requested_symbols ?? '—'}</dd>
                </div>
                <div className="rounded-input border border-border bg-base/40 px-2 py-2">
                  <dt className="text-muted">合格</dt>
                  <dd className="metric-value text-base">{result.cohort.eligible_symbols ?? '—'}</dd>
                </div>
                <div className="rounded-input border border-border bg-base/40 px-2 py-2">
                  <dt className="text-muted">删失</dt>
                  <dd className="metric-value text-base">{result.cohort.censored_symbols ?? '—'}</dd>
                </div>
              </dl>
            ) : null}
            {result.warnings.length > 0 ? (
              <ul className="space-y-1 text-xs text-warning">
                {result.warnings.map((warning) => (
                  <li key={`${warning.code}-${warning.message}`}>{warning.message}</li>
                ))}
              </ul>
            ) : null}
            {result.resource_estimate ? (
              <p className="font-mono text-[11px] text-muted">
                class={result.resource_estimate.class ?? '—'} · full_market={String(result.resource_estimate.full_market_supported)}
              </p>
            ) : null}
          </>
        ) : null}
      </PanelBody>
    </Panel>
  )
}

import { CheckCircle2 } from 'lucide-react'
import { useCapabilities, useSettings } from '@/lib/useSharedQueries'
import { CAP_LABELS } from '@/lib/capability-labels'

export function SettingsKeysPanel() {
  const settings = useSettings()
  const caps = useCapabilities()
  const provider = settings.data?.data_provider ?? settings.data?.mode ?? 'fquant_local'
  const providerLabel = provider === 'fquant_local' ? 'duckdb' : provider
  const capEntries = Object.entries(caps.data?.capabilities ?? {})

  return (
    <div className="max-w-2xl space-y-3">
      <section className="panel">
        <div className="panel-header">
          <div className="flex items-center gap-2">
            <CheckCircle2 className="h-4 w-4 text-accent" />
            <h2 className="section-title">数据源</h2>
          </div>
        </div>
        <div className="panel-body">
          <p className="text-sm text-secondary leading-relaxed">
            当前使用 <span className="font-mono text-foreground">{providerLabel}</span> 数据源。
            功能可用性由当前 provider capability 决定。
          </p>
          {caps.data && (
            <div className="mt-3 text-xs text-muted">
              已启用 {capEntries.length} 项能力 · {caps.data.label}
            </div>
          )}
        </div>
      </section>

      <section className="panel overflow-hidden">
        <div className="panel-header">
          <h2 className="section-title">可用功能</h2>
        </div>
        {capEntries.length > 0 ? (
          <div className="divide-y divide-border">
            {capEntries.map(([cap, lim]) => {
              const meta = CAP_LABELS[cap]
              return (
                <div key={cap} className="flex items-baseline justify-between gap-4 px-3 py-2.5">
                  <div className="min-w-0">
                    <div className="truncate text-sm text-foreground">{meta?.name ?? cap}</div>
                    {meta?.hint && <div className="mt-0.5 truncate text-[11px] text-muted">{meta.hint}</div>}
                  </div>
                  <div className="shrink-0 text-right font-mono text-xs text-secondary num">
                    {lim.rpm ? `${lim.rpm}/min` : lim.subscribe ? `${lim.subscribe} 订阅` : lim.batch ? `${lim.batch} 只/次` : '—'}
                  </div>
                </div>
              )
            })}
          </div>
        ) : (
          <div className="px-3 py-8 text-center text-sm text-muted">暂无 capability 缓存</div>
        )}
      </section>
    </div>
  )
}

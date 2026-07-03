import { CheckCircle2 } from 'lucide-react'
import { useCapabilities, useSettings } from '@/lib/useSharedQueries'
import { CAP_LABELS } from '@/lib/capability-labels'

export function SettingsKeysPanel() {
  const settings = useSettings()
  const caps = useCapabilities()
  const provider = settings.data?.data_provider ?? settings.data?.mode ?? 'fquant_local'
  const capEntries = Object.entries(caps.data?.capabilities ?? {})

  return (
    <div className="max-w-2xl space-y-4">
      <section className="rounded-card border border-border bg-surface p-5">
        <div className="flex items-center gap-2">
          <CheckCircle2 className="h-4 w-4 text-bear" />
          <h2 className="text-sm font-medium text-foreground">数据源</h2>
        </div>
        <p className="mt-3 text-sm text-secondary leading-relaxed">
          当前使用 <span className="font-mono text-foreground">{provider}</span> 数据源。
          功能可用性由当前 provider capability 决定。
        </p>
        {caps.data && (
          <div className="mt-3 text-xs text-muted">
            已启用 {capEntries.length} 项能力 · {caps.data.label}
          </div>
        )}
      </section>

      <section className="rounded-card border border-border bg-surface overflow-hidden">
        <div className="px-5 py-3 border-b border-border text-sm font-medium text-foreground">
          可用功能
        </div>
        {capEntries.length > 0 ? (
          <div className="divide-y divide-border">
            {capEntries.map(([cap, lim]) => {
              const meta = CAP_LABELS[cap]
              return (
                <div key={cap} className="px-5 py-3 flex items-baseline justify-between gap-4">
                  <div className="min-w-0">
                    <div className="text-sm text-foreground truncate">{meta?.name ?? cap}</div>
                    {meta?.hint && <div className="mt-0.5 text-[11px] text-muted truncate">{meta.hint}</div>}
                  </div>
                  <div className="text-right shrink-0 text-xs font-mono text-secondary">
                    {lim.rpm ? `${lim.rpm}/min` : lim.subscribe ? `${lim.subscribe} 订阅` : lim.batch ? `${lim.batch} 只/次` : '—'}
                  </div>
                </div>
              )
            })}
          </div>
        ) : (
          <div className="px-5 py-8 text-center text-sm text-muted">暂无 capability 缓存</div>
        )}
      </section>
    </div>
  )
}

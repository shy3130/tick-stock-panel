import { X } from 'lucide-react'
import { InstrumentSearchAdder } from '@/components/instruments/InstrumentSearchInput'
import { cn } from '@/lib/cn'
import type { FactorDetail } from '../model/factor'
import type { RunScope } from '../model/status'

export function ScopeEditor({
  detail,
  scope,
  onChange,
}: {
  detail: FactorDetail
  scope: RunScope
  onChange: (scope: RunScope) => void
}) {
  const supportsSymbols = detail.supported_scopes.includes('symbols')
  const supportsFullMarket = detail.supported_scopes.includes('full_market')
  const cap = detail.scope_capabilities.find((item) => item.type === scope.type)

  return (
    <section className="space-y-3" aria-label="运行范围">
      <div>
        <p className="section-kicker">Scope</p>
        <h3 className="section-title">运行范围</h3>
        <p className="mt-1 text-xs leading-relaxed text-muted">范围与因子参数分离。symbols / full-market 由 Control Plane 统一处理，不写入 parameters。</p>
      </div>
      <div className="flex flex-wrap gap-2" role="radiogroup" aria-label="范围类型">
        {supportsSymbols ? (
          <ScopeChoice
            checked={scope.type === 'symbols'}
            label="标的列表"
            hint="有界 interactive"
            onSelect={() => onChange({ type: 'symbols', symbols: scope.type === 'symbols' ? scope.symbols : [] })}
          />
        ) : null}
        {supportsFullMarket ? (
          <ScopeChoice
            checked={scope.type === 'full_market'}
            label="全市场"
            hint="独立 worker / 可能排队"
            onSelect={() => onChange({ type: 'full_market' })}
          />
        ) : null}
      </div>
      {cap?.notes ? <p className="text-xs leading-relaxed text-secondary">{cap.notes}</p> : null}
      {cap?.unavailable_capabilities.length ? (
        <p className="text-xs text-warning">当前范围不可用：{cap.unavailable_capabilities.join('、')}</p>
      ) : null}
      {detail.id === 'negative-exclusion' ? (
        <p className="rounded-input border border-warning/30 bg-warning/5 px-2.5 py-2 text-xs leading-relaxed text-warning">
          标的范围可跑 V2/V4/V5；V1/V3 仅展示为 capability unavailable。全市场内部只映射 V5，不能把 V5 标成 V1–V5 全量能力。
        </p>
      ) : null}
      {scope.type === 'symbols' ? (
        <div className="space-y-2">
          <InstrumentSearchAdder
            onAdd={(result) => {
              if (scope.symbols.includes(result.symbol)) return
              onChange({ type: 'symbols', symbols: [...scope.symbols, result.symbol] })
            }}
            assetTypes={['stock']}
            placeholder="搜索并添加标的"
          />
          <ul className="flex flex-wrap gap-1.5" aria-label="已选标的">
            {scope.symbols.map((symbol) => (
              <li key={symbol}>
                <span className="inline-flex min-h-11 items-center gap-1 rounded-btn border border-border bg-elevated px-2 font-mono text-xs sm:min-h-[var(--control-h)]">
                  {symbol}
                  <button
                    type="button"
                    className="inline-flex h-8 w-8 items-center justify-center rounded-btn text-muted hover:text-danger"
                    aria-label={`移除 ${symbol}`}
                    onClick={() => onChange({ type: 'symbols', symbols: scope.symbols.filter((item) => item !== symbol) })}
                  >
                    <X className="h-3.5 w-3.5" />
                  </button>
                </span>
              </li>
            ))}
          </ul>
          {detail.id === 'weak-to-strong' ? (
            <p className="text-[11px] text-muted">弱转强标的上限 100。</p>
          ) : null}
        </div>
      ) : null}
    </section>
  )
}

function ScopeChoice({ checked, label, hint, onSelect }: { checked: boolean; label: string; hint: string; onSelect: () => void }) {
  return (
    <button
      type="button"
      role="radio"
      aria-checked={checked}
      onClick={onSelect}
      className={cn(
        'min-h-11 rounded-btn border px-3 py-2 text-left text-xs transition-colors duration-fast ease-smooth',
        checked ? 'border-accent bg-accent/10 text-foreground' : 'border-border bg-elevated text-secondary hover:text-foreground',
      )}
    >
      <span className="block font-medium">{label}</span>
      <span className="mt-0.5 block text-[11px] text-muted">{hint}</span>
    </button>
  )
}

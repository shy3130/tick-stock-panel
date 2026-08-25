import { useState } from 'react'
import { useMutation } from '@tanstack/react-query'
import { ChevronDown, CloudSun, Loader2 } from 'lucide-react'
import { api, type StrategyBacktestRequest } from '@/lib/api'
import { fmtPct, priceColorClass } from '@/lib/format'
import { buildRegimeGrid } from './trustDiagnosticsCore'
import { useStrategyCheckStatus } from './StrategyCheckPanel'

interface Props {
  request: StrategyBacktestRequest
}

const fmt = (value: unknown, digits = 2) => {
  const parsed = Number(value)
  return Number.isFinite(parsed) ? parsed.toFixed(digits) : '—'
}

/** 市场状态分桶面板 — 按钮触发(避免每次回测都打端点); 四桶 2x2 网格 + definitions 折叠 */
export function RegimeBreakdownPanel({ request }: Props) {
  const onStatusChange = useStrategyCheckStatus('regime')
  const [definitionsOpen, setDefinitionsOpen] = useState(false)
  const mutation = useMutation({
    mutationFn: () => api.strategyRegimeBreakdown(request),
    onMutate: () => { onStatusChange?.('running') },
    onSuccess: () => { onStatusChange?.('completed') },
    onError: (error) => {
      onStatusChange?.('failed', error instanceof Error ? error.message : '市场状态分桶统计失败')
    },
  })
  const regime = mutation.data?.regime
  const definitions = regime ? Object.entries(regime.definitions) : []
  return (
    <section className="rounded-btn border border-border bg-surface/40">
      <div className="flex flex-wrap items-center justify-between gap-3 border-b border-border px-3 py-2.5">
        <div>
          <div className="flex items-center gap-1.5 text-xs font-semibold text-secondary"><CloudSun className="h-3.5 w-3.5 text-accent" />市场状态分桶表现</div>
          <div className="mt-0.5 text-[10px] leading-4 text-muted">按基准净值 vs 60 日均值（牛/熊）× 20 日滚动波动 vs 全样本中位数（高波动/平静）四桶统计策略条件表现；会按相同配置重跑一次回测。</div>
        </div>
        <button
          type="button"
          onClick={() => mutation.mutate()}
          disabled={mutation.isPending}
          className="inline-flex h-8 items-center gap-1.5 rounded-btn bg-accent px-3 text-[11px] font-medium text-white transition-opacity hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-50"
        >
          {mutation.isPending ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <CloudSun className="h-3.5 w-3.5" />}
          {mutation.isPending ? '统计中…' : '按状态分桶'}
        </button>
      </div>

      {mutation.error && (
        <div className="mx-3 mt-3 rounded-input border border-danger/30 bg-danger/5 px-3 py-2 text-[11px] text-danger">
          {mutation.error instanceof Error ? mutation.error.message : '市场状态分桶统计失败'}
        </div>
      )}

      {mutation.data && (
        <div className="space-y-3 p-3">
          {regime == null ? (
            <div className="rounded-input border border-border px-3 py-3 text-center text-[11px] leading-5 text-muted">
              样本不足（&lt;120 对齐交易日）：策略净值与基准净值的对齐交易日不足以划出有效的状态分桶，请拉长回测区间。
            </div>
          ) : (
            <>
              <div className="flex flex-wrap gap-x-4 gap-y-1 text-[10px] text-muted">
                <span>对齐交易日 <span className="font-mono text-secondary">{regime.n_days}</span></span>
                <span>状态 warmup <span className="font-mono text-secondary">{regime.warmup_days} 天</span>（60 日均线未就绪，不计入任何桶）</span>
                {mutation.data.note && <span>{mutation.data.note}</span>}
              </div>
              <div className="grid gap-2 sm:grid-cols-2">
                {buildRegimeGrid(regime.buckets).map(cell => (
                  <div key={cell.key} className="overflow-hidden rounded-input border border-border">
                    <div className={`flex items-center justify-between gap-2 border-b border-border px-3 py-1.5 ${cell.trend === 'bull' ? 'bg-bull/5' : 'bg-bear/5'}`}>
                      <span className="text-[11px] font-medium text-foreground">{cell.label}</span>
                      <span className="font-mono text-[10px] text-muted">{cell.days} 天 · {cell.daysPct != null ? `${(cell.daysPct * 100).toFixed(1)}%` : '—'}</span>
                    </div>
                    <div className="grid grid-cols-2 gap-px bg-border">
                      <div className="bg-surface px-3 py-2"><div className="text-[10px] text-muted">策略累计收益</div><div className={`mt-0.5 font-mono text-sm font-semibold num ${priceColorClass(cell.strategyTotalReturn)}`}>{fmtPct(cell.strategyTotalReturn, 1)}</div></div>
                      <div className="bg-surface px-3 py-2"><div className="text-[10px] text-muted">超额（对基准）</div><div className={`mt-0.5 font-mono text-sm font-semibold num ${priceColorClass(cell.excessTotalReturn)}`}>{fmtPct(cell.excessTotalReturn, 1)}</div></div>
                      <div className="bg-surface px-3 py-2"><div className="text-[10px] text-muted">Sharpe</div><div className="mt-0.5 font-mono text-sm font-semibold text-foreground num">{fmt(cell.strategySharpe)}</div></div>
                      <div className="bg-surface px-3 py-2"><div className="text-[10px] text-muted">最大回撤</div><div className="mt-0.5 font-mono text-sm font-semibold text-bear num">{fmtPct(cell.strategyMaxDrawdown, 1)}</div></div>
                    </div>
                  </div>
                ))}
              </div>
              {definitions.length > 0 && (
                <div className="rounded-input border border-border">
                  <button
                    type="button"
                    onClick={() => setDefinitionsOpen(open => !open)}
                    className="flex w-full items-center justify-between px-3 py-1.5 text-[10px] text-muted transition-colors hover:text-secondary"
                  >
                    <span>分桶定义（trend / vol）</span>
                    <ChevronDown className={`h-3.5 w-3.5 transition-transform ${definitionsOpen ? 'rotate-180' : ''}`} />
                  </button>
                  {definitionsOpen && (
                    <div className="space-y-0.5 border-t border-border px-3 py-2">
                      {definitions.map(([key, text]) => (
                        <div key={key} className="font-mono text-[10px] leading-4 text-muted"><span className="text-secondary">{key}</span>: {text}</div>
                      ))}
                      <div className="text-[10px] leading-4 text-muted">注: 波动阈值基于基准全样本中位数，属事后统计（轻度前视），仅用于分组解释，不构成交易信号。</div>
                    </div>
                  )}
                </div>
              )}
              <div className="text-[10px] text-muted font-mono">run_id: {mutation.data.run_id}</div>
            </>
          )}
        </div>
      )}
    </section>
  )
}

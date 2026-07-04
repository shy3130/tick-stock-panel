import { useMemo, useState } from 'react'
import { useMutation, useQuery } from '@tanstack/react-query'
import { Loader2, X } from 'lucide-react'
import type { EChartsOption } from 'echarts'
import { api, type OptimizeMethod, type OptimizeResult } from '@/lib/api'
import { instrumentSearchMeta } from '@/lib/instrumentSearch'
import { PageHeader } from '@/components/PageHeader'
import { useECharts } from '@/pages/backtest/charts/useECharts'

const METHODS: { key: OptimizeMethod; label: string; hint: string }[] = [
  { key: 'risk_parity', label: '风险平价', hint: '各标的风险贡献均等' },
  { key: 'equal', label: '等权', hint: '1/N 均分' },
  { key: 'equal_vol', label: '等波动', hint: '按波动倒数分配' },
  { key: 'mean_variance', label: '均值方差', hint: '收益/协方差最优' },
  { key: 'max_diversification', label: '最大分散', hint: '最大化分散度比率' },
  { key: 'score_weight', label: '动量加权', hint: '按近区间累计动量分配' },
]

const PIE = ['#3B82F6', '#22D3EE', '#F59E0B', '#A78BFA', '#2D9B65', '#C74040', '#E879F9', '#FACC15']

export function Optimizer() {
  const [symbols, setSymbols] = useState<{ symbol: string; name?: string }[]>([])
  const [query, setQuery] = useState('')
  const [method, setMethod] = useState<OptimizeMethod>('risk_parity')
  const [lookback, setLookback] = useState(120)
  const [result, setResult] = useState<OptimizeResult | null>(null)

  const strategies = useQuery({ queryKey: ['opt-strategies'], queryFn: api.screenerStrategies })
  const search = useQuery({
    queryKey: ['optimizer-search', query],
    queryFn: () => api.instrumentSearch(query, 10),
    enabled: query.trim().length > 0,
  })

  const run = useMutation({
    mutationFn: () => api.optimize({ symbols: symbols.map(s => s.symbol), method, lookback_days: lookback }),
    onSuccess: setResult,
  })
  const importFromStrategy = useMutation({
    mutationFn: (id: string) => api.screenerRunPreset(id),
    onSuccess: (res) => {
      const picked = (res.rows ?? [])
        .map((row: any) => ({ symbol: row.symbol as string, name: row.name as string | undefined }))
        .filter(row => row.symbol)
      setSymbols(prev => {
        const seen = new Set(prev.map(row => row.symbol))
        return [...prev, ...picked.filter(row => !seen.has(row.symbol))].slice(0, 50)
      })
    },
  })

  const addSymbol = (symbol: string, name?: string) => {
    if (!symbols.some(s => s.symbol === symbol)) setSymbols([...symbols, { symbol, name }])
    setQuery('')
  }

  const pieOption = useMemo<EChartsOption | null>(() => {
    if (!result) return null
    return {
      backgroundColor: 'transparent',
      tooltip: {
        trigger: 'item',
        formatter: (p: any) => `${p.name} ${(p.value * 100).toFixed(2)}%`,
      },
      series: [{
        type: 'pie',
        radius: ['45%', '72%'],
        center: ['50%', '50%'],
        data: result.weights.map((w, i) => ({
          name: w.name ?? w.symbol,
          value: w.weight,
          itemStyle: { color: PIE[i % PIE.length] },
        })),
        label: {
          color: '#A1A1AA',
          fontSize: 11,
          formatter: (p: any) => `${p.name} ${p.percent.toFixed(1)}%`,
        },
      }],
    }
  }, [result])
  const pieRef = useECharts(pieOption, [result])

  return (
    <div className="p-4 space-y-4">
      <PageHeader title="组合优化" subtitle="标的权重 · 组合波动 · 分散度" />

      <div className="rounded-card border border-border bg-surface p-3 space-y-2">
        <div className="flex flex-wrap gap-1.5">
          {symbols.map(s => (
            <span key={s.symbol} className="inline-flex items-center gap-1 rounded bg-elevated px-2 py-0.5 text-xs">
              {s.name ?? s.symbol}
              <button type="button" onClick={() => setSymbols(symbols.filter(x => x.symbol !== s.symbol))}>
                <X className="h-3 w-3" />
              </button>
            </span>
          ))}
        </div>
        <div className="flex items-center gap-2">
          <select
            disabled={importFromStrategy.isPending}
            onChange={e => {
              if (e.target.value) importFromStrategy.mutate(e.target.value)
              e.target.value = ''
            }}
            className="h-7 px-2 rounded-input border border-border bg-elevated text-xs"
          >
            <option value="">从策略导入标的…</option>
            {strategies.data?.presets.map(s => <option key={s.id} value={s.id}>{s.name}</option>)}
          </select>
          {importFromStrategy.isPending && <Loader2 className="h-3.5 w-3.5 animate-spin text-muted" />}
        </div>
        <div className="relative">
          <input
            value={query}
            onChange={e => setQuery(e.target.value)}
            placeholder="搜索代码/名称/拼音添加标的（支持 A股 / ETF）"
            className="w-full h-8 px-2.5 rounded-input border border-border bg-elevated text-xs"
          />
          {search.data && search.data.results.length > 0 && query && (
            <div className="absolute z-20 mt-1 w-full rounded-card border border-border bg-surface max-h-52 overflow-auto">
              {search.data.results.map(r => {
                const meta = instrumentSearchMeta(r)
                return (
                  <button
                    key={r.symbol}
                    type="button"
                    onClick={() => addSymbol(r.symbol, r.name)}
                    className="flex w-full items-center justify-between px-2.5 py-1.5 text-xs hover:bg-elevated"
                  >
                    <span>{r.name} <span className="text-muted">{r.symbol}</span></span>
                    {meta ? <span className="text-muted">{meta}</span> : null}
                  </button>
                )
              })}
            </div>
          )}
        </div>
      </div>

      <div className="rounded-card border border-border bg-surface p-3 flex flex-wrap items-end gap-3">
        <label className="text-xs text-muted flex flex-col gap-1">
          优化方法
          <select
            value={method}
            onChange={e => setMethod(e.target.value as OptimizeMethod)}
            className="h-8 px-2 rounded-input border border-border bg-elevated text-xs text-foreground"
          >
            {METHODS.map(m => <option key={m.key} value={m.key}>{m.label}</option>)}
          </select>
        </label>
        <label className="text-xs text-muted flex flex-col gap-1">
          回看天数
          <input
            type="number"
            min={20}
            max={1000}
            value={lookback}
            onChange={e => setLookback(Number(e.target.value))}
            className="h-8 w-24 px-2 rounded-input border border-border bg-elevated text-xs num"
          />
        </label>
        <span className="text-[11px] text-muted">{METHODS.find(m => m.key === method)?.hint}</span>
        <button
          type="button"
          disabled={symbols.length < 2 || run.isPending}
          onClick={() => run.mutate()}
          className="ml-auto h-8 px-4 rounded-btn bg-accent/90 text-base text-xs font-medium hover:bg-accent disabled:opacity-40 flex items-center gap-1.5"
        >
          {run.isPending && <Loader2 className="h-3.5 w-3.5 animate-spin" />}
          计算权重
        </button>
      </div>

      {run.isError && <div className="text-xs text-danger">{(run.error as Error).message}</div>}

      {result && (
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
          <div className="rounded-card border border-border bg-surface p-3">
            <div className="flex gap-4 text-[11px] text-muted mb-2">
              <span>标的 {result.stats.n}</span>
              {result.stats.annualized_vol != null && <span>年化波动 {(result.stats.annualized_vol * 100).toFixed(1)}%</span>}
              {result.stats.diversification_ratio != null && <span>分散度 {result.stats.diversification_ratio}</span>}
            </div>
            <table className="w-full text-xs">
              <thead>
                <tr className="text-muted">
                  <th className="text-left py-1">标的</th>
                  <th className="text-right">权重</th>
                </tr>
              </thead>
              <tbody>
                {result.weights.slice().sort((a, b) => b.weight - a.weight).map(w => (
                  <tr key={w.symbol} className="border-t border-border/40">
                    <td className="py-1">{w.name ?? w.symbol} <span className="text-muted">{w.symbol}</span></td>
                    <td className="text-right num">{(w.weight * 100).toFixed(2)}%</td>
                  </tr>
                ))}
              </tbody>
            </table>
            {result.meta.dropped.length > 0 && (
              <div className="mt-2 text-[11px] text-muted">已剔除（无本地数据，如港股）：{result.meta.dropped.join(', ')}</div>
            )}
          </div>
          <div className="rounded-card border border-border bg-surface p-3">
            <div ref={pieRef} style={{ width: '100%', height: 300 }} />
          </div>
        </div>
      )}
    </div>
  )
}

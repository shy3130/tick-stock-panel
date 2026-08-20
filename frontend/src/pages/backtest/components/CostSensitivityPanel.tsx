import { useMemo } from 'react'
import { useMutation } from '@tanstack/react-query'
import type { EChartsOption } from 'echarts'
import { Loader2, Scale } from 'lucide-react'
import { api, type CostSensitivityRow, type StrategyBacktestRequest } from '@/lib/api'
import { fmtBigNum, fmtPct, priceColorClass } from '@/lib/format'
import { useECharts } from '../charts/useECharts'
import { sortCostRows } from './trustDiagnosticsCore'

interface Props {
  request: StrategyBacktestRequest
}

const finiteNumber = (value: unknown): number | null => {
  if (value == null) return null
  const parsed = Number(value)
  return Number.isFinite(parsed) ? parsed : null
}

const fmt = (value: unknown, digits = 2) => {
  const parsed = finiteNumber(value)
  return parsed == null ? '—' : parsed.toFixed(digits)
}


/** ECharts axis tooltip 回调实际读取的字段(结构子集, 回调契约保证) */
interface AxisTooltipParam {
  dataIndex?: number
}

const TOOLTIP_STYLE = {
  backgroundColor: 'rgba(15,23,42,0.95)',
  borderColor: 'rgba(148,163,184,0.2)',
  textStyle: { color: '#e2e8f0', fontSize: 12 },
}

/** 倍数-总收益/Sharpe 双线小图 (双 y 轴): x = 成本倍数(升序), 左轴总收益%, 右轴 Sharpe */
function CostSensitivityChart({ rows }: { rows: CostSensitivityRow[] }) {
  const option = useMemo<EChartsOption>(() => ({
    animation: false,
    grid: { left: 48, right: 44, top: 28, bottom: 26 },
    legend: { top: 0, textStyle: { color: '#94a3b8', fontSize: 10 } },
    tooltip: {
      trigger: 'axis',
      ...TOOLTIP_STYLE,
      formatter: (params: AxisTooltipParam | AxisTooltipParam[]) => {
        const first = Array.isArray(params) ? params[0] : params
        const row = rows[Number(first?.dataIndex ?? -1)]
        if (!row) return ''
        return [
          `<div style="font-size:11px;color:#94a3b8;margin-bottom:4px">成本倍数 ${row.multiplier}x${row.is_baseline ? '（基线）' : ''}</div>`,
          `<div style="display:flex;justify-content:space-between;gap:16px"><span>总收益</span><span style="font-family:monospace">${row.total_return != null ? fmtPct(row.total_return, 2) : '—'}</span></div>`,
          `<div style="display:flex;justify-content:space-between;gap:16px"><span>Sharpe</span><span style="font-family:monospace">${row.sharpe != null ? row.sharpe.toFixed(2) : '—'}</span></div>`,
        ].join('')
      },
    },
    xAxis: {
      type: 'category',
      data: rows.map(row => `${row.multiplier}x`),
      axisLabel: { color: '#64748b', fontSize: 10 },
      axisTick: { show: false },
      axisLine: { lineStyle: { color: '#334155' } },
    },
    yAxis: [
      {
        type: 'value',
        scale: true,
        name: '总收益',
        nameTextStyle: { color: '#64748b', fontSize: 10 },
        axisLabel: { color: '#64748b', fontSize: 10, formatter: (v: number) => `${(v * 100).toFixed(0)}%` },
        splitLine: { lineStyle: { color: '#1e293b' } },
        axisLine: { show: false },
      },
      {
        type: 'value',
        scale: true,
        position: 'right',
        name: 'Sharpe',
        nameTextStyle: { color: '#64748b', fontSize: 10 },
        axisLabel: { color: '#64748b', fontSize: 10, formatter: (v: number) => v.toFixed(1) },
        splitLine: { show: false },
        axisLine: { show: false },
      },
    ],
    series: [
      {
        name: '总收益',
        type: 'line',
        yAxisIndex: 0,
        data: rows.map(row => (row.total_return == null ? null : +(row.total_return * 100).toFixed(4))),
        symbol: 'circle',
        symbolSize: 5,
        connectNulls: true,
        lineStyle: { width: 1.6, color: '#3b82f6' },
        itemStyle: { color: '#3b82f6' },
      },
      {
        name: 'Sharpe',
        type: 'line',
        yAxisIndex: 1,
        data: rows.map(row => (row.sharpe == null ? null : +row.sharpe.toFixed(4))),
        symbol: 'circle',
        symbolSize: 5,
        connectNulls: true,
        lineStyle: { width: 1.6, color: '#f59e0b', type: 'dashed' },
        itemStyle: { color: '#f59e0b' },
      },
    ],
  }), [rows])
  const chartRef = useECharts(option, [rows])
  return <div ref={chartRef} className="h-[240px]" />
}

/** 成本敏感性面板 — 按钮触发; 佣金+滑点按倍数整体缩放重跑, 检验策略对交易成本的耐受度 */
export function CostSensitivityPanel({ request }: Props) {
  const mutation = useMutation({
    mutationFn: () => api.strategyCostSensitivity(request),
  })
  const cs = mutation.data?.cost_sensitivity
  const rows = useMemo(() => (cs ? sortCostRows(cs.rows) : []), [cs])
  return (
    <section className="rounded-btn border border-border bg-surface/40">
      <div className="flex flex-wrap items-center justify-between gap-3 border-b border-border px-3 py-2.5">
        <div>
          <div className="flex items-center gap-1.5 text-xs font-semibold text-secondary"><Scale className="h-3.5 w-3.5 text-accent" />成本敏感性</div>
          <div className="mt-0.5 text-[10px] leading-4 text-muted">把佣金与滑点按同一倍数整体缩放后重跑回测（1x = 当前基线成本）；检验策略收益对交易成本的耐受度。每个倍数都会执行一次同口径回测。</div>
        </div>
        <button
          type="button"
          onClick={() => mutation.mutate()}
          disabled={mutation.isPending}
          className="inline-flex h-8 items-center gap-1.5 rounded-btn bg-accent px-3 text-[11px] font-medium text-white transition-opacity hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-50"
        >
          {mutation.isPending ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Scale className="h-3.5 w-3.5" />}
          {mutation.isPending ? '重跑中…' : '成本敏感性分析'}
        </button>
      </div>

      {mutation.error && (
        <div className="mx-3 mt-3 rounded-input border border-danger/30 bg-danger/5 px-3 py-2 text-[11px] text-danger">
          {mutation.error instanceof Error ? mutation.error.message : '成本敏感性分析失败'}
        </div>
      )}

      {cs && rows.length > 0 && (
        <div className="space-y-3 p-3">
          <div className="overflow-hidden rounded-input border border-border"><CostSensitivityChart rows={rows} /></div>
          <div className="data-table-scroll">
            <table className="data-table">
              <thead>
                <tr>
                  <th>成本倍数</th>
                  <th className="text-right">佣金(‱)</th>
                  <th className="text-right">滑点(bps)</th>
                  <th className="text-right">总收益</th>
                  <th className="text-right">年化</th>
                  <th className="text-right">Sharpe</th>
                  <th className="text-right">最大回撤</th>
                  <th className="text-right">最终权益</th>
                  <th className="text-right">总成本</th>
                  <th className="text-right">交易数</th>
                </tr>
              </thead>
              <tbody>
                {rows.map(row => (
                  <tr key={row.multiplier} className={row.is_baseline ? 'bg-accent/5' : ''}>
                    <td className="font-mono font-medium text-foreground">
                      {row.multiplier}x{row.is_baseline && <span className="ml-1 rounded border border-accent/30 bg-accent/10 px-1 text-[9px] text-accent">基线</span>}
                    </td>
                    <td className="text-right font-mono num">{(row.fees_pct * 10000).toFixed(2)}</td>
                    <td className="text-right font-mono num">{row.slippage_bps.toFixed(1)}</td>
                    <td className={`text-right font-mono num ${priceColorClass(row.total_return)}`}>{fmtPct(row.total_return)}</td>
                    <td className={`text-right font-mono num ${priceColorClass(row.annualized_return)}`}>{fmtPct(row.annualized_return)}</td>
                    <td className="text-right font-mono num">{fmt(row.sharpe)}</td>
                    <td className="text-right font-mono text-bear num">{fmtPct(row.max_drawdown)}</td>
                    <td className="text-right font-mono num">{fmtBigNum(row.final_equity)}</td>
                    <td className="text-right font-mono num">{fmtBigNum(row.total_cost)}</td>
                    <td className="text-right font-mono num">{row.n_trades ?? '—'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {cs.note && <div className="text-[10px] leading-4 text-muted">{cs.note}</div>}
          <div className="flex flex-wrap gap-x-4 gap-y-1 text-[10px] text-muted">
            {mutation.data && <span className="font-mono">run_id(基线): {mutation.data.run_id_baseline}</span>}
            {mutation.data && <span className="font-mono">耗时 {mutation.data.elapsed_ms.toLocaleString('zh-CN')} ms</span>}
          </div>
        </div>
      )}
    </section>
  )
}

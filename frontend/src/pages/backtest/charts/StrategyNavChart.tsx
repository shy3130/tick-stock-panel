import { useMemo } from 'react'
import { useECharts } from './useECharts'
import type { StrategyBacktestResult } from '@/lib/api'
import { getNavPresentation } from './navPresentation'

interface Props {
  result: StrategyBacktestResult
}

export function StrategyNavChart({ result }: Props) {
  const {
    isCandidateExecution,
    navLabel,
    navAxisLabel,
    allowsBenchmark,
  } = getNavPresentation(result.stats?.full_kind)
  const hasBenchmarkLegend = allowsBenchmark && (result.benchmark_curve ?? []).some(row => row.close != null || row.value != null)

  const option = useMemo(() => {
    if (!result.equity_curve.length) return null
    const moneyFmt = new Intl.NumberFormat('zh-CN', { maximumFractionDigits: 0 })
    const valueFmt = new Intl.NumberFormat('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })
    const axisMoneyFmt = (v: number) => {
      if (Math.abs(v) >= 100_000_000) return `${(v / 100_000_000).toFixed(1)}亿`
      if (Math.abs(v) >= 10_000) return `${(v / 10_000).toFixed(0)}万`
      return moneyFmt.format(v)
    }
    const dates = result.equity_curve.map(r => r.date.slice(0, 10))
    const navValues = result.equity_curve.map(r => r.value)
    const benchmarkByDate = new Map((result.benchmark_curve ?? []).map(r => [r.date.slice(0, 10), r.close ?? r.value]))
    const benchmarkValues = dates.map(d => benchmarkByDate.get(d) ?? null)
    const hasBenchmark = allowsBenchmark && benchmarkValues.some(v => v != null)
    const benchmarkName = result.benchmark_curve?.[0]?.name ?? '基准'
    const ddValues = result.drawdown_curve.map(r => r.value * 100)

    return {
      animation: false,
      axisPointer: {
        link: [{ xAxisIndex: 'all' }],
        label: { backgroundColor: '#334155' },
      },
      grid: [
        { left: 64, right: hasBenchmark ? 64 : 16, top: 14, bottom: '40%' },
        { left: 64, right: hasBenchmark ? 64 : 16, top: '68%', bottom: 46 },
      ],
      xAxis: [
        {
          type: 'category', data: dates, gridIndex: 0,
          axisLabel: { show: false }, axisTick: { show: false },
          axisPointer: { show: true, type: 'line' },
          axisLine: { lineStyle: { color: '#334155' } },
        },
        {
          type: 'category', data: dates, gridIndex: 1,
          axisLabel: { color: '#64748b', fontSize: 10, interval: Math.floor(dates.length / 6) },
          axisTick: { show: false },
          axisPointer: { show: true, type: 'line' },
          axisLine: { lineStyle: { color: '#334155' } },
        },
      ],
      yAxis: [
        {
          type: 'value', gridIndex: 0,
          scale: true,
          name: hasBenchmark ? `${benchmarkName}点位` : navAxisLabel,
          nameTextStyle: { color: hasBenchmark ? 'rgba(148,163,184,0.55)' : '#64748b', fontSize: 10, padding: [0, 0, 4, 0] },
          axisLabel: {
            color: hasBenchmark ? 'rgba(148,163,184,0.55)' : '#64748b',
            fontSize: 10,
            formatter: hasBenchmark ? ((v: number) => v.toFixed(0)) : (isCandidateExecution ? valueFmt.format : axisMoneyFmt),
          },
          splitLine: { lineStyle: { color: '#1e293b' } },
          axisLine: { show: false },
        },
        {
          type: 'value', gridIndex: 0,
          position: 'right',
          scale: true,
          name: hasBenchmark ? '策略资金' : '',
          nameTextStyle: { color: '#64748b', fontSize: 10, padding: [0, 0, 4, 0] },
          axisLabel: {
            show: hasBenchmark,
            color: '#64748b',
            fontSize: 10,
            formatter: axisMoneyFmt,
          },
          splitLine: { show: false },
          axisLine: { show: false },
        },
        {
          type: 'value', gridIndex: 1,
          position: 'right',
          max: 0,
          axisLabel: {
            color: '#64748b', fontSize: 10,
            formatter: (v: number) => `${v.toFixed(1)}%`,
          },
          splitLine: { lineStyle: { color: '#1e293b' } },
          axisLine: { show: false },
        },
      ],
      dataZoom: [
        {
          type: 'inside',
          xAxisIndex: [0, 1],
          filterMode: 'filter',
          zoomOnMouseWheel: true,
          moveOnMouseMove: true,
          moveOnMouseWheel: false,
        },
        {
          type: 'slider',
          xAxisIndex: [0, 1],
          filterMode: 'filter',
          height: 16,
          bottom: 10,
          borderColor: 'rgba(148,163,184,0.18)',
          backgroundColor: 'rgba(15,23,42,0.55)',
          fillerColor: 'rgba(59,130,246,0.18)',
          handleStyle: { color: '#64748b', borderColor: '#94a3b8' },
          textStyle: { color: '#64748b', fontSize: 10 },
          brushSelect: false,
        },
      ],
      tooltip: {
        trigger: 'axis',
        backgroundColor: 'rgba(15,23,42,0.95)',
        borderColor: 'rgba(148,163,184,0.2)',
        textStyle: { color: '#e2e8f0', fontSize: 12 },
        formatter: (params: any) => {
          const date = params[0]?.axisValue ?? ''
          let html = `<div style="font-size:11px;color:#94a3b8;margin-bottom:4px">${date}</div>`
          for (const p of params) {
            if (p.value == null) continue
            const isDrawdown = p.seriesName === '回撤'
            const isBenchmark = p.seriesName === `同期${benchmarkName}`
            const isSampleCurve = isCandidateExecution && p.seriesName === navLabel
            html += `<div style="display:flex;justify-content:space-between;gap:16px">
              <span style="color:${p.color}">${p.seriesName}</span>
              <span style="font-family:monospace">${
                isDrawdown
                  ? `${(p.value as number).toFixed(2)}%`
                  : isBenchmark
                    ? `${valueFmt.format(p.value as number)} 点`
                    : isSampleCurve
                      ? valueFmt.format(p.value as number)
                      : moneyFmt.format(p.value as number)
              }</span>
            </div>`
          }
          return html
        },
      },
      series: [
        {
          name: navLabel,
          type: 'line',
          xAxisIndex: 0,
          yAxisIndex: hasBenchmark ? 1 : 0,
          data: navValues,
          symbol: 'none',
          lineStyle: { color: '#3b82f6', width: 2.2 },
          areaStyle: {
            color: {
              type: 'linear', x: 0, y: 0, x2: 0, y2: 1,
              colorStops: [
                { offset: 0, color: 'rgba(59,130,246,0.15)' },
                { offset: 1, color: 'rgba(59,130,246,0.01)' },
              ],
            } as any,
          },
        },
        ...(hasBenchmark ? [{
          name: `同期${benchmarkName}`,
          type: 'line',
          xAxisIndex: 0,
          yAxisIndex: 0,
          data: benchmarkValues,
          symbol: 'none',
          connectNulls: true,
          lineStyle: { color: 'rgba(148,163,184,0.45)', width: 1, type: 'dashed' },
        }] : []),
        {
          name: '回撤',
          type: 'line',
          xAxisIndex: 1,
          yAxisIndex: 2,
          data: ddValues,
          symbol: 'none',
          lineStyle: { color: 'rgba(240,68,56,0.6)', width: 1 },
          areaStyle: { color: 'rgba(240,68,56,0.12)' },
        },
      ],
    } as any
  }, [result.equity_curve, result.drawdown_curve, result.benchmark_curve, result.run_id, isCandidateExecution, navLabel, navAxisLabel, allowsBenchmark])

  const chartRef = useECharts(option, [result.run_id])

  return (
    <div>
      <div className="flex flex-wrap items-center gap-4 px-4 pb-2">
        <span className="flex items-center gap-1.5 text-[10px] text-secondary">
          <span className="w-3 h-0.5 rounded bg-accent" />
          {navLabel}
        </span>
        <span className="flex items-center gap-1.5 text-[10px] text-secondary">
          <span className="w-3 h-0.5 rounded bg-red-400/60" />
          回撤
        </span>
        {hasBenchmarkLegend && (
          <span className="flex items-center gap-1.5 text-[10px] text-secondary">
            <span className="w-3 h-0.5 rounded border-t border-dashed border-slate-400/60" />
            同期{result.benchmark_curve?.[0]?.name ?? '基准'}
          </span>
        )}
        <span className="ml-auto text-[10px] text-muted">滚轮缩放 · 拖动平移</span>
      </div>
      <div ref={chartRef} className="h-[282px]" />
    </div>
  )
}

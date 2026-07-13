import { useMemo } from 'react'
import { useECharts } from './useECharts'
import type { StrategyBacktestResult } from '@/lib/api'
import { useChartTheme } from '@/lib/theme'
import { useIsNarrowViewport } from '@/lib/sidebarState'

interface Props {
  result: StrategyBacktestResult
}

export function StrategyNavChart({ result }: Props) {
  const ct = useChartTheme()
  const compact = useIsNarrowViewport(640)
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
    const hasBenchmark = benchmarkValues.some(v => v != null)
    const ddValues = result.drawdown_curve.map(r => r.value * 100)
    const xLabelInterval = Math.max(0, Math.ceil(dates.length / (compact ? 3 : 6)) - 1)

    return {
      animation: false,
      axisPointer: {
        link: [{ xAxisIndex: 'all' }],
        label: { backgroundColor: ct.crosshairLabelBg },
      },
      grid: [
        { left: compact ? 42 : 64, right: hasBenchmark ? (compact ? 42 : 64) : (compact ? 8 : 16), top: 14, bottom: '40%' },
        { left: compact ? 42 : 64, right: hasBenchmark ? (compact ? 42 : 64) : (compact ? 8 : 16), top: '68%', bottom: compact ? 20 : 46 },
      ],
      xAxis: [
        {
          type: 'category', data: dates, gridIndex: 0,
          axisLabel: { show: false }, axisTick: { show: false },
          axisPointer: { show: true, type: 'line' },
          axisLine: { lineStyle: { color: ct.border } },
        },
        {
          type: 'category', data: dates, gridIndex: 1,
          axisLabel: {
            color: ct.text,
            fontSize: compact ? 9 : 10,
            interval: xLabelInterval,
            hideOverlap: true,
            formatter: compact ? ((value: string) => value.slice(5)) : undefined,
          },
          axisTick: { show: false },
          axisPointer: { show: true, type: 'line' },
          axisLine: { lineStyle: { color: ct.border } },
        },
      ],
      yAxis: [
        {
          type: 'value', gridIndex: 0,
          scale: true,
          name: compact ? '' : hasBenchmark ? '上证点位' : '策略资金',
          nameTextStyle: { color: hasBenchmark ? ct.text : ct.text, fontSize: 10, padding: [0, 0, 4, 0] },
          axisLabel: {
            color: hasBenchmark ? ct.text : ct.text,
            fontSize: 10,
            formatter: hasBenchmark ? ((v: number) => v.toFixed(0)) : axisMoneyFmt,
          },
          splitLine: { lineStyle: { color: ct.grid } },
          axisLine: { show: false },
        },
        {
          type: 'value', gridIndex: 0,
          position: 'right',
          scale: true,
          name: compact ? '' : hasBenchmark ? '策略资金' : '',
          nameTextStyle: { color: ct.text, fontSize: 10, padding: [0, 0, 4, 0] },
          axisLabel: {
            show: hasBenchmark,
            color: ct.text,
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
            color: ct.text, fontSize: 10,
            formatter: (v: number) => `${v.toFixed(1)}%`,
          },
          splitLine: { lineStyle: { color: ct.grid } },
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
          show: !compact,
          xAxisIndex: [0, 1],
          filterMode: 'filter',
          height: 16,
          bottom: 10,
          borderColor: ct.border,
          backgroundColor: ct.zoomFill,
          fillerColor: 'rgba(59,130,246,0.18)',
          handleStyle: { color: ct.text, borderColor: '#94a3b8' },
          textStyle: { color: ct.text, fontSize: 10 },
          brushSelect: false,
        },
      ],
      tooltip: {
        trigger: 'axis',
        confine: true,
        backgroundColor: ct.tooltipBg,
        borderColor: ct.tooltipBorder,
        textStyle: { color: ct.tooltipText, fontSize: 12 },
        formatter: (params: any) => {
          const date = params[0]?.axisValue ?? ''
          let html = `<div style="font-size:11px;color:${ct.text};margin-bottom:4px">${date}</div>`
          for (const p of params) {
            if (p.value == null) continue
            const isDrawdown = p.seriesName === '回撤'
            const isBenchmark = p.seriesName === '同期上证指数'
            html += `<div style="display:flex;justify-content:space-between;gap:16px">
              <span style="color:${p.color}">${p.seriesName}</span>
              <span style="font-family:monospace">${
                isDrawdown
                  ? `${(p.value as number).toFixed(2)}%`
                  : isBenchmark
                    ? `${valueFmt.format(p.value as number)} 点`
                    : moneyFmt.format(p.value as number)
              }</span>
            </div>`
          }
          return html
        },
      },
      series: [
        {
          name: '净值',
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
          name: '同期上证指数',
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
  }, [result.equity_curve, result.drawdown_curve, result.benchmark_curve, result.run_id, ct, compact])

  const chartRef = useECharts(option, [result.run_id, ct, compact])

  return (
    <div>
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 px-2 pb-2 sm:gap-x-4 sm:px-4">
        <span className="flex items-center gap-1.5 text-[10px] text-secondary">
          <span className="w-3 h-0.5 rounded bg-accent" />
          策略净值
        </span>
        <span className="flex items-center gap-1.5 text-[10px] text-secondary">
          <span className="w-3 h-0.5 rounded bg-red-400/60" />
          回撤
        </span>
        {(result.benchmark_curve?.length ?? 0) > 0 && (
          <span className="flex items-center gap-1.5 text-[10px] text-secondary">
            <span className="w-3 h-0.5 rounded border-t border-dashed border-slate-400/60" />
            同期上证指数
          </span>
        )}
        <span className="ml-auto hidden text-[10px] text-muted sm:inline">滚轮缩放 · 拖动平移</span>
      </div>
      <div ref={chartRef} className="h-[248px] sm:h-[282px]" />
    </div>
  )
}

import { useMemo } from 'react'
import { useECharts } from './useECharts'
import type { FactorBacktestResult } from '@/lib/api'
import { useChartTheme } from '@/lib/theme'

const GROUP_COLORS = [
  '#6366f1', // Q1 indigo
  '#8b5cf6', // Q2 violet
  '#f59e0b', // Q3 amber
  '#f97316', // Q4 orange
  '#ef4444', // Q5 red
  '#ec4899', // Q6
  '#14b8a6', // Q7
  '#06b6d4', // Q8
  '#84cc16', // Q9
  '#a855f7', // Q10
]

interface Props {
  result: FactorBacktestResult
}

function groupSortKey(group: string): number {
  return group.startsWith('Q') ? Number(group.slice(1)) || 0 : 0
}

function getGroupCols(row: Record<string, any>): string[] {
  return Object.keys(row).filter(k => k !== 'date').sort((a, b) => groupSortKey(a) - groupSortKey(b))
}

export function FactorGroupNavChart({ result }: Props) {
  const ct = useChartTheme()
  const option = useMemo(() => {
    if (!result.group_nav.length) return null

    const dates = result.group_nav.map(r => (r.date as string).slice(0, 10))
    const groupCols = getGroupCols(result.group_nav[0])

    // 多空净值
    const lsNav = result.long_short_nav
    const hasLS = lsNav && lsNav.length > 0

    const series = groupCols.map((col, i) => ({
      name: col,
      type: 'line',
      data: result.group_nav.map(r => r[col]),
      symbol: 'none',
        lineStyle: { color: GROUP_COLORS[i % GROUP_COLORS.length], width: 1.5 } as any,
        itemStyle: { color: GROUP_COLORS[i % GROUP_COLORS.length] } as any,
    }))

    if (hasLS) {
      series.push({
        name: '多空',
        type: 'line',
        data: lsNav.map(r => r.value),
        symbol: 'none',
        lineStyle: { color: '#fbbf24', width: 2, type: 'dashed' },
        itemStyle: { color: '#fbbf24' },
      })
    }

    return {
      animation: false,
      legend: {
        show: false,
      },
      grid: { left: 56, right: 16, top: 12, bottom: 28 },
      tooltip: {
        trigger: 'axis',
        backgroundColor: ct.tooltipBg,
        borderColor: ct.tooltipBorder,
        textStyle: { color: ct.tooltipText, fontSize: 12 },
        formatter: (params: any) => {
          const date = params[0]?.axisValue ?? ''
          let html = `<div style="font-size:11px;color:${ct.text};margin-bottom:4px">${date}</div>`
          for (const p of params) {
            if (p.value == null) continue
            html += `<div style="display:flex;justify-content:space-between;gap:16px">
              <span style="display:flex;align-items:center;gap:4px">
                <span style="width:8px;height:3px;border-radius:1px;background:${p.color};display:inline-block"></span>
                ${p.seriesName}
              </span>
              <span style="font-family:monospace">${(p.value as number).toFixed(4)}</span>
            </div>`
          }
          return html
        },
      },
      xAxis: {
        type: 'category',
        data: dates,
        axisLabel: { color: ct.text, fontSize: 10, interval: Math.floor(dates.length / 6) },
        axisLine: { lineStyle: { color: ct.border } },
        axisTick: { show: false },
      },
      yAxis: {
        type: 'value',
        scale: true,
        axisLabel: { color: ct.text, fontSize: 10 },
        splitLine: { lineStyle: { color: ct.grid } },
        axisLine: { show: false },
      },
      series,
    } as any
  }, [result.group_nav, result.long_short_nav, result.run_id, ct])

  const chartRef = useECharts(option, [result.run_id, ct])

  // 图例
  const groupCols = result.group_nav.length > 0
    ? getGroupCols(result.group_nav[0])
    : []

  return (
    <div>
      <div className="flex items-center gap-3 px-4 pb-2">
        {groupCols.map((col, i) => (
          <span key={col} className="flex items-center gap-1 text-[10px] text-secondary">
            <span className="w-2 h-2 rounded-full" style={{ backgroundColor: GROUP_COLORS[i % GROUP_COLORS.length] }} />
            {col}
          </span>
        ))}
        {result.long_short_nav?.length > 0 && (
          <span className="flex items-center gap-1 text-[10px] text-secondary">
            <span className="w-2 h-0.5 rounded bg-yellow-400" style={{ borderTop: '2px dashed #fbbf24' }} />
            多空
          </span>
        )}
      </div>
      <div ref={chartRef} className="h-[280px]" />
    </div>
  )
}

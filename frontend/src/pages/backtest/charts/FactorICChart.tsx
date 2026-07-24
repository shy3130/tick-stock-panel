import { useMemo } from 'react'
import { useECharts } from './useECharts'
import type { FactorBacktestResult } from '@/lib/api'
import { useChartTheme } from '@/lib/theme'

interface Props {
  result: FactorBacktestResult
}

export function FactorICChart({ result }: Props) {
  const ct = useChartTheme()
  const option = useMemo(() => {
    if (!result.ic_series.length) return null

    const dates = result.ic_series.map(r => r.date.slice(0, 10))
    const values = result.ic_series.map(r => r.ic)

    // 12期移动平均
    const maWindow = 12
    const ma: (number | null)[] = values.map((_, i) => {
      if (i < maWindow - 1) return null
      const slice = values.slice(i - maWindow + 1, i + 1)
      return slice.reduce((a, b) => a + b, 0) / slice.length
    })

    return {
      animation: false,
      grid: { left: 50, right: 16, top: 16, bottom: 28 },
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
              <span style="color:${p.color}">${p.seriesName}</span>
              <span style="font-family:monospace">${(p.value * 100).toFixed(2)}%</span>
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
        axisLabel: { color: ct.text, fontSize: 10, formatter: (v: number) => `${(v * 100).toFixed(0)}%` },
        splitLine: { lineStyle: { color: ct.grid } },
        axisLine: { show: false },
      },
      series: [
        {
          name: 'IC',
          type: 'bar',
          data: values.map(v => ({
            value: v,
            itemStyle: {
              color: v >= 0
                ? 'rgba(240,68,56,0.6)'
                : 'rgba(18,183,106,0.6)',
            },
          })),
          barMaxWidth: 6,
        },
        {
          name: `MA${maWindow}`,
          type: 'line',
          data: ma,
          smooth: true,
          symbol: 'none',
          lineStyle: { color: '#f59e0b', width: 1.5 },
          z: 10,
        },
      ],
    } as any
  }, [result.ic_series, ct])

  const chartRef = useECharts(option, [result.run_id, ct])

  return <div ref={chartRef} className="h-[200px]" />
}

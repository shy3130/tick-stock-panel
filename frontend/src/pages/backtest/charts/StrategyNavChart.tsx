import { useMemo, useRef, useState } from 'react'
import * as echarts from 'echarts'
import { useECharts } from './useECharts'
import type { StrategyBacktestResult } from '@/lib/api'
import { useChartTheme } from '@/lib/theme'

interface Props {
  result: StrategyBacktestResult
}

export function StrategyNavChart({ result }: Props) {
  const ct = useChartTheme()
  // 可点击隐藏的图例(series name 为 key)。策略净值/回撤保持常显。
  const [hidden, setHidden] = useState<Set<string>>(new Set())
  // 保存上一次 dataZoom 的 start/end, 切换图例重绘(notMerge:true)时回填, 避免缩放窗口复位。
  const containerRef = useRef<HTMLDivElement>(null)
  const toggleLegend = (name: string) =>
    setHidden(prev => {
      const next = new Set(prev)
      if (next.has(name)) next.delete(name)
      else next.add(name)
      return next
    })
  const option = useMemo(() => {
    if (!result.equity_curve.length) return null

    // 回填上次 dataZoom 的 start/end, 切换图例(notMerge:true 全量重绘)时不丢失缩放窗口。
    const prevInstance = containerRef.current ? echarts.getInstanceByDom(containerRef.current) : null
    const prevZoom = (prevInstance?.getOption() as any)?.dataZoom?.[0]
    const zoomStart = prevZoom && typeof prevZoom.start === 'number' ? prevZoom.start : undefined
    const zoomEnd = prevZoom && typeof prevZoom.end === 'number' ? prevZoom.end : undefined

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
    const positionValues = result.equity_curve.map(r => {
      if (r.exposure != null) return r.exposure * 100
      if (r.cash != null && r.value > 0) {
        return Math.max(0, Math.min(100, (1 - r.cash / r.value) * 100))
      }
      return null
    })
    const hasPosition = positionValues.some(v => v != null)
    const navColor = '#3b82f6'
    const benchmarkColor = '#64748b'
    const drawdownColor = '#f04438'
    const positionColor = '#f59e0b'

    return {
      animation: false,
      axisPointer: {
        link: [{ xAxisIndex: 'all' }],
        label: { backgroundColor: ct.crosshairLabelBg },
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
          axisLine: { lineStyle: { color: ct.border } },
        },
        {
          type: 'category', data: dates, gridIndex: 1,
          axisLabel: { color: ct.text, fontSize: 10, interval: Math.floor(dates.length / 6) },
          axisTick: { show: false },
          axisPointer: { show: true, type: 'line' },
          axisLine: { lineStyle: { color: ct.border } },
        },
      ],
      yAxis: [
        {
          type: 'value', gridIndex: 0,
          scale: true,
          name: hasBenchmark ? '上证点位' : '策略资金',
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
          name: hasBenchmark ? '策略资金' : '',
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
        ...(hasPosition ? [{
          type: 'value',
          gridIndex: 0,
          min: 0,
          max: 100,
          show: false,
          splitLine: { show: false },
          axisLine: { show: false },
        }] : []),
      ],
      dataZoom: [
        {
          type: 'inside',
          xAxisIndex: [0, 1],
          filterMode: 'filter',
          zoomOnMouseWheel: true,
          moveOnMouseMove: true,
          moveOnMouseWheel: false,
          ...(zoomStart !== undefined ? { start: zoomStart } : {}),
          ...(zoomEnd !== undefined ? { end: zoomEnd } : {}),
        },
        {
          type: 'slider',
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
          ...(zoomStart !== undefined ? { start: zoomStart } : {}),
          ...(zoomEnd !== undefined ? { end: zoomEnd } : {}),
        },
      ],
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
            const isDrawdown = p.seriesName === '回撤'
            const isBenchmark = p.seriesName === '同期上证指数'
            const isPosition = p.seriesName === '仓位'
            html += `<div style="display:flex;justify-content:space-between;gap:16px">
              <span style="color:${p.color}">${p.seriesName}</span>
              <span style="font-family:monospace">${
                isDrawdown || isPosition
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
          itemStyle: { color: navColor },
          lineStyle: { color: navColor, width: 2.2 },
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
        ...(hasBenchmark && !hidden.has('同期上证指数') ? [{
          name: '同期上证指数',
          type: 'line',
          xAxisIndex: 0,
          yAxisIndex: 0,
          data: benchmarkValues,
          symbol: 'none',
          connectNulls: true,
          itemStyle: { color: benchmarkColor },
          lineStyle: { color: benchmarkColor, opacity: 0.55, width: 1, type: 'dashed' },
        }] : []),
        {
          name: '回撤',
          type: 'line',
          xAxisIndex: 1,
          yAxisIndex: 2,
          data: ddValues,
          symbol: 'none',
          itemStyle: { color: drawdownColor },
          lineStyle: { color: drawdownColor, opacity: 0.65, width: 1 },
          areaStyle: { color: 'rgba(240,68,56,0.12)' },
        },
        ...(hasPosition && !hidden.has('仓位') ? [{
          name: '仓位',
          type: 'line',
          step: 'end',
          xAxisIndex: 0,
          yAxisIndex: 3,
          data: positionValues,
          symbol: 'none',
          itemStyle: { color: positionColor },
          lineStyle: { color: positionColor, width: 1.2 },
          areaStyle: { color: 'rgba(245,158,11,0.08)' },
          z: 1,
        }] : []),
      ],
    } as any
  }, [result.equity_curve, result.drawdown_curve, result.benchmark_curve, result.run_id, ct, hidden])

  const chartRef = useECharts(option, [result.run_id, ct], containerRef)

  return (
    <div>
      <div className="flex flex-wrap items-center gap-4 px-4 pb-2">
        <span className="flex items-center gap-1.5 text-[10px] text-secondary">
          <span className="w-3 h-0.5 rounded bg-[#3b82f6]" />
          策略净值
        </span>
        <span className="flex items-center gap-1.5 text-[10px] text-secondary">
          <span className="w-3 h-0.5 rounded bg-[#f04438]" />
          回撤
        </span>
        {result.equity_curve.some(r => r.exposure != null || (r.cash != null && r.value > 0)) && (
          <button
            type="button"
            onClick={() => toggleLegend('仓位')}
            title="点击显示/隐藏"
            className={`flex items-center gap-1.5 text-[10px] text-secondary cursor-pointer transition-opacity ${
              hidden.has('仓位') ? 'opacity-40' : 'opacity-100'
            }`}
          >
            <span className="w-3 h-0.5 rounded bg-[#f59e0b]" />
            仓位
          </button>
        )}
        {(result.benchmark_curve?.length ?? 0) > 0 && (
          <button
            type="button"
            onClick={() => toggleLegend('同期上证指数')}
            title="点击显示/隐藏"
            className={`flex items-center gap-1.5 text-[10px] text-secondary cursor-pointer transition-opacity ${
              hidden.has('同期上证指数') ? 'opacity-40' : 'opacity-100'
            }`}
          >
            <span className="w-3 h-0.5 rounded border-t border-dashed border-[#64748b]" />
            同期上证指数
          </button>
        )}
        <span className="ml-auto text-[10px] text-muted">滚轮缩放 · 拖动平移</span>
      </div>
      <div ref={chartRef} className="h-[282px]" />
    </div>
  )
}

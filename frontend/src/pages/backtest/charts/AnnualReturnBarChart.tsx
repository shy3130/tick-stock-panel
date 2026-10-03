import { useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { useECharts } from './useECharts'
import type { EChartsOption } from 'echarts'
import { api } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { useChartTheme } from '@/lib/theme'
import { analyzeReturns, fmtRet, sliceCurveFrom, type EquityPoint, type ReturnsAnalysis } from '../returnsAnalysis'

// 叠加指数候选 — 与 FundFlowChart 的 OVERLAY_INDEXES 清单一致 (有本地 kline_index 数据即可出)
const OVERLAY_INDEXES = [
  { symbol: '000001.SH', name: '上证指数' },
  { symbol: '000300.SH', name: '沪深300' },
  { symbol: '399006.SZ', name: '创业板指' },
  { symbol: '000688.SH', name: '科创50' },
  { symbol: '399001.SZ', name: '深证成指' },
] as const

const LS_KEY = 'backtest_returns_index'
const BENCHMARK_SYMBOL = '000001.SH'

// 涨跌色 — 与 index.css --bull/--bear (#F04438/#12B76A) 一致; 画布不吃 CSS 变量需硬编码
const BULL = '#F04438'
const BEAR = '#12B76A'

interface Props {
  analysis: ReturnsAnalysis
  /** 回测结果自带的上证基准曲线；选上证时零请求直接复用 (与净值曲线图同口径) */
  benchmarkCurve?: { date: string; value: number; close?: number }[] | null
  /** 回测起止区间 — 叠加指数按同一窗口切片对齐 */
  range: { start: string; end: string }
}

function padStart(start: string, days: number): string {
  const d = new Date(`${start}T00:00:00`)
  if (Number.isNaN(d.getTime())) return start
  d.setDate(d.getDate() - days)
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`
}

/**
 * 年度收益横向直方图 — 0 轴居中，正红负绿；
 * 平均年度收益用虚线 markLine 标记；不完整年度（回测截止年）降透明度并标注“截至M月”。
 * 最新年份在最上方。可叠加指数年度收益（菱形 marker + tooltip 超额对比）。
 */
export function AnnualReturnBarChart({ analysis, benchmarkCurve, range }: Props) {
  const ct = useChartTheme()
  const [indexSymbol, setIndexSymbol] = useState<string>(() => {
    try { return localStorage.getItem(LS_KEY) ?? BENCHMARK_SYMBOL } catch { return BENCHMARK_SYMBOL }
  })
  const onIndexChange = (v: string) => {
    setIndexSymbol(v)
    try { localStorage.setItem(LS_KEY, v) } catch { /* ignore */ }
  }

  // 上证且回测结果自带基准曲线 → 直接复用，不发请求
  const useBuiltinBenchmark = indexSymbol === BENCHMARK_SYMBOL && (benchmarkCurve?.length ?? 0) > 0
  const fetchStart = useMemo(() => padStart(range.start, 15), [range.start])
  const indexQ = useQuery({
    queryKey: QK.indexDaily(indexSymbol, fetchStart, range.end),
    queryFn: () => api.indexDaily(indexSymbol, 2000, range.end ? { start: fetchStart, end: range.end } : undefined),
    enabled: indexSymbol !== '' && !useBuiltinBenchmark,
    staleTime: 300_000,
  })

  // 叠加指数年度收益: close 序列切到回测窗口, 与策略“全年”同口径 (start 前最后收盘为基准)
  const overlay = useMemo(() => {
    if (!indexSymbol) return null
    let raw: EquityPoint[] | null = null
    if (useBuiltinBenchmark) {
      raw = (benchmarkCurve ?? []).map(r => ({ date: r.date, value: Number(r.close ?? r.value) }))
    } else if (indexQ.data?.rows) {
      raw = indexQ.data.rows.map(r => ({ date: r.date, value: Number(r.close) }))
    }
    if (!raw || raw.length === 0) return null
    const { curve, baseline } = sliceCurveFrom(raw, range.start)
    const a = analyzeReturns(curve, baseline)
    if (!a) return null
    return new Map(a.years.map(y => [y.year, y.annual]))
  }, [indexSymbol, useBuiltinBenchmark, benchmarkCurve, indexQ.data, range.start])

  const overlayName = OVERLAY_INDEXES.find(i => i.symbol === indexSymbol)?.name ?? ''

  const option = useMemo<EChartsOption>(() => {
    // yAxis category 自下而上，倒序写入使最新年在顶部
    const rows = [...analysis.years].reverse()
    const cats = rows.map(r => (r.partial ? `${r.year} (截至${r.lastMonth}月)` : `${r.year}`))
    const vals = rows.map(r => ({
      value: Number((r.annual * 100).toFixed(2)),
      itemStyle: {
        color: r.annual >= 0 ? BULL : BEAR,
        opacity: r.partial ? 0.6 : 1,
      },
      // 负值 bar 向左延伸，标签放左端避免与 0 轴重叠
      label: { position: (r.annual >= 0 ? 'right' : 'left') as 'right' | 'left' },
    }))
    const overlayData = overlay
      ? rows
          .map((r, i) => {
            const v = overlay.get(r.year)
            return v != null ? { value: [Number((v * 100).toFixed(2)), i] as [number, number] } : null
          })
          .filter((d): d is { value: [number, number] } => d != null)
      : []

    return {
      grid: { left: 92, right: 56, top: 20, bottom: 24 },
      tooltip: {
        trigger: 'axis',
        axisPointer: { type: 'shadow' },
        backgroundColor: ct.tooltipBg,
        borderColor: ct.tooltipBorder,
        textStyle: { color: ct.tooltipText, fontSize: 12 },
        formatter: (params: any) => {
          const ps = Array.isArray(params) ? params : [params]
          const barP = ps.find((p: any) => p.seriesType === 'bar') ?? ps[0]
          const row = rows[barP?.dataIndex]
          if (!row) return ''
          let html = `${row.year}年${row.partial ? ` (截至${row.lastMonth}月)` : ''}<br/>策略: ${fmtRet(row.annual)}%`
          const ov = overlay?.get(row.year)
          if (ov != null) {
            html += `<br/>${overlayName}: ${fmtRet(ov)}%<br/>超额: ${fmtRet(row.annual - ov)}%`
          }
          return html
        },
      },
      xAxis: {
        type: 'value',
        axisLabel: { color: ct.text, fontSize: 10, formatter: (v: number) => `${v}%` },
        splitLine: { lineStyle: { color: ct.grid } },
        axisLine: { show: false },
      },
      yAxis: {
        type: 'category',
        data: cats,
        axisLabel: { color: ct.text, fontSize: 10, fontWeight: 'bold' },
        axisLine: { lineStyle: { color: ct.border } },
        axisTick: { show: false },
      },
      series: [
        {
          name: '策略',
          type: 'bar',
          data: vals,
          barWidth: '55%',
          label: {
            show: true,
            position: 'right',
            color: ct.text,
            fontSize: 10,
            formatter: (p: any) => `${(p.value as number).toFixed(1)}%`,
          },
          markLine:
            analysis.avgAnnual != null
              ? {
                  symbol: 'none',
                  silent: true,
                  lineStyle: { color: ct.text, type: 'dashed', width: 1, opacity: 0.8 },
                  label: {
                    color: ct.text,
                    fontSize: 10,
                    formatter: `平均 ${fmtRet(analysis.avgAnnual)}%`,
                    position: 'insideEndTop',
                  },
                  data: [{ xAxis: Number((analysis.avgAnnual * 100).toFixed(2)) }],
                }
              : undefined,
        },
        ...(overlayData.length > 0
          ? [{
              name: overlayName,
              type: 'scatter',
              symbol: 'diamond',
              symbolSize: 9,
              data: overlayData,
              itemStyle: { color: '#64748b' },
              label: {
                show: true,
                position: 'top',
                distance: 4,
                fontSize: 9,
                color: ct.text,
                formatter: (p: any) => `${(p.value[0] as number).toFixed(1)}%`,
              },
              z: 3,
            } as const]
          : []),
      ],
    }
  }, [analysis, overlay, overlayName, ct])

  const chartRef = useECharts(option, [analysis, overlay, overlayName, ct])

  const height = Math.max(120, analysis.years.length * 34 + 48)
  return (
    <div>
      <div className="mb-1 flex items-center justify-between">
        <span className="text-xs font-medium text-secondary">
          年度收益（%）
          {overlayName && <span className="ml-2 text-[10px] font-normal text-muted">◆ {overlayName}</span>}
        </span>
        <select
          aria-label="指数叠加"
          className="h-6 max-w-28 truncate rounded border border-border bg-surface px-1 text-[10px] text-secondary outline-none focus:border-accent"
          value={indexSymbol}
          onChange={e => onIndexChange(e.target.value)}
        >
          <option value="">不叠加</option>
          {OVERLAY_INDEXES.map(i => <option key={i.symbol} value={i.symbol}>{i.name}</option>)}
        </select>
      </div>
      <div ref={chartRef} className="w-full" style={{ height }} />
    </div>
  )
}

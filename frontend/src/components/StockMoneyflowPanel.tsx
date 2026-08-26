import { useEffect, useMemo, useRef } from 'react'
import { useQuery } from '@tanstack/react-query'
import * as echarts from 'echarts'
import type { ECharts, EChartsOption } from 'echarts'
import {
  api,
  type MarketDataMoneyflowStockRow,
} from '@/lib/api'
import { QK } from '@/lib/queryKeys'

/**
 * 大单净额面板（资金行为视图）。
 *
 * 参考「资金行为分析系统02——三大模块」的模块一（大单净额面板）：
 * 展示最近 N 个交易日的超大单/大单/中单/小单净额与主力净额趋势，
 * 用于观察筹码稳定度。数据来自已发布日级资金流快照，source 字段全程
 * 透传展示。
 *
 * 注意：笔记的模块二（主动/被动四维流向）与模块三（换手度）依赖
 * 逐笔方向字段，该字段语义待上游统一（见 StockTransScatter 注释），
 * 暂不实现。
 */

const RECENT_DAYS = 6
/** 请求窗口：30 个自然日覆盖长假后的最近 6 个交易日。 */
const LOOKBACK_DAYS = 30

interface MoneyflowTier {
  key: 'super_large_net' | 'large_net' | 'medium_net' | 'small_net'
  label: string
  color: string
}

const TIERS: MoneyflowTier[] = [
  { key: 'super_large_net', label: '超大单', color: '#EF4444' },
  { key: 'large_net', label: '大单', color: '#F59E0B' },
  { key: 'medium_net', label: '中单', color: '#60A5FA' },
  { key: 'small_net', label: '小单', color: '#A1A1AA' },
]

type MoneyflowFieldKey = MoneyflowTier['key'] | 'main_traditional_net'

const FLOW_FIELDS: Array<{ key: MoneyflowFieldKey; label: string }> = [
  ...TIERS,
  { key: 'main_traditional_net', label: '主力净额' },
]

function fmtAmt(v: number): string {
  const abs = Math.abs(v)
  const sign = v < 0 ? '-' : ''
  if (abs >= 100_000_000) return `${sign}${(abs / 100_000_000).toFixed(2)}亿`
  if (abs >= 10_000) return `${sign}${(abs / 10_000).toFixed(0)}万`
  return `${sign}${abs.toFixed(0)}`
}

function isoDaysAgo(date: string, days: number): string {
  const d = new Date(`${date}T00:00:00`)
  d.setDate(d.getDate() - days)
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`
}

function isValidNum(v: number | null | undefined): v is number {
  return typeof v === 'number' && Number.isFinite(v)
}

interface Props {
  symbol: string
  date: string
  height?: number
}

export function StockMoneyflowPanel({ symbol, date, height = 300 }: Props) {
  const chartRef = useRef<HTMLDivElement>(null)
  const chart = useRef<ECharts | null>(null)

  const start = useMemo(() => isoDaysAgo(date, LOOKBACK_DAYS), [date])

  const flow = useQuery({
    queryKey: QK.marketDataMoneyflowStock(symbol, 'daily', start, date),
    queryFn: () => api.marketDataMoneyflowStock(symbol, { freq: 'daily', start, end: date }),
    enabled: !!symbol && !!date,
    retry: false,
  })

  const recent = useMemo<MarketDataMoneyflowStockRow[]>(() => {
    const rows = (flow.data?.rows ?? [])
      .filter(r => typeof r.trade_date === 'string' && r.trade_date)
      .sort((a, b) => String(a.trade_date).localeCompare(String(b.trade_date)))
    return rows.slice(-RECENT_DAYS)
  }, [flow.data?.rows])

  const missingFields = useMemo(
    () => FLOW_FIELDS.flatMap(field => {
      const missing = recent.filter(row => !isValidNum(row[field.key])).length
      return missing > 0 ? [`${field.label} ${missing}/${recent.length}`] : []
    }),
    [recent],
  )
  const hasFlowValues = recent.some(row =>
    FLOW_FIELDS.some(field => isValidNum(row[field.key])),
  )

  const option = useMemo<EChartsOption | null>(() => {
    if (recent.length === 0 || !hasFlowValues) return null
    const dates = recent.map(r => String(r.trade_date).slice(5))

    const series: EChartsOption['series'] = TIERS.map(tier => ({
      name: tier.label,
      type: 'bar',
      data: recent.map(r => {
        const v = r[tier.key]
        return isValidNum(v) ? v : null
      }),
      itemStyle: { color: tier.color },
      barMaxWidth: 14,
    }))

    // 主力净额（超大+大，传统口径）折线叠加
    series.push({
      name: '主力净额',
      type: 'line',
      data: recent.map(r => (isValidNum(r.main_traditional_net) ? r.main_traditional_net : null)),
      showSymbol: true,
      symbolSize: 5,
      lineStyle: { color: '#F43F5E', width: 1.5 },
      itemStyle: { color: '#F43F5E' },
      z: 5,
    })

    return {
      backgroundColor: 'transparent',
      animation: false,
      tooltip: {
        trigger: 'axis',
        axisPointer: { type: 'shadow' },
        valueFormatter: (v: unknown) =>
          typeof v === 'number' ? fmtAmt(v) : '—',
      },
      legend: {
        top: 0,
        right: 0,
        textStyle: { color: '#A1A1AA', fontSize: 10 },
        itemWidth: 10,
        itemHeight: 10,
        inactiveColor: '#52525B',
      },
      grid: { left: 60, right: 12, top: 26, bottom: 22 },
      xAxis: {
        type: 'category',
        data: dates,
        axisLine: { lineStyle: { color: '#27272A' } },
        axisLabel: { color: '#A1A1AA', fontSize: 10 },
      },
      yAxis: {
        type: 'value',
        axisLabel: {
          color: '#A1A1AA', fontSize: 10,
          formatter: (v: number) => fmtAmt(v),
        },
        splitLine: { lineStyle: { color: 'rgba(255,255,255,0.04)' } },
      },
      series,
    }
  }, [recent, hasFlowValues])

  useEffect(() => {
    const el = chartRef.current
    if (!el || !option) return

    const instance = echarts.init(el)
    chart.current = instance
    instance.setOption(option, true)

    const ro = new ResizeObserver(() => instance.resize())
    ro.observe(el)
    return () => {
      ro.disconnect()
      instance.dispose()
      if (chart.current === instance) chart.current = null
    }
  }, [option])

  const rowSources = [...new Set(recent
    .map(row => row.source)
    .filter((source): source is string => typeof source === 'string' && source.length > 0))]
  const sourceLabel = rowSources.length > 0
    ? rowSources.join(' / ')
    : flow.data?.source ?? '来源未标记'
  const apiSourceNote = flow.data?.source && !rowSources.includes(flow.data.source)
    ? ` · API ${flow.data.source}`
    : ''

  if (flow.isLoading) {
    return <div className="text-xs text-muted py-2">资金流加载中…</div>
  }
  if (flow.isError) {
    return (
      <div className="text-xs text-muted py-2">
        资金流数据不可用（{(flow.error as Error | null)?.message ?? '接口错误'}）
      </div>
    )
  }
  if (!option) {
    return (
      <div className="text-xs text-muted py-2">
        {recent.length > 0 && !hasFlowValues
          ? '日级资金流存在交易日，但大单分档字段全部缺失'
          : '无日级资金流数据'}
      </div>
    )
  }

  return (
    <div>
      <div ref={chartRef} style={{ width: '100%', height }} />
      <div className="text-[10px] font-mono text-muted/70 px-1">
        近 {recent.length} 个交易日 · 来源 {sourceLabel}{apiSourceNote}
      </div>
      {missingFields.length > 0 && (
        <div className="text-[10px] text-warning/80 px-1">
          分档字段缺失：{missingFields.join(' · ')}
        </div>
      )}
    </div>
  )
}

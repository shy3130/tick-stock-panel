import { useEffect, useMemo, useRef } from 'react'
import { useQuery } from '@tanstack/react-query'
import * as echarts from 'echarts'
import type { ECharts, EChartsOption } from 'echarts'
import {
  api,
  type MinuteKlineRow,
  type MarketDataTransactionRow,
} from '@/lib/api'
import { TRANSACTION_AMOUNT_TIERS, transactionAmountTier } from '@/lib/transactionTiers'
import { QK } from '@/lib/queryKeys'

/**
 * 逐笔成交散点（资金行为视图，金额维度）。
 *
 * 参考「资金行为分析系统01——逐笔成交可视化」的可观察形态，但只使用
 * 已验证可靠的字段（time/price/volume/amount/order_count）：
 * - x = 交易时间，y = 成交价，点大小/颜色按单笔金额分档
 * - 叠加当日分时价格线与均价线（复用 klineMinute 的 react-query 缓存）
 *
 * 刻意不展示买卖方向维度：逐笔 direction/side 字段在三个下游项目
 * (fm-cli / fquant / tickflow) 语义互相矛盾且无权威定义，实测与
 * tick-rule 相关性异常（疑似生产侧由价格推导）。在数据源统一语义
 * 之前展示方向会编造 provenance，违反项目红线。
 */

const TRANS_LIMIT = 20000


function fmtAmt(v: number): string {
  if (v >= 100_000_000) return `${(v / 100_000_000).toFixed(2)}亿`
  if (v >= 10_000) return `${(v / 10_000).toFixed(1)}万`
  return v.toFixed(0)
}

function fmtVol(v: number): string {
  // 逐笔 volume 为股数（mapping 契约），展示折算为手
  return `${Math.round(v / 100)}手`
}

/** 提取 HH:MM（兼容 "09:25:00" 与 ISO datetime）。 */
function timeOf(dt: string | null | undefined): string | null {
  if (!dt) return null
  const m = dt.match(/(\d{2}):(\d{2}):\d{2}/)
  if (m) return `${m[1]}:${m[2]}`
  return null
}

interface Props {
  symbol: string
  date: string
  height?: number
}

export function StockTransScatter({ symbol, date, height = 300 }: Props) {
  const chartRef = useRef<HTMLDivElement>(null)
  const chart = useRef<ECharts | null>(null)

  const trans = useQuery({
    queryKey: QK.marketDataTransactions(symbol, date, TRANS_LIMIT),
    queryFn: () => api.marketDataTransactions(symbol, { date, limit: TRANS_LIMIT }),
    enabled: !!symbol && !!date,
    retry: false,
  })

  // 分时线背景：同 symbol+date 的 klineMinute 与分时图共享缓存，不产生额外请求
  const minute = useQuery({
    queryKey: QK.klineMinute(symbol, date),
    queryFn: () => api.klineMinute(symbol, date),
    enabled: !!symbol && !!date,
  })

  const transRows: MarketDataTransactionRow[] = useMemo(
    () => trans.data?.rows ?? [], [trans.data?.rows])
  const minuteRows: MinuteKlineRow[] = useMemo(
    () => minute.data?.rows ?? [], [minute.data?.rows])

  const option = useMemo<EChartsOption | null>(() => {
    const points = transRows
      .map(r => ({ t: timeOf(r.datetime), price: r.price, amount: r.amount, volume: r.volume, orderCount: r.order_count }))
      .filter(p => p.t && typeof p.price === 'number' && p.price > 0)
    if (points.length === 0) return null

    // 按档分组为独立 series，图例可切换金额维度
    const scatterSeries = TRANSACTION_AMOUNT_TIERS.map((tier, idx) => ({
      name: tier.label,
      type: 'scatter' as const,
      data: points
        .filter(p => transactionAmountTier(p.amount) === tier)
        .map(p => [p.t, p.price, p.amount ?? 0, p.volume ?? 0, p.orderCount ?? 1]),
      symbolSize: tier.size,
      itemStyle: { color: tier.color, opacity: 0.85 },
      z: 3 + idx,
    }))

    // 分时价格线 + 均价线（有分时数据时作为背景参照）
    const hasMinute = minuteRows.length > 0
    const times: string[] = []
    const prices: [string, number][] = []
    const avgs: [string, number][] = []
    let sumAmt = 0
    let sumVol = 0
    for (const r of minuteRows) {
      const t = timeOf(r.datetime)
      if (!t) continue
      times.push(t)
      prices.push([t, r.close])
      sumAmt += r.amount
      sumVol += r.volume
      avgs.push([t, sumVol > 0 ? sumAmt / sumVol : r.close])
    }

    const series: EChartsOption['series'] = [...scatterSeries]
    if (hasMinute) {
      series.push(
        {
          name: '分时价',
          type: 'line',
          data: prices,
          showSymbol: false,
          lineStyle: { color: 'rgba(59,130,246,0.9)', width: 1.2 },
          z: 2,
          silent: true,
        },
        {
          name: '均价',
          type: 'line',
          data: avgs,
          showSymbol: false,
          lineStyle: { color: '#F59E0B', width: 1, type: 'dashed' },
          z: 2,
          silent: true,
        },
      )
    }

    const allTimes = [...new Set([...points.map(p => p.t!), ...times])].sort()

    return {
      backgroundColor: 'transparent',
      animation: false,
      tooltip: {
        trigger: 'item',
        formatter: (params: unknown) => {
          const p = params as { seriesName: string; value: [string, number, number, number, number] }
          if (p.seriesName === '分时价' || p.seriesName === '均价') return ''
          const [t, price, amount, volume, orderCount] = p.value
          const oc = orderCount > 1 ? `（${orderCount}笔合并）` : ''
          return [
            `<b>${t}</b>${oc}`,
            `价格 ${price.toFixed(2)}`,
            `数量 ${fmtVol(volume)}`,
            `金额 <b>${fmtAmt(amount)}</b>`,
          ].join('<br/>')
        },
      },
      legend: {
        top: 0,
        textStyle: { color: '#A1A1AA', fontSize: 10 },
        itemWidth: 10,
        itemHeight: 10,
        inactiveColor: '#52525B',
      },
      grid: { left: 56, right: 12, top: 26, bottom: 22 },
      xAxis: {
        type: 'category',
        data: allTimes,
        axisLine: { lineStyle: { color: '#27272A' } },
        axisLabel: { color: '#A1A1AA', fontSize: 10 },
        splitLine: { show: false },
      },
      yAxis: {
        type: 'value',
        scale: true,
        axisLabel: {
          color: '#A1A1AA', fontSize: 10,
          formatter: (v: number) => v.toFixed(2),
        },
        splitLine: { lineStyle: { color: 'rgba(255,255,255,0.04)' } },
      },
      dataZoom: [{
        type: 'inside',
      }],
      series,
    }
  }, [transRows, minuteRows])

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


  if (trans.isLoading) {
    return <div className="text-xs text-muted py-2">逐笔数据加载中…</div>
  }
  if (trans.isError) {
    return (
      <div className="text-xs text-muted py-2">
        逐笔数据不可用（{(trans.error as Error | null)?.message ?? '接口错误'}）
      </div>
    )
  }
  if (!option) {
    return <div className="text-xs text-muted py-2">该日无逐笔成交数据</div>
  }

  const minuteBaselineNote = minuteRows.length > 0
    ? null
    : minute.isLoading
      ? '分时基准加载中：散点暂未叠加分时价与均价'
      : minute.isError
        ? `分时基准不可用：散点未叠加分时价与均价（${(minute.error as Error | null)?.message ?? '接口错误'}）`
        : '当日无分时基准：散点未叠加分时价与均价'

  return (
    <div>
      <div ref={chartRef} style={{ width: '100%', height }} />
      {trans.data?.source && (
        <div className="text-[10px] font-mono text-muted/70 px-1">
          逐笔 {transRows.length} 笔 · {trans.data.source}
        </div>
      )}
      {minuteBaselineNote && (
        <div className="text-[10px] text-warning/80 px-1">{minuteBaselineNote}</div>
      )}
    </div>
  )
}

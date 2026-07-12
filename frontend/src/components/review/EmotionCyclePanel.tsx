/**
 * 情绪周期 —— 近 N 个交易日的市场情绪原始读数时序。
 *
 * 对齐 ../fquant 复盘的 EmotionDailyPoint 口径:只给原始计数(涨停/跌停/炸板/封板率/
 * 最高连板/成交额/涨跌家数),不给"情绪分"——情绪分是 Dashboard 雷达图的语义,
 * 按日回扫要重算概念排名,代价过高且不是复盘的口径。
 */
import { useMemo } from 'react'
import { useQuery } from '@tanstack/react-query'
import type { EChartsOption } from 'echarts'
import { Activity, RefreshCw } from 'lucide-react'

import { api, type ReviewDailyPoint } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { cn } from '@/lib/cn'
import { fmtBigNum } from '@/lib/format'
import { EmptyState } from '@/components/EmptyState'
import { useECharts } from '@/pages/backtest/charts/useECharts'
import {
  ReviewCard, DaysSwitch, Kpi, fmtPct1, pctTone, shortDate,
  BULL, BEAR, ACCENT, WARN, AXIS, GRID,
} from './shared'

function deltaOf(cur: number | null | undefined, prev: number | null | undefined): string | undefined {
  if (cur == null || prev == null) return undefined
  const d = cur - prev
  if (d === 0) return undefined
  return `${d > 0 ? '+' : ''}${d}`
}

/** 涨停/跌停/炸板柱 + 封板率折线(右轴) */
function LimitChart({ series }: { series: ReviewDailyPoint[] }) {
  const option = useMemo<EChartsOption>(() => ({
    grid: { left: 40, right: 44, top: 28, bottom: 28 },
    legend: {
      top: 0, right: 0, itemWidth: 8, itemHeight: 8,
      textStyle: { color: AXIS, fontSize: 10 },
      data: ['涨停', '跌停', '炸板', '封板率'],
    },
    tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' } },
    xAxis: {
      type: 'category',
      data: series.map(s => shortDate(s.trade_date)),
      axisLabel: { color: AXIS, fontSize: 9, interval: Math.max(0, Math.floor(series.length / 12)) },
      axisLine: { lineStyle: { color: GRID } },
    },
    yAxis: [
      {
        type: 'value', name: '家数',
        nameTextStyle: { color: AXIS, fontSize: 9 },
        axisLabel: { color: AXIS, fontSize: 9 },
        splitLine: { lineStyle: { color: GRID } },
      },
      {
        // 不设 name:右轴名会渲染在右上角、与 legend 相撞;legend 已标注"封板率"
        type: 'value', min: 0, max: 100,
        axisLabel: { color: AXIS, fontSize: 9, formatter: '{value}%' },
        splitLine: { show: false },
      },
    ],
    series: [
      { name: '涨停', type: 'bar', stack: 'a', data: series.map(s => s.limit_up_count), itemStyle: { color: BULL }, barMaxWidth: 18 },
      { name: '跌停', type: 'bar', stack: 'a', data: series.map(s => -s.limit_down_count), itemStyle: { color: BEAR }, barMaxWidth: 18 },
      { name: '炸板', type: 'bar', data: series.map(s => s.break_count), itemStyle: { color: WARN, opacity: 0.65 }, barMaxWidth: 10 },
      {
        name: '封板率', type: 'line', yAxisIndex: 1, smooth: true, symbol: 'circle', symbolSize: 3,
        data: series.map(s => s.seal_rate == null ? null : Number(s.seal_rate.toFixed(1))),
        lineStyle: { color: ACCENT, width: 1.6 }, itemStyle: { color: ACCENT }, connectNulls: false,
      },
    ],
  }), [series])

  return <div ref={useECharts(option, [series])} className="h-52 w-full" />
}

/** 成交额柱 + 最高连板折线(右轴) —— 量能与接力高度的共振视图 */
function AmountChart({ series }: { series: ReviewDailyPoint[] }) {
  const option = useMemo<EChartsOption>(() => ({
    grid: { left: 48, right: 40, top: 28, bottom: 28 },
    legend: {
      top: 0, right: 0, itemWidth: 8, itemHeight: 8,
      textStyle: { color: AXIS, fontSize: 10 },
      data: ['成交额', '最高连板'],
    },
    tooltip: {
      trigger: 'axis',
      formatter: (params: any) => {
        const arr = Array.isArray(params) ? params : [params]
        const i = arr[0]?.dataIndex ?? 0
        const s = series[i]
        if (!s) return ''
        return [
          s.trade_date,
          `成交额: ${fmtBigNum(s.total_amount)}`,
          `较前日: ${fmtPct1(s.amount_change_rate, 1, true)}`,
          `最高连板: ${s.max_board_count}`,
          `连板家数: ${s.connected_board_count}`,
        ].join('<br/>')
      },
    },
    xAxis: {
      type: 'category',
      data: series.map(s => shortDate(s.trade_date)),
      axisLabel: { color: AXIS, fontSize: 9, interval: Math.max(0, Math.floor(series.length / 12)) },
      axisLine: { lineStyle: { color: GRID } },
    },
    yAxis: [
      {
        type: 'value',
        axisLabel: { color: AXIS, fontSize: 9, formatter: (v: number) => fmtBigNum(v) },
        splitLine: { lineStyle: { color: GRID } },
      },
      {
        // 同上:右轴名与 legend 相撞,legend 已标注"最高连板"
        type: 'value', minInterval: 1,
        axisLabel: { color: AXIS, fontSize: 9 },
        splitLine: { show: false },
      },
    ],
    series: [
      { name: '成交额', type: 'bar', data: series.map(s => s.total_amount), itemStyle: { color: ACCENT, opacity: 0.55 }, barMaxWidth: 18 },
      {
        name: '最高连板', type: 'line', yAxisIndex: 1, smooth: true, symbol: 'circle', symbolSize: 3,
        data: series.map(s => s.max_board_count),
        lineStyle: { color: BULL, width: 1.6 }, itemStyle: { color: BULL },
      },
    ],
  }), [series])

  return <div ref={useECharts(option, [series])} className="h-52 w-full" />
}

export function EmotionCyclePanel({ asOf, days, onDaysChange }: {
  asOf?: string
  days: number
  onDaysChange: (d: number) => void
}) {
  const q = useQuery({
    queryKey: QK.reviewEmotion(asOf, days),
    queryFn: () => api.reviewEmotion(asOf, days),
    staleTime: 60_000,
    placeholderData: (prev) => prev,
  })

  const series = q.data?.series ?? []
  const last = series[series.length - 1]
  const prev = series[series.length - 2]

  if (q.isLoading && !q.data) {
    return (
      <div className="grid h-64 place-items-center rounded-card border border-border bg-surface/80">
        <RefreshCw className="h-4 w-4 animate-spin text-muted" />
      </div>
    )
  }
  if (!last) {
    return (
      <div className="rounded-card border border-border bg-surface/80">
        <EmptyState icon={Activity} title="暂无情绪周期数据" hint="需要日 K enriched 面板,请先前往「数据」页同步" />
      </div>
    )
  }

  return (
    <div className="space-y-3">
      <ReviewCard
        title="情绪周期"
        icon={<Activity className="h-3.5 w-3.5 text-accent" />}
        hint={`${series.length} 个交易日 · 截至 ${last.trade_date}`}
        right={<DaysSwitch value={days} options={[20, 30, 60]} onChange={onDaysChange} />}
      >
        {/* 最新一日读数条(带较前日 delta) */}
        <div className="grid grid-cols-3 gap-x-4 gap-y-3 border-b border-border px-3.5 py-3 sm:grid-cols-4 lg:grid-cols-7">
          <Kpi label="涨停" value={String(last.limit_up_count)} tone="text-bull" delta={deltaOf(last.limit_up_count, prev?.limit_up_count)} />
          <Kpi label="跌停" value={String(last.limit_down_count)} tone="text-bear" delta={deltaOf(last.limit_down_count, prev?.limit_down_count)} />
          <Kpi label="炸板" value={String(last.break_count)} tone="text-warning" delta={deltaOf(last.break_count, prev?.break_count)} />
          <Kpi label="封板率" value={fmtPct1(last.seal_rate, 0)} />
          <Kpi label="最高连板" value={String(last.max_board_count)} tone="text-bull" delta={deltaOf(last.max_board_count, prev?.max_board_count)} />
          <Kpi label="连板家数" value={String(last.connected_board_count)} delta={deltaOf(last.connected_board_count, prev?.connected_board_count)} />
          <Kpi label="成交额" value={fmtBigNum(last.total_amount)} delta={fmtPct1(last.amount_change_rate, 1, true)} />
        </div>

        <div className="grid grid-cols-1 gap-2 px-2 py-2 xl:grid-cols-2">
          <LimitChart series={series} />
          <AmountChart series={series} />
        </div>
      </ReviewCard>

      {/* 逐日明细 */}
      <ReviewCard title="逐日读数" icon={<Activity className="h-3.5 w-3.5 text-accent" />}>
        <div className="max-h-[24rem] overflow-y-auto">
          <table className="w-full border-collapse text-[11px]">
            <thead className="sticky top-0 z-10 bg-surface">
              <tr className="border-b border-border text-[10px] text-secondary">
                <th className="px-3 py-1.5 text-left font-normal">日期</th>
                <th className="px-2 py-1.5 text-right font-normal">成交额</th>
                <th className="px-2 py-1.5 text-right font-normal">较前日</th>
                <th className="px-2 py-1.5 text-right font-normal">涨</th>
                <th className="px-2 py-1.5 text-right font-normal">跌</th>
                <th className="px-2 py-1.5 text-right font-normal">涨停</th>
                <th className="px-2 py-1.5 text-right font-normal">跌停</th>
                <th className="px-2 py-1.5 text-right font-normal">炸板</th>
                <th className="px-2 py-1.5 text-right font-normal">封板率</th>
                <th className="px-2 py-1.5 text-right font-normal">最高板</th>
                <th className="px-2 py-1.5 text-right font-normal">跌超7%</th>
                <th className="px-3 py-1.5 text-right font-normal">平均涨幅</th>
              </tr>
            </thead>
            <tbody>
              {[...series].reverse().map((s) => (
                <tr key={s.trade_date} className="border-b border-border/40 transition-colors last:border-0 hover:bg-elevated/40">
                  <td className="px-3 py-1.5 font-mono tabular-nums text-foreground">{s.trade_date}</td>
                  <td className="px-2 py-1.5 text-right font-mono tabular-nums text-secondary">{fmtBigNum(s.total_amount)}</td>
                  <td className={cn('px-2 py-1.5 text-right font-mono tabular-nums', pctTone(s.amount_change_rate))}>
                    {fmtPct1(s.amount_change_rate, 1, true)}
                  </td>
                  <td className="px-2 py-1.5 text-right font-mono tabular-nums text-bull">{s.up_count}</td>
                  <td className="px-2 py-1.5 text-right font-mono tabular-nums text-bear">{s.down_count}</td>
                  <td className="px-2 py-1.5 text-right font-mono font-semibold tabular-nums text-bull">{s.limit_up_count}</td>
                  <td className="px-2 py-1.5 text-right font-mono tabular-nums text-bear">{s.limit_down_count}</td>
                  <td className="px-2 py-1.5 text-right font-mono tabular-nums text-warning">{s.break_count}</td>
                  <td className="px-2 py-1.5 text-right font-mono tabular-nums text-foreground">{fmtPct1(s.seal_rate, 0)}</td>
                  <td className="px-2 py-1.5 text-right font-mono tabular-nums text-foreground">{s.max_board_count}</td>
                  <td className="px-2 py-1.5 text-right font-mono tabular-nums text-secondary">{s.down_more_than_7_count}</td>
                  <td className={cn('px-3 py-1.5 text-right font-mono tabular-nums', pctTone(s.avg_change))}>
                    {fmtPct1(s.avg_change, 2, true)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </ReviewCard>
    </div>
  )
}

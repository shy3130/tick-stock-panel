/**
 * 港股市场宽度 —— 近 N 个交易日的涨跌家数 / 成交额 / 平均涨幅时序。
 *
 * 为什么没有涨停/封板率/连板:港股无涨跌停制度。这里用「涨/跌超 5%」的家数
 * 作为"显著异动"的替代读数 —— 它是港股语境下最接近 A 股涨跌停的强弱刻度。
 */
import { useMemo } from 'react'
import { useQuery } from '@tanstack/react-query'
import type { EChartsOption } from 'echarts'
import { Activity, RefreshCw } from 'lucide-react'

import { api, type HkBreadthPoint } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { cn } from '@/lib/cn'
import { fmtBigNum } from '@/lib/format'
import { EmptyState } from '@/components/EmptyState'
import { useECharts } from '@/pages/backtest/charts/useECharts'
import {
  ReviewCard, DaysSwitch, Kpi, fmtPct1, pctTone, shortDate,
  BULL, BEAR, ACCENT, AXIS, GRID,
} from './shared'

/** 涨跌家数(上下对称柱) + 平均涨幅折线(右轴) */
function BreadthChart({ series }: { series: HkBreadthPoint[] }) {
  const option = useMemo<EChartsOption>(() => ({
    grid: { left: 44, right: 44, top: 28, bottom: 28 },
    legend: {
      top: 0, right: 0, itemWidth: 8, itemHeight: 8,
      textStyle: { color: AXIS, fontSize: 10 },
      data: ['上涨', '下跌', '平均涨幅'],
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
        type: 'value', axisLabel: { color: AXIS, fontSize: 9 },
        splitLine: { lineStyle: { color: GRID } },
      },
      {
        type: 'value',
        axisLabel: { color: AXIS, fontSize: 9, formatter: '{value}%' },
        splitLine: { show: false },
      },
    ],
    series: [
      { name: '上涨', type: 'bar', stack: 'b', data: series.map(s => s.up_count), itemStyle: { color: BULL }, barMaxWidth: 18 },
      { name: '下跌', type: 'bar', stack: 'b', data: series.map(s => -s.down_count), itemStyle: { color: BEAR }, barMaxWidth: 18 },
      {
        name: '平均涨幅', type: 'line', yAxisIndex: 1, smooth: true, symbol: 'circle', symbolSize: 3,
        data: series.map(s => s.avg_change == null ? null : Number(s.avg_change.toFixed(2))),
        lineStyle: { color: ACCENT, width: 1.6 }, itemStyle: { color: ACCENT },
      },
    ],
  }), [series])

  return <div ref={useECharts(option, [series])} className="h-52 w-full" />
}

/** 成交额柱 + 涨/跌超 5% 家数折线 —— 量能与异动强度 */
function AmountChart({ series, strongPct }: { series: HkBreadthPoint[]; strongPct: number }) {
  const option = useMemo<EChartsOption>(() => ({
    grid: { left: 52, right: 44, top: 28, bottom: 28 },
    legend: {
      top: 0, right: 0, itemWidth: 8, itemHeight: 8,
      textStyle: { color: AXIS, fontSize: 10 },
      data: ['成交额', `涨超${strongPct}%`, `跌超${strongPct}%`],
    },
    tooltip: {
      trigger: 'axis',
      formatter: (params: any) => {
        const arr = Array.isArray(params) ? params : [params]
        const s = series[arr[0]?.dataIndex ?? 0]
        if (!s) return ''
        return [
          s.trade_date,
          `成交额: ${fmtBigNum(s.total_amount)}`,
          `较前日: ${fmtPct1(s.amount_change_rate, 1, true)}`,
          `涨超${strongPct}%: ${s.strong_up}`,
          `跌超${strongPct}%: ${s.strong_down}`,
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
        type: 'value', minInterval: 1,
        axisLabel: { color: AXIS, fontSize: 9 },
        splitLine: { show: false },
      },
    ],
    series: [
      { name: '成交额', type: 'bar', data: series.map(s => s.total_amount), itemStyle: { color: ACCENT, opacity: 0.5 }, barMaxWidth: 18 },
      {
        name: `涨超${strongPct}%`, type: 'line', yAxisIndex: 1, smooth: true, symbol: 'circle', symbolSize: 3,
        data: series.map(s => s.strong_up), lineStyle: { color: BULL, width: 1.5 }, itemStyle: { color: BULL },
      },
      {
        name: `跌超${strongPct}%`, type: 'line', yAxisIndex: 1, smooth: true, symbol: 'circle', symbolSize: 3,
        data: series.map(s => s.strong_down), lineStyle: { color: BEAR, width: 1.5 }, itemStyle: { color: BEAR },
      },
    ],
  }), [series, strongPct])

  return <div ref={useECharts(option, [series, strongPct])} className="h-52 w-full" />
}

export function HkBreadthPanel({ asOf, days, onDaysChange }: {
  asOf?: string
  days: number
  onDaysChange: (d: number) => void
}) {
  const q = useQuery({
    queryKey: QK.reviewHkBreadth(asOf, days),
    queryFn: () => api.reviewHkBreadth(asOf, days),
    staleTime: 60_000,
    placeholderData: (prev) => prev,
  })

  const series = q.data?.series ?? []
  const strongPct = q.data?.strong_pct ?? 5
  const last = series[series.length - 1]

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
        <EmptyState
          icon={Activity}
          title="暂无港股行情数据"
          hint="港股复盘读 fstore 的 daily_markets(asset_type=3)，请确认 fstore DuckDB 可访问"
        />
      </div>
    )
  }

  return (
    <div className="space-y-3">
      <ReviewCard
        title="港股市场宽度"
        icon={<Activity className="h-3.5 w-3.5 text-accent" />}
        hint={`${series.length} 个交易日 · 截至 ${last.trade_date}`}
        right={<DaysSwitch value={days} options={[20, 30, 60]} onChange={onDaysChange} />}
      >
        <div className="grid grid-cols-3 gap-x-4 gap-y-3 border-b border-border px-3.5 py-3 sm:grid-cols-4 lg:grid-cols-7">
          <Kpi label="上涨" value={String(last.up_count)} tone="text-bull" />
          <Kpi label="下跌" value={String(last.down_count)} tone="text-bear" />
          <Kpi label="平盘" value={String(last.flat_count)} />
          <Kpi label="上涨占比" value={fmtPct1(last.up_pct, 0)} />
          <Kpi label={`涨超${strongPct}%`} value={String(last.strong_up)} tone="text-bull" />
          <Kpi label={`跌超${strongPct}%`} value={String(last.strong_down)} tone="text-bear" />
          <Kpi label="成交额" value={fmtBigNum(last.total_amount)} delta={fmtPct1(last.amount_change_rate, 1, true)} />
        </div>

        <div className="grid grid-cols-1 gap-2 px-2 py-2 xl:grid-cols-2">
          <BreadthChart series={series} />
          <AmountChart series={series} strongPct={strongPct} />
        </div>

        <div className="border-t border-border px-3.5 py-2">
          <p className="text-[10px] leading-relaxed text-muted">
            港股无涨跌停制度，故不设涨停/封板率/连板梯队；以「涨跌超 {strongPct}%」的家数作为异动强度读数。
          </p>
        </div>
      </ReviewCard>

      <ReviewCard title="逐日读数" icon={<Activity className="h-3.5 w-3.5 text-accent" />}>
        <div className="max-h-[24rem] overflow-y-auto">
          <table className="w-full border-collapse text-[11px]">
            <thead className="sticky top-0 z-10 bg-surface">
              <tr className="border-b border-border text-[10px] text-secondary">
                <th className="px-3 py-1.5 text-left font-normal">日期</th>
                <th className="px-2 py-1.5 text-right font-normal">成交额</th>
                <th className="px-2 py-1.5 text-right font-normal">较前日</th>
                <th className="px-2 py-1.5 text-right font-normal">总数</th>
                <th className="px-2 py-1.5 text-right font-normal">涨</th>
                <th className="px-2 py-1.5 text-right font-normal">跌</th>
                <th className="px-2 py-1.5 text-right font-normal">涨占比</th>
                <th className="px-2 py-1.5 text-right font-normal">涨超{strongPct}%</th>
                <th className="px-2 py-1.5 text-right font-normal">跌超{strongPct}%</th>
                <th className="px-2 py-1.5 text-right font-normal">中位涨幅</th>
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
                  <td className="px-2 py-1.5 text-right font-mono tabular-nums text-muted">{s.total}</td>
                  <td className="px-2 py-1.5 text-right font-mono font-semibold tabular-nums text-bull">{s.up_count}</td>
                  <td className="px-2 py-1.5 text-right font-mono font-semibold tabular-nums text-bear">{s.down_count}</td>
                  <td className="px-2 py-1.5 text-right font-mono tabular-nums text-foreground">{fmtPct1(s.up_pct, 0)}</td>
                  <td className="px-2 py-1.5 text-right font-mono tabular-nums text-bull">{s.strong_up}</td>
                  <td className="px-2 py-1.5 text-right font-mono tabular-nums text-bear">{s.strong_down}</td>
                  <td className={cn('px-2 py-1.5 text-right font-mono tabular-nums', pctTone(s.median_change))}>
                    {fmtPct1(s.median_change, 2, true)}
                  </td>
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

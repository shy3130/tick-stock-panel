/**
 * 连板天梯 —— 近 N 日板层分布 + 晋级率序列。
 *
 * 口径提醒:晋级率由**板层分布跨日派生**(今日 N+1 板家数 / 昨日 N 板家数),
 * 不做个股连板配对。它度量的是"梯队整体晋级强度",而非"某只票是否晋级"。
 * 单日的个股级梯队详情见「连板梯队」页(/limit-ladder)。
 */
import { useMemo } from 'react'
import { useQuery } from '@tanstack/react-query'
import type { EChartsOption } from 'echarts'
import { Layers, RefreshCw } from 'lucide-react'
import { Link } from 'react-router-dom'

import { api, type ReviewDailyPoint } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { cn } from '@/lib/cn'
import { EmptyState } from '@/components/EmptyState'
import { useECharts } from '@/pages/backtest/charts/useECharts'
import {
  ReviewCard, DaysSwitch, Kpi, fmtPct1, shortDate,
  BULL, ACCENT, WARN, AXIS, GRID,
} from './shared'

// 板层由低到高:颜色越高越红(接力高度越高越热)
const TIERS: { key: keyof ReviewDailyPoint; label: string; color: string }[] = [
  { key: 'board_1', label: '首板', color: 'rgba(240,68,56,0.28)' },
  { key: 'board_2', label: '2板', color: 'rgba(240,68,56,0.45)' },
  { key: 'board_3', label: '3板', color: 'rgba(240,68,56,0.62)' },
  { key: 'board_4', label: '4板', color: 'rgba(240,68,56,0.79)' },
  { key: 'board_5', label: '5板', color: BULL },
  { key: 'high_board', label: '高标', color: '#7C2D12' },
]

/** 板层堆叠柱 + 晋级率折线(右轴) */
function LadderChart({ series }: { series: ReviewDailyPoint[] }) {
  const option = useMemo<EChartsOption>(() => ({
    grid: { left: 40, right: 44, top: 28, bottom: 28 },
    legend: {
      top: 0, right: 0, itemWidth: 8, itemHeight: 8,
      textStyle: { color: AXIS, fontSize: 10 },
      data: [...TIERS.map(t => t.label), '总晋级率', '1进2'],
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
        type: 'value', name: '家数', minInterval: 1,
        nameTextStyle: { color: AXIS, fontSize: 9 },
        axisLabel: { color: AXIS, fontSize: 9 },
        splitLine: { lineStyle: { color: GRID } },
      },
      {
        // 不设 name:右轴名会渲染在右上角、与 legend 相撞;legend 已标注晋级率序列
        type: 'value', min: 0,
        axisLabel: { color: AXIS, fontSize: 9, formatter: '{value}%' },
        splitLine: { show: false },
      },
    ],
    series: [
      ...TIERS.map(t => ({
        name: t.label,
        type: 'bar' as const,
        stack: 'boards',
        data: series.map(s => Number(s[t.key] ?? 0)),
        itemStyle: { color: t.color },
        barMaxWidth: 20,
      })),
      {
        name: '总晋级率', type: 'line', yAxisIndex: 1, smooth: true, symbol: 'circle', symbolSize: 3,
        data: series.map(s => s.promotion_rate == null ? null : Number(s.promotion_rate.toFixed(1))),
        lineStyle: { color: ACCENT, width: 1.6 }, itemStyle: { color: ACCENT }, connectNulls: false,
      },
      {
        name: '1进2', type: 'line', yAxisIndex: 1, smooth: true, symbol: 'circle', symbolSize: 3,
        data: series.map(s => s.first_to_second_rate == null ? null : Number(s.first_to_second_rate.toFixed(1))),
        lineStyle: { color: WARN, width: 1.4, type: 'dashed' }, itemStyle: { color: WARN }, connectNulls: false,
      },
    ],
  }), [series])

  return <div ref={useECharts(option, [series])} className="h-56 w-full" />
}

const RATE_COLS: { key: keyof ReviewDailyPoint; label: string }[] = [
  { key: 'promotion_rate', label: '总晋级' },
  { key: 'first_to_second_rate', label: '1进2' },
  { key: 'second_to_third_rate', label: '2进3' },
  { key: 'third_to_fourth_rate', label: '3进4' },
  { key: 'fourth_to_fifth_rate', label: '4进5' },
  { key: 'fifth_to_high_rate', label: '5进高' },
]

// 晋级率染色:>50% 强(红) / 30~50 中 / <30 弱
function rateTone(v: number | null | undefined): string {
  if (v == null) return 'text-muted'
  if (v >= 50) return 'text-bull font-semibold'
  if (v >= 30) return 'text-warning'
  return 'text-secondary'
}

export function LadderPromotionPanel({ asOf, days, onDaysChange }: {
  asOf?: string
  days: number
  onDaysChange: (d: number) => void
}) {
  const q = useQuery({
    queryKey: QK.reviewLadder(asOf, days),
    queryFn: () => api.reviewLadder(asOf, days),
    staleTime: 60_000,
    placeholderData: (prev) => prev,
  })

  const series = q.data?.series ?? []
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
        <EmptyState icon={Layers} title="暂无连板天梯数据" hint="需要日 K enriched 面板,请先前往「数据」页同步" />
      </div>
    )
  }

  return (
    <div className="space-y-3">
      <ReviewCard
        title="连板天梯"
        icon={<Layers className="h-3.5 w-3.5 text-accent" />}
        hint={`${series.length} 个交易日 · 截至 ${last.trade_date}`}
        right={<DaysSwitch value={days} options={[10, 20, 30]} onChange={onDaysChange} />}
      >
        <div className="grid grid-cols-3 gap-x-4 gap-y-3 border-b border-border px-3.5 py-3 sm:grid-cols-4 lg:grid-cols-8">
          {TIERS.map(t => (
            <Kpi key={t.label} label={t.label} value={String(last[t.key] ?? 0)} tone="text-foreground" />
          ))}
          <Kpi label="总晋级率" value={fmtPct1(last.promotion_rate, 0)} tone={rateTone(last.promotion_rate)} />
          <Kpi label="1进2" value={fmtPct1(last.first_to_second_rate, 0)} tone={rateTone(last.first_to_second_rate)} />
        </div>

        <div className="px-2 py-2">
          <LadderChart series={series} />
        </div>

        <div className="border-t border-border px-3.5 py-2">
          <p className="text-[10px] leading-relaxed text-muted">
            晋级率 = 今日 N+1 板家数 / 昨日 N 板家数,度量梯队整体接力强度(非个股配对)。
            单日个股级梯队详情见 <Link to="/limit-ladder" className="text-accent hover:underline">连板梯队页 →</Link>
          </p>
        </div>
      </ReviewCard>

      <ReviewCard title="逐日晋级率" icon={<Layers className="h-3.5 w-3.5 text-accent" />}>
        <div className="max-h-[24rem] overflow-y-auto">
          <table className="w-full border-collapse text-[11px]">
            <thead className="sticky top-0 z-10 bg-surface">
              <tr className="border-b border-border text-[10px] text-secondary">
                <th className="px-3 py-1.5 text-left font-normal">日期</th>
                {TIERS.map(t => <th key={t.label} className="px-2 py-1.5 text-right font-normal">{t.label}</th>)}
                <th className="px-2 py-1.5 text-right font-normal">最高板</th>
                {RATE_COLS.map(c => <th key={c.label} className="px-2 py-1.5 text-right font-normal">{c.label}</th>)}
              </tr>
            </thead>
            <tbody>
              {[...series].reverse().map((s) => (
                <tr key={s.trade_date} className="border-b border-border/40 transition-colors last:border-0 hover:bg-elevated/40">
                  <td className="px-3 py-1.5 font-mono tabular-nums text-foreground">{s.trade_date}</td>
                  {TIERS.map(t => (
                    <td key={t.label} className="px-2 py-1.5 text-right font-mono tabular-nums text-foreground">
                      {Number(s[t.key] ?? 0) || <span className="text-muted">—</span>}
                    </td>
                  ))}
                  <td className="px-2 py-1.5 text-right font-mono font-semibold tabular-nums text-bull">{s.max_board_count}</td>
                  {RATE_COLS.map(c => (
                    <td key={c.label} className={cn('px-2 py-1.5 text-right font-mono tabular-nums', rateTone(s[c.key] as number | null))}>
                      {fmtPct1(s[c.key] as number | null, 0)}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </ReviewCard>
    </div>
  )
}

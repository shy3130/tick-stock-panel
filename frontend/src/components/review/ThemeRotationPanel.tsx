/**
 * 题材轮动 —— 近 N 日 × Top 题材的涨停矩阵,横向读主线切换。
 *
 * 每日只统计涨停股,按 ext_data 概念映射归集。矩阵行(题材)按窗口内涨停总数取 Top,
 * 保证行稳定可比 —— 若每列各取各的 Top,列与列之间就没法横着读了。
 * 未配置概念扩展数据时给引导态,而不是空白。
 */
import { useMemo } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { Shuffle, RefreshCw, Database, ChevronRight } from 'lucide-react'

import { api, type ReviewRotationCell } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { cn } from '@/lib/cn'
import { fmtBigNum } from '@/lib/format'
import { EmptyState } from '@/components/EmptyState'
import { ReviewCard, DaysSwitch, fmtPct1, shortDate } from './shared'

/** 涨停数 → 热度背景(相对窗口内最大值归一,避免绝对阈值在冷市下全白) */
function heatStyle(count: number, max: number): React.CSSProperties {
  if (count <= 0) return {}
  const ratio = max > 0 ? count / max : 0
  // 0.08 ~ 0.55 的红色透明度区间:冷格淡到几乎看不见,热格明确
  const alpha = 0.08 + ratio * 0.47
  return { backgroundColor: `rgba(240, 68, 56, ${alpha.toFixed(3)})` }
}

function cellTitle(cell: ReviewRotationCell): string {
  const leaders = cell.leaders.map(l => `${l.name ?? l.symbol}${l.boards > 1 ? `(${l.boards}板)` : ''}`).join(' / ')
  return [
    `${cell.trade_date} · ${cell.name}`,
    `涨停 ${cell.limit_up_count} 家 · 最高 ${cell.max_board_count} 板`,
    `成交额 ${fmtBigNum(cell.amount)} · 平均涨幅 ${fmtPct1(cell.avg_change, 2, true)}`,
    leaders ? `龙头: ${leaders}` : '',
  ].filter(Boolean).join('\n')
}

export function ThemeRotationPanel({ asOf, days, onDaysChange }: {
  asOf?: string
  days: number
  onDaysChange: (d: number) => void
}) {
  const TOP = 8
  const q = useQuery({
    queryKey: QK.reviewRotation(asOf, days, TOP),
    queryFn: () => api.reviewRotation(asOf, days, TOP),
    staleTime: 60_000,
    placeholderData: (prev) => prev,
  })

  const data = q.data
  // (题材, 日期) → 单元格,供矩阵 O(1) 取数
  const { grid, max } = useMemo(() => {
    const grid = new Map<string, ReviewRotationCell>()
    let max = 0
    for (const c of data?.cells ?? []) {
      grid.set(`${c.name}|${c.trade_date}`, c)
      if (c.limit_up_count > max) max = c.limit_up_count
    }
    return { grid, max }
  }, [data])

  if (q.isLoading && !data) {
    return (
      <div className="grid h-64 place-items-center rounded-card border border-border bg-surface/80">
        <RefreshCw className="h-4 w-4 animate-spin text-muted" />
      </div>
    )
  }

  // 未配置概念扩展数据 —— 给引导,而非空白
  if (data && !data.available && data.reason === 'no_concept_ext') {
    return (
      <div className="flex flex-col items-center justify-center gap-4 rounded-card border border-border bg-surface/80 px-6 py-16">
        <div className="grid h-14 w-14 place-items-center rounded-2xl border border-accent/30 bg-gradient-to-br from-accent/20 to-purple-500/15">
          <Database className="h-6 w-6 text-accent" strokeWidth={1.8} />
        </div>
        <div className="text-center">
          <div className="text-sm font-medium text-foreground">未配置概念数据</div>
          <p className="mt-1 max-w-sm text-xs leading-relaxed text-muted">
            题材轮动需要「概念成分」扩展数据来把涨停股归集到题材。
            前往数据页拉取同花顺概念预设后即可使用。
          </p>
        </div>
        <Link
          to="/concept-analysis"
          className="inline-flex items-center gap-1.5 rounded-btn bg-accent px-4 py-2 text-xs font-medium text-white shadow-sm transition-all hover:bg-accent/90 hover:shadow"
        >
          <Database className="h-3.5 w-3.5" />前往拉取概念数据
          <ChevronRight className="h-3.5 w-3.5" />
        </Link>
      </div>
    )
  }

  const themes = data?.themes ?? []
  const dates = data?.trade_dates ?? []

  if (themes.length === 0 || dates.length === 0) {
    return (
      <div className="rounded-card border border-border bg-surface/80">
        <EmptyState icon={Shuffle} title="窗口内无涨停题材" hint="所选区间没有涨停股,或涨停股未命中任何概念。可以把窗口拉长再看。" />
      </div>
    )
  }

  return (
    <ReviewCard
      title="题材轮动"
      icon={<Shuffle className="h-3.5 w-3.5 text-accent" />}
      hint={`Top ${themes.length} 题材 × ${dates.length} 日 · 格内为涨停家数`}
      right={<DaysSwitch value={days} options={[5, 10, 20]} onChange={onDaysChange} />}
    >
      <div className="overflow-x-auto px-3.5 py-3">
        <table className="w-full min-w-[42rem] border-collapse text-[11px]">
          <thead>
            <tr>
              <th className="sticky left-0 z-10 bg-surface px-2 py-1.5 text-left text-[10px] font-normal text-secondary">题材</th>
              {dates.map(d => (
                <th key={d} className="px-1 py-1.5 text-center font-mono text-[10px] font-normal tabular-nums text-secondary">
                  {shortDate(d)}
                </th>
              ))}
              <th className="px-2 py-1.5 text-right text-[10px] font-normal text-secondary">合计</th>
            </tr>
          </thead>
          <tbody>
            {themes.map(theme => {
              const total = dates.reduce((sum, d) => sum + (grid.get(`${theme}|${d}`)?.limit_up_count ?? 0), 0)
              return (
                <tr key={theme}>
                  <td className="sticky left-0 z-10 max-w-[9rem] truncate bg-surface px-2 py-1 text-foreground" title={theme}>
                    {theme}
                  </td>
                  {dates.map(d => {
                    const cell = grid.get(`${theme}|${d}`)
                    const count = cell?.limit_up_count ?? 0
                    return (
                      <td key={d} className="px-0.5 py-0.5">
                        <div
                          className={cn(
                            'grid h-7 place-items-center rounded font-mono text-[11px] tabular-nums transition-colors',
                            count > 0 ? 'text-foreground' : 'text-muted/30',
                            cell && 'cursor-default',
                          )}
                          style={heatStyle(count, max)}
                          title={cell ? cellTitle(cell) : undefined}
                        >
                          {count > 0 ? (
                            <span className="flex items-baseline gap-0.5">
                              {count}
                              {/* 有连板才标高度,首板不标,避免满屏噪声 */}
                              {cell && cell.max_board_count > 1 && (
                                <span className="text-[8px] text-bull">{cell.max_board_count}板</span>
                              )}
                            </span>
                          ) : '·'}
                        </div>
                      </td>
                    )
                  })}
                  <td className="px-2 py-1 text-right font-mono font-semibold tabular-nums text-foreground">{total}</td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
      <div className="border-t border-border px-3.5 py-2">
        <p className="text-[10px] leading-relaxed text-muted">
          行按窗口内涨停总数取 Top {themes.length}(保证跨列可比);色深随当日涨停家数递增。悬停格子看最高板与龙头。
        </p>
      </div>
    </ReviewCard>
  )
}

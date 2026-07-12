/**
 * 复盘分区共用的展示基元。
 *
 * 单位约定(与后端 services/review_series 对齐):所有 *_rate / *_pct / avg_change
 * 传进来时**已经是百分数**(5.0 = 5%),这里只 toFixed,绝不再乘 100。
 */
import type { ReactNode } from 'react'
import { Link } from 'react-router-dom'
import { cn } from '@/lib/cn'
import { fmtBigNum } from '@/lib/format'
import type { ReviewClueStock } from '@/lib/api'

// A 股语义色:涨红跌绿(与 tailwind bull/bear token 同源)
export const BULL = '#F04438'
export const BEAR = '#12B76A'
export const ACCENT = '#3B82F6'
export const WARN = '#F79009'
// 图表轴/网格 —— 取中性灰,明暗主题下都可读
export const AXIS = '#a1a1aa'
export const GRID = 'rgba(160,160,170,0.18)'

export function fmtPct1(v: number | null | undefined, digits = 1, withSign = false): string {
  if (v == null || Number.isNaN(v)) return '—'
  const sign = withSign && v > 0 ? '+' : ''
  return `${sign}${v.toFixed(digits)}%`
}

export function pctTone(v: number | null | undefined): string {
  if (v == null || Number.isNaN(v) || v === 0) return 'text-muted'
  return v > 0 ? 'text-bull' : 'text-bear'
}

/** 日期短写:2026-07-09 → 07-09 */
export function shortDate(d: string): string {
  return d.length >= 10 ? d.slice(5) : d
}

/** 分区卡片 —— 与 Review 页现有 rounded-card / bg-surface/80 风格一致 */
export function ReviewCard({
  title, icon, hint, right, children, className,
}: {
  title: string
  icon?: ReactNode
  hint?: string
  right?: ReactNode
  children: ReactNode
  className?: string
}) {
  return (
    <div className={cn('overflow-hidden rounded-card border border-border bg-surface/80', className)}>
      <div className="flex items-center justify-between gap-2 border-b border-border bg-gradient-to-r from-accent/5 to-transparent px-3.5 py-2.5">
        <div className="flex min-w-0 items-center gap-1.5">
          {icon}
          <span className="truncate text-xs font-medium text-foreground">{title}</span>
          {hint && <span className="truncate text-[10px] text-muted">{hint}</span>}
        </div>
        {right}
      </div>
      {children}
    </div>
  )
}

/** 天数切换段控件 */
export function DaysSwitch({
  value, options, onChange,
}: {
  value: number
  options: number[]
  onChange: (v: number) => void
}) {
  return (
    <div className="flex shrink-0 items-center gap-0.5 rounded-btn bg-elevated/60 p-0.5">
      {options.map((d) => (
        <button
          key={d}
          onClick={() => onChange(d)}
          className={cn(
            'rounded px-2 py-0.5 font-mono text-[10px] tabular-nums transition-colors',
            value === d ? 'bg-accent text-white' : 'text-secondary hover:text-foreground',
          )}
        >
          {d}日
        </button>
      ))}
    </div>
  )
}

/** KPI 小格 —— 顶部读数条 */
export function Kpi({
  label, value, tone, delta,
}: {
  label: string
  value: string
  tone?: string
  delta?: string
}) {
  return (
    <div className="flex flex-col gap-0.5">
      <span className="text-[10px] text-secondary">{label}</span>
      <div className="flex items-baseline gap-1">
        <span className={cn('font-mono text-sm font-semibold tabular-nums', tone ?? 'text-foreground')}>{value}</span>
        {delta && <span className="font-mono text-[10px] tabular-nums text-muted">{delta}</span>}
      </div>
    </div>
  )
}

/**
 * 线索股票表 —— 复盘 5 张清单共用。
 * `extra` 用于每张表的专有列(冲高回落的回落幅度、反包的昨日涨跌)。
 */
export function ClueTable({
  rows, empty, extra,
}: {
  rows: ReviewClueStock[]
  empty: string
  extra?: { label: string; render: (s: ReviewClueStock) => ReactNode }
}) {
  if (rows.length === 0) {
    return <div className="px-3.5 py-8 text-center text-[11px] text-muted">{empty}</div>
  }
  return (
    <div className="max-h-[22rem] overflow-y-auto">
      <table className="w-full border-collapse text-[11px]">
        <thead className="sticky top-0 z-10 bg-surface">
          <tr className="border-b border-border text-[10px] text-secondary">
            <th className="px-3 py-1.5 text-left font-normal">名称</th>
            <th className="px-2 py-1.5 text-right font-normal">现价</th>
            <th className="px-2 py-1.5 text-right font-normal">涨跌</th>
            {extra && <th className="px-2 py-1.5 text-right font-normal">{extra.label}</th>}
            <th className="px-2 py-1.5 text-right font-normal">成交额</th>
            <th className="px-2 py-1.5 text-right font-normal">换手</th>
            <th className="px-3 py-1.5 text-left font-normal">行业 / 概念</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((s) => (
            <tr key={s.symbol} className="border-b border-border/40 transition-colors last:border-0 hover:bg-elevated/40">
              <td className="px-3 py-1.5">
                <Link
                  to={`/stock-analysis?symbol=${encodeURIComponent(s.symbol)}`}
                  className="flex items-center gap-1.5 transition-colors hover:text-accent"
                >
                  <span className="truncate font-medium text-foreground">{s.name ?? '—'}</span>
                  <span className="font-mono text-[10px] text-muted">{s.symbol.split('.')[0]}</span>
                  {s.boards > 1 && (
                    <span className="rounded bg-bull/15 px-1 font-mono text-[9px] text-bull">{s.boards}板</span>
                  )}
                </Link>
              </td>
              <td className="px-2 py-1.5 text-right font-mono tabular-nums text-foreground">
                {s.close?.toFixed(2) ?? '—'}
              </td>
              <td className={cn('px-2 py-1.5 text-right font-mono font-semibold tabular-nums', pctTone(s.change_pct))}>
                {fmtPct1(s.change_pct, 2, true)}
              </td>
              {extra && <td className="px-2 py-1.5 text-right font-mono tabular-nums">{extra.render(s)}</td>}
              <td className="px-2 py-1.5 text-right font-mono tabular-nums text-secondary">{fmtBigNum(s.amount)}</td>
              <td className="px-2 py-1.5 text-right font-mono tabular-nums text-secondary">
                {s.turnover_rate != null ? `${s.turnover_rate.toFixed(1)}%` : '—'}
              </td>
              <td className="max-w-[14rem] px-3 py-1.5">
                <div className="flex items-center gap-1 truncate">
                  {s.industry && (
                    <span className="shrink-0 rounded bg-accent/10 px-1 text-[9px] text-accent">{s.industry}</span>
                  )}
                  <span className="truncate text-[10px] text-muted">{s.concepts.join(' · ')}</span>
                </div>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

/**
 * 港股涨跌榜 —— 单日涨幅榜 / 跌幅榜 / 成交额榜 + 板块分布 + 涨跌幅分布。
 *
 * 没有换手率列:fstore 里港股的 hslv 全是 NULL(实测),不是忘了写。
 * 也没有冲高回落:港股行没有高/低/开盘价。
 */
import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { TrendingUp, TrendingDown, Coins, RefreshCw, BarChart3 } from 'lucide-react'
import type { LucideIcon } from 'lucide-react'

import { api, type HkMovers, type HkMoverStock } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { cn } from '@/lib/cn'
import { fmtBigNum } from '@/lib/format'
import { EmptyState } from '@/components/EmptyState'
import { ReviewCard, fmtPct1, pctTone, BULL, BEAR } from './shared'

type MoverKey = keyof Pick<HkMovers, 'top_gainers' | 'top_losers' | 'top_amount'>

const LISTS: { key: MoverKey; label: string; icon: LucideIcon }[] = [
  { key: 'top_gainers', label: '涨幅榜', icon: TrendingUp },
  { key: 'top_losers', label: '跌幅榜', icon: TrendingDown },
  { key: 'top_amount', label: '成交额榜', icon: Coins },
]

function MoverTable({ rows }: { rows: HkMoverStock[] }) {
  if (rows.length === 0) {
    return <div className="px-3.5 py-8 text-center text-[11px] text-muted">当日无数据</div>
  }
  return (
    <div className="max-h-[22rem] overflow-y-auto">
      <table className="w-full border-collapse text-[11px]">
        <thead className="sticky top-0 z-10 bg-surface">
          <tr className="border-b border-border text-[10px] text-secondary">
            <th className="px-3 py-1.5 text-left font-normal">名称</th>
            <th className="px-2 py-1.5 text-right font-normal">现价</th>
            <th className="px-2 py-1.5 text-right font-normal">涨跌</th>
            <th className="px-2 py-1.5 text-right font-normal">成交额</th>
            <th className="px-3 py-1.5 text-left font-normal">板块</th>
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
                  <span className="truncate font-medium text-foreground">{s.name ?? s.symbol}</span>
                  <span className="font-mono text-[10px] text-muted">{s.symbol.split('.')[0]}</span>
                </Link>
              </td>
              <td className="px-2 py-1.5 text-right font-mono tabular-nums text-foreground">{s.close?.toFixed(2) ?? '—'}</td>
              <td className={cn('px-2 py-1.5 text-right font-mono font-semibold tabular-nums', pctTone(s.change_pct))}>
                {fmtPct1(s.change_pct, 2, true)}
              </td>
              <td className="px-2 py-1.5 text-right font-mono tabular-nums text-secondary">{fmtBigNum(s.amount)}</td>
              <td className="px-3 py-1.5">
                {s.board && <span className="rounded bg-accent/10 px-1 text-[9px] text-accent">{s.board}</span>}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

export function HkMoversPanel({ asOf }: { asOf?: string }) {
  const [active, setActive] = useState<MoverKey>('top_gainers')
  const LIMIT = 30

  const q = useQuery({
    queryKey: QK.reviewHkMovers(asOf, LIMIT),
    queryFn: () => api.reviewHkMovers(asOf, LIMIT),
    staleTime: 60_000,
    placeholderData: (prev) => prev,
  })

  const data = q.data

  if (q.isLoading && !data) {
    return (
      <div className="grid h-64 place-items-center rounded-card border border-border bg-surface/80">
        <RefreshCw className="h-4 w-4 animate-spin text-muted" />
      </div>
    )
  }
  if (!data?.trade_date) {
    return (
      <div className="rounded-card border border-border bg-surface/80">
        <EmptyState icon={BarChart3} title="暂无港股行情数据" hint="港股复盘读 fstore 的 daily_markets(asset_type=3)" />
      </div>
    )
  }

  const maxBand = Math.max(...data.distribution.map(d => d.count), 1)

  return (
    <div className="space-y-3">
      {/* 板块分布 + 涨跌幅分布 */}
      <div className="grid grid-cols-1 gap-3 lg:grid-cols-2">
        <ReviewCard title="板块分布" icon={<BarChart3 className="h-3.5 w-3.5 text-accent" />} hint={data.trade_date}>
          <div className="divide-y divide-border/40">
            {data.boards.map(b => (
              <div key={b.board} className="flex items-center gap-3 px-3.5 py-2.5">
                <span className="w-14 shrink-0 text-[11px] text-foreground">{b.board}</span>
                <span className="w-12 shrink-0 text-right font-mono text-[10px] tabular-nums text-muted">{b.count} 只</span>
                {/* 涨跌家数条 */}
                <div className="flex h-2 flex-1 overflow-hidden rounded-full bg-elevated">
                  <div className="h-full" style={{ width: `${(b.up / (b.count || 1)) * 100}%`, backgroundColor: BULL }} />
                  <div className="h-full" style={{ width: `${(b.down / (b.count || 1)) * 100}%`, backgroundColor: BEAR }} />
                </div>
                <span className="w-20 shrink-0 text-right font-mono text-[10px] tabular-nums">
                  <span className="text-bull">{b.up}</span>
                  <span className="text-muted"> / </span>
                  <span className="text-bear">{b.down}</span>
                </span>
                <span className={cn('w-14 shrink-0 text-right font-mono text-[11px] font-semibold tabular-nums', pctTone(b.avg_change))}>
                  {fmtPct1(b.avg_change, 2, true)}
                </span>
                <span className="w-16 shrink-0 text-right font-mono text-[10px] tabular-nums text-secondary">
                  {fmtBigNum(b.amount)}
                </span>
              </div>
            ))}
          </div>
        </ReviewCard>

        <ReviewCard title="涨跌幅分布" icon={<BarChart3 className="h-3.5 w-3.5 text-accent" />} hint="港股无涨跌停，分桶放宽至 ±7%">
          <div className="space-y-1 px-3.5 py-3">
            {data.distribution.map(d => {
              const negative = d.label.startsWith('<') || d.label.startsWith('-')
              return (
                <div key={d.label} className="flex items-center gap-2">
                  <span className="w-14 shrink-0 text-right font-mono text-[10px] tabular-nums text-secondary">{d.label}</span>
                  <div className="h-3 flex-1 overflow-hidden rounded bg-elevated/60">
                    <div
                      className="h-full rounded transition-all"
                      style={{
                        width: `${(d.count / maxBand) * 100}%`,
                        backgroundColor: negative ? BEAR : BULL,
                        opacity: 0.75,
                      }}
                    />
                  </div>
                  <span className="w-10 shrink-0 text-right font-mono text-[10px] tabular-nums text-foreground">{d.count}</span>
                  <span className="w-10 shrink-0 text-right font-mono text-[10px] tabular-nums text-muted">
                    {d.pct.toFixed(0)}%
                  </span>
                </div>
              )
            })}
          </div>
        </ReviewCard>
      </div>

      {/* 榜单 */}
      <ReviewCard
        title="港股涨跌榜"
        icon={<TrendingUp className="h-3.5 w-3.5 text-accent" />}
        hint={data.trade_date ?? undefined}
      >
        <div className="flex flex-wrap items-center gap-1 border-b border-border px-2.5 py-2">
          {LISTS.map(l => {
            const on = active === l.key
            const Icon = l.icon
            return (
              <button
                key={l.key}
                onClick={() => setActive(l.key)}
                className={cn(
                  'inline-flex items-center gap-1.5 rounded-btn border px-2.5 py-1 text-[11px] transition-colors',
                  on
                    ? 'border-accent/40 bg-accent/10 text-accent'
                    : 'border-transparent text-secondary hover:bg-elevated/60 hover:text-foreground',
                )}
              >
                <Icon className="h-3 w-3" />
                {l.label}
              </button>
            )
          })}
        </div>
        <MoverTable rows={data[active] ?? []} />
      </ReviewCard>
    </div>
  )
}

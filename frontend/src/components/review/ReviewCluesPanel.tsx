/**
 * 风险与线索 —— 单日五张清单:炸板池 / 跌停池 / 冲高回落 / 成交额榜 / 反包股。
 *
 * 口径(后端 services/review_series 定义,这里只展示):
 *   冲高回落 = 盘中最高较昨收 ≥ +5% 且收盘涨幅 ≤ +2%,按回落幅度((收-高)/高)排序
 *   反包股   = 昨日跌幅 ≥ 3%、今日涨幅 ≥ 5%,且今收 > 昨开(实体吞没)
 */
import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { AlertTriangle, RefreshCw, Flame, TrendingDown, Coins, Undo2 } from 'lucide-react'
import type { LucideIcon } from 'lucide-react'

import { api, type ReviewClues } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { cn } from '@/lib/cn'
import { EmptyState } from '@/components/EmptyState'
import { ReviewCard, ClueTable, fmtPct1, pctTone } from './shared'

type ClueKey = keyof Pick<ReviewClues, 'broken' | 'limit_down' | 'surge_and_fade' | 'top_amount' | 'rebound'>

const LISTS: {
  key: ClueKey
  label: string
  icon: LucideIcon
  empty: string
  /** 该清单的专有列 */
  extra?: { label: string; render: (s: any) => React.ReactNode }
}[] = [
  {
    key: 'broken', label: '炸板池', icon: Flame,
    empty: '当日无炸板股 —— 要么没人摸板,要么封得住',
  },
  {
    key: 'limit_down', label: '跌停池', icon: TrendingDown,
    empty: '当日无跌停股',
  },
  {
    key: 'surge_and_fade', label: '冲高回落', icon: AlertTriangle,
    empty: '当日无显著冲高回落',
    extra: {
      label: '回落',
      render: (s) => (
        <span className="text-bear">{fmtPct1(s.fade_pct, 1)}</span>
      ),
    },
  },
  {
    key: 'top_amount', label: '成交额榜', icon: Coins,
    empty: '当日无成交数据',
  },
  {
    key: 'rebound', label: '反包股', icon: Undo2,
    empty: '当日无反包股',
    extra: {
      label: '昨日',
      render: (s) => (
        <span className={cn(pctTone(s.prev_change_pct))}>{fmtPct1(s.prev_change_pct, 2, true)}</span>
      ),
    },
  },
]

export function ReviewCluesPanel({ asOf }: { asOf?: string }) {
  const [active, setActive] = useState<ClueKey>('broken')
  const LIMIT = 30

  const q = useQuery({
    queryKey: QK.reviewClues(asOf, LIMIT),
    queryFn: () => api.reviewClues(asOf, LIMIT),
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
        <EmptyState icon={AlertTriangle} title="暂无线索数据" hint="需要日 K enriched 面板,请先前往「数据」页同步" />
      </div>
    )
  }

  const current = LISTS.find(l => l.key === active)!
  const rows = data[active] ?? []

  return (
    <ReviewCard
      title="风险与线索"
      icon={<AlertTriangle className="h-3.5 w-3.5 text-accent" />}
      hint={`${data.trade_date}${data.prev_date ? ` · 对比 ${data.prev_date}` : ''}`}
    >
      {/* 清单切换 —— 带条数徽标,一眼看出哪张表有货 */}
      <div className="flex flex-wrap items-center gap-1 border-b border-border px-2.5 py-2">
        {LISTS.map(l => {
          const count = (data[l.key] ?? []).length
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
              <span className={cn('font-mono text-[10px] tabular-nums', on ? 'text-accent' : 'text-muted')}>
                {count}
              </span>
            </button>
          )
        })}
      </div>

      <ClueTable rows={rows} empty={current.empty} extra={current.extra} />
    </ReviewCard>
  )
}

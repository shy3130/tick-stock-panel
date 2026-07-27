import { useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  AlertTriangle,
  BookOpen,
  BookOpenCheck,
  FilePenLine,
  RefreshCw,
  Trash2,
} from 'lucide-react'

import { ConfirmDialog } from '@/components/ConfirmDialog'
import { toast } from '@/components/Toast'
import { cn } from '@/lib/cn'
import type { Portfolio } from '../portfolio/api'
import {
  TRADE_REVIEWS_QUERY_KEY,
  TRADE_REVIEW_STRATEGIES_QUERY_KEY,
  tradeReviewApi,
  type MistakeTag,
  type ReviewStrategy,
  type TradeReviewItem,
} from './api'
import {
  filterTradeReviewItems,
  mistakeTagLabel,
  MISTAKE_TAG_OPTIONS,
  type ReviewPnlFilter,
} from './reviewView'
import { TradeReviewEditorDialog } from './TradeReviewEditorDialog'

const selectClass = 'min-h-11 rounded-input border border-border bg-base px-3 text-xs text-secondary outline-none focus:border-accent focus:ring-2 focus:ring-accent/20 lg:min-h-9'

function signedMoney(value: number | null): string {
  if (value == null || !Number.isFinite(value)) return '--'
  const prefix = value > 0 ? '+¥' : value < 0 ? '-¥' : '¥'
  return `${prefix}${Math.abs(value).toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`
}

function percent(value: number | null): string {
  if (value == null || !Number.isFinite(value)) return '--'
  return `${value > 0 ? '+' : ''}${(value * 100).toFixed(2)}%`
}

function reviewSummary(item: TradeReviewItem): string {
  const review = item.review
  if (!review) return ''
  return review.conclusion || review.exit_reason || review.entry_reason || review.expectation || review.invalidation
}

export function TradeReviewPanel({ portfolio }: { portfolio: Portfolio }) {
  const queryClient = useQueryClient()
  const [strategyId, setStrategyId] = useState('')
  const [mistakeTag, setMistakeTag] = useState<'' | MistakeTag>('')
  const [pnlResult, setPnlResult] = useState<ReviewPnlFilter>('all')
  const [editor, setEditor] = useState<(TradeReviewItem & { trade: NonNullable<TradeReviewItem['trade']> }) | null>(null)
  const [orphanToDelete, setOrphanToDelete] = useState<TradeReviewItem | null>(null)
  const tradeKey = useMemo(
    () => portfolio.trades.map(trade => (
      [trade.id, trade.updated_at, trade.side, trade.quantity, trade.price, trade.fees, trade.trade_date].join(':')
    )).join('|'),
    [portfolio.trades],
  )
  const reviewsQuery = useQuery({
    queryKey: [...TRADE_REVIEWS_QUERY_KEY, tradeKey],
    queryFn: tradeReviewApi.list,
  })
  const strategiesQuery = useQuery({
    queryKey: TRADE_REVIEW_STRATEGIES_QUERY_KEY,
    queryFn: tradeReviewApi.strategies,
    staleTime: 60_000,
  })
  const strategies = useMemo(() => {
    const options = new Map<string, ReviewStrategy>()
    for (const strategy of strategiesQuery.data?.strategies ?? []) options.set(strategy.id, strategy)
    for (const item of reviewsQuery.data?.items ?? []) {
      const id = item.review?.strategy_id
      if (id && !options.has(id)) options.set(id, { id, name: `${id}（当前不可用）` })
    }
    return [...options.values()].sort((a, b) => a.name.localeCompare(b.name, 'zh-CN'))
  }, [reviewsQuery.data?.items, strategiesQuery.data?.strategies])
  const strategyNames = useMemo(
    () => new Map(strategies.map(strategy => [strategy.id, strategy.name])),
    [strategies],
  )
  const visibleItems = useMemo(
    () => filterTradeReviewItems(reviewsQuery.data?.items ?? [], { strategyId, mistakeTag, pnlResult }),
    [mistakeTag, pnlResult, reviewsQuery.data?.items, strategyId],
  )
  const summary = reviewsQuery.data?.summary
  const progress = summary?.sell_count
    ? Math.round((summary.reviewed_sell_count / summary.sell_count) * 100)
    : 0

  const removeOrphan = useMutation({
    mutationFn: (tradeId: string) => tradeReviewApi.delete(tradeId),
    onSuccess: async () => {
      setOrphanToDelete(null)
      await queryClient.invalidateQueries({ queryKey: TRADE_REVIEWS_QUERY_KEY })
      toast('孤立复盘已删除', 'success')
    },
    onError: error => {
      toast(error instanceof Error ? error.message : '孤立复盘删除失败', 'error')
    },
  })

  if (reviewsQuery.isError) {
    return (
      <section aria-labelledby="trade-review-title" className="rounded-card border border-border bg-surface p-6 text-center">
        <AlertTriangle className="mx-auto h-7 w-7 text-danger" />
        <h2 id="trade-review-title" className="mt-3 text-sm font-semibold text-foreground">交易复盘读取失败</h2>
        <button type="button" onClick={() => reviewsQuery.refetch()} className="mt-4 inline-flex min-h-11 items-center gap-2 rounded-btn border border-border px-4 text-sm text-secondary hover:bg-elevated">
          <RefreshCw className="h-4 w-4" />重新读取
        </button>
      </section>
    )
  }

  return (
    <>
      <section aria-labelledby="trade-review-title" className="overflow-hidden rounded-card border border-border bg-surface">
        <div className="flex min-h-12 items-center justify-between gap-3 border-b border-border px-4 py-2.5">
          <div className="flex min-w-0 items-center gap-2.5">
            <BookOpenCheck className="h-4 w-4 shrink-0 text-g-research" />
            <div className="min-w-0">
              <h2 id="trade-review-title" className="text-sm font-semibold text-foreground">交易复盘</h2>
              <p className="mt-0.5 text-[11px] leading-4 text-muted sm:truncate">盈亏按交易流水实时归因，复盘结论独立保存。</p>
            </div>
          </div>
          <span className="shrink-0 font-mono text-[11px] text-muted">
            {summary ? `${summary.reviewed_sell_count}/${summary.sell_count} 笔卖出已复盘` : '读取中'}
          </span>
        </div>
        <div className="h-0.5 bg-base" aria-hidden="true">
          <div className="h-full bg-g-research transition-[width] duration-300" style={{ width: `${progress}%` }} />
        </div>

        <div className="grid gap-2 border-b border-border bg-base/40 p-3 sm:grid-cols-3">
          <label className="grid gap-1">
            <span className="text-[10px] font-medium text-muted">策略</span>
            <select value={strategyId} onChange={event => setStrategyId(event.target.value)} className={selectClass}>
              <option value="">全部策略</option>
              {strategies.map(strategy => <option key={strategy.id} value={strategy.id}>{strategy.name}</option>)}
            </select>
          </label>
          <label className="grid gap-1">
            <span className="text-[10px] font-medium text-muted">错误归因</span>
            <select value={mistakeTag} onChange={event => setMistakeTag(event.target.value as '' | MistakeTag)} className={selectClass}>
              <option value="">全部标签</option>
              {MISTAKE_TAG_OPTIONS.map(option => <option key={option.value} value={option.value}>{option.label}</option>)}
            </select>
          </label>
          <label className="grid gap-1">
            <span className="text-[10px] font-medium text-muted">盈亏结果</span>
            <select value={pnlResult} onChange={event => setPnlResult(event.target.value as ReviewPnlFilter)} className={selectClass}>
              <option value="all">全部结果</option>
              <option value="profit">盈利卖出</option>
              <option value="loss">亏损卖出</option>
              <option value="breakeven">盈亏平衡</option>
              <option value="planned">买入计划</option>
            </select>
          </label>
        </div>

        {reviewsQuery.isLoading ? (
          <div className="flex min-h-40 items-center justify-center text-sm text-muted">读取交易归因...</div>
        ) : visibleItems.length === 0 ? (
          <div className="flex min-h-40 flex-col items-center justify-center px-6 text-center">
            <BookOpen className="h-7 w-7 text-muted" />
            <p className="mt-3 text-sm text-muted">{portfolio.trades.length === 0 ? '记录交易后可在这里建立计划和复盘。' : '当前筛选条件下没有交易。'}</p>
          </div>
        ) : (
          <div className="max-h-[36rem] divide-y divide-border overflow-y-auto">
            {visibleItems.map(item => {
              if (!item.trade) {
                return (
                  <div key={item.review!.id} className="flex items-center justify-between gap-4 px-4 py-3.5">
                    <div className="min-w-0">
                      <div className="flex items-center gap-2 text-xs font-medium text-warning"><AlertTriangle className="h-3.5 w-3.5" />原交易已删除</div>
                      <p className="mt-1 truncate text-xs text-muted">{reviewSummary(item) || item.review!.trade_id}</p>
                    </div>
                    <button type="button" onClick={() => setOrphanToDelete(item)} className="flex h-9 w-9 shrink-0 items-center justify-center rounded-btn text-muted hover:bg-danger/10 hover:text-danger" aria-label="删除孤立复盘" title="删除复盘"><Trash2 className="h-3.5 w-3.5" /></button>
                  </div>
                )
              }
              const trade = item.trade
              const pnl = item.attribution?.realized_pnl ?? null
              return (
                <div key={trade.id} className="grid gap-3 px-4 py-3.5 transition-colors hover:bg-elevated/30 md:grid-cols-[minmax(0,1.35fr)_minmax(0,1fr)_auto] md:items-center">
                  <div className="min-w-0">
                    <div className="flex items-center gap-2">
                      <span className={cn('text-xs font-semibold', trade.side === 'buy' ? 'text-bull' : 'text-bear')}>{trade.side === 'buy' ? '买入' : '卖出'}</span>
                      <span className="truncate text-sm font-medium text-foreground">{trade.name || trade.symbol}</span>
                      {item.review && <BookOpenCheck className="h-3.5 w-3.5 shrink-0 text-g-research" aria-label="已复盘" />}
                    </div>
                    <div className="mt-1 font-mono text-[10px] text-muted">{trade.trade_date} · {trade.symbol} · {trade.quantity.toLocaleString('zh-CN')} 股</div>
                    {reviewSummary(item) && <p className="mt-1.5 line-clamp-1 text-xs text-secondary">{reviewSummary(item)}</p>}
                  </div>

                  <div className="min-w-0">
                    <div className="flex items-baseline gap-2">
                      <span className={cn('font-mono text-sm font-semibold', pnl == null || pnl === 0 ? 'text-muted' : pnl > 0 ? 'text-bull' : 'text-bear')}>{trade.side === 'buy' ? '计划记录' : signedMoney(pnl)}</span>
                      {trade.side === 'sell' && <span className="font-mono text-[10px] text-muted">{percent(item.attribution?.return_pct ?? null)} · {item.attribution?.holding_days ?? '--'} 天</span>}
                    </div>
                    <div className="mt-1.5 flex flex-wrap gap-1.5">
                      {item.review?.strategy_id && <span className="truncate text-[10px] text-accent">{strategyNames.get(item.review.strategy_id) ?? item.review.strategy_id}</span>}
                      {item.review?.mistake_tags.map(tag => <span key={tag} className="rounded border border-danger/20 bg-danger/8 px-1.5 py-0.5 text-[10px] text-danger">{mistakeTagLabel(tag)}</span>)}
                      {!item.review && <span className="text-[10px] text-muted">尚未记录</span>}
                    </div>
                  </div>

                  <div className="flex justify-end">
                    <button
                      type="button"
                      onClick={() => setEditor(item as TradeReviewItem & { trade: NonNullable<TradeReviewItem['trade']> })}
                      className="inline-flex min-h-10 items-center gap-2 rounded-btn border border-border px-3 text-xs text-secondary hover:bg-elevated hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
                    >
                      <FilePenLine className="h-3.5 w-3.5" />{item.review ? '编辑复盘' : trade.side === 'buy' ? '记录计划' : '记录复盘'}
                    </button>
                  </div>
                </div>
              )
            })}
          </div>
        )}
      </section>

      {editor && (
        <TradeReviewEditorDialog
          item={editor}
          strategies={strategies}
          onClose={() => setEditor(null)}
          onSaved={() => setEditor(null)}
        />
      )}

      <ConfirmDialog
        open={orphanToDelete !== null}
        title="删除孤立复盘？"
        message="原交易已经不存在，这条复盘文字将被永久删除。"
        confirmText="删除复盘"
        danger
        pending={removeOrphan.isPending}
        onCancel={() => setOrphanToDelete(null)}
        onConfirm={() => { if (orphanToDelete?.review) removeOrphan.mutate(orphanToDelete.review.trade_id) }}
      />
    </>
  )
}

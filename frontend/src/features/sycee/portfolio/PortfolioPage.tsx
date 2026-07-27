import { useLayoutEffect, useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  ArrowDownLeft,
  ArrowUpRight,
  History,
  Loader2,
  Pencil,
  Plus,
  RefreshCw,
  Trash2,
  WalletCards,
  Wifi,
} from 'lucide-react'

import { ConfirmDialog } from '@/components/ConfirmDialog'
import { PageHeader } from '@/components/PageHeader'
import { toast } from '@/components/Toast'
import { cn } from '@/lib/cn'
import { getNavIconMeta } from '@/lib/navRegistry'
import {
  PORTFOLIO_QUERY_KEY,
  portfolioApi,
  type Portfolio,
  type PortfolioTrade,
  type PortfolioTradeSide,
} from './api'
import { buildPortfolioView, type PortfolioPositionView } from './portfolioView'
import { PortfolioSellAlertPanel } from './PortfolioSellAlertPanel'
import { TradeEditorDialog } from './TradeEditorDialog'

interface EditorState {
  trade: PortfolioTrade | null
  initial?: { symbol?: string; name?: string; side?: PortfolioTradeSide }
}

const EMPTY_PORTFOLIO: Portfolio = {
  trades: [],
  positions: [],
  summary: { position_count: 0, trade_count: 0, cost_value: 0, realized_pnl: 0 },
}

const moneyFormatter = new Intl.NumberFormat('zh-CN', {
  style: 'currency',
  currency: 'CNY',
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
})
const numberFormatter = new Intl.NumberFormat('zh-CN', { maximumFractionDigits: 4 })

function money(value: number | null): string {
  return value == null || !Number.isFinite(value) ? '--' : moneyFormatter.format(value)
}

function signedMoney(value: number | null): string {
  if (value == null || !Number.isFinite(value)) return '--'
  return `${value > 0 ? '+' : ''}${moneyFormatter.format(value)}`
}

function percent(value: number | null): string {
  if (value == null || !Number.isFinite(value)) return '--'
  return `${value > 0 ? '+' : ''}${(value * 100).toFixed(2)}%`
}

function price(value: number | null): string {
  if (value == null || !Number.isFinite(value)) return '--'
  return value.toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 3 })
}

function pnlClass(value: number | null): string {
  if (value == null || value === 0) return 'text-muted'
  return value > 0 ? 'text-bull' : 'text-bear'
}

function SummaryMetric({
  label,
  value,
  hint,
  valueClassName,
}: {
  label: string
  value: string
  hint: string
  valueClassName?: string
}) {
  return (
    <div className="min-w-0 px-4 py-3 lg:px-5 lg:py-4">
      <div className="text-[11px] font-medium text-muted">{label}</div>
      <div className={cn('mt-1 truncate font-mono text-lg font-semibold tabular-nums text-foreground lg:text-xl', valueClassName)} title={value}>
        {value}
      </div>
      <div className="mt-1 truncate text-[10px] text-muted">{hint}</div>
    </div>
  )
}

function QuoteLabel({ position }: { position: PortfolioPositionView }) {
  if (position.current_price == null) {
    return <span className="text-[10px] text-warning">待补行情</span>
  }
  if (position.is_live) {
    return (
      <span className="inline-flex items-center gap-1 text-[10px] text-bull">
        <span className="h-1.5 w-1.5 rounded-full bg-bull" aria-hidden="true" />实盘
      </span>
    )
  }
  return <span className="font-mono text-[10px] text-muted">{position.quote_date} 收盘</span>
}

function PositionActions({ position, onTrade }: {
  position: PortfolioPositionView
  onTrade: (position: PortfolioPositionView, side: PortfolioTradeSide) => void
}) {
  return (
    <div className="flex items-center justify-end gap-1">
      <button
        type="button"
        onClick={() => onTrade(position, 'buy')}
        className="flex h-9 w-9 items-center justify-center rounded-btn text-muted transition-colors hover:bg-bull/10 hover:text-bull focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
        aria-label={`买入 ${position.name || position.symbol}`}
        title="记录买入"
      >
        <ArrowDownLeft className="h-4 w-4" />
      </button>
      <button
        type="button"
        onClick={() => onTrade(position, 'sell')}
        className="flex h-9 w-9 items-center justify-center rounded-btn text-muted transition-colors hover:bg-bear/10 hover:text-bear focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
        aria-label={`卖出 ${position.name || position.symbol}`}
        title="记录卖出"
      >
        <ArrowUpRight className="h-4 w-4" />
      </button>
    </div>
  )
}

export function PortfolioPage() {
  const queryClient = useQueryClient()
  const [editor, setEditor] = useState<EditorState | null>(null)
  const [deleteTarget, setDeleteTarget] = useState<PortfolioTrade | null>(null)

  useLayoutEffect(() => {
    document.querySelector('main')?.scrollTo({ top: 0 })
  }, [])

  const navMeta = getNavIconMeta('/portfolio')
  const portfolioQuery = useQuery({
    queryKey: PORTFOLIO_QUERY_KEY,
    queryFn: portfolioApi.get,
  })
  const portfolio = portfolioQuery.data ?? EMPTY_PORTFOLIO
  const symbols = useMemo(
    () => portfolio.positions.map(position => position.symbol).sort(),
    [portfolio.positions],
  )
  const symbolKey = symbols.join(',')
  const quotesQuery = useQuery({
    queryKey: ['sycee', 'portfolio-quotes', symbolKey],
    queryFn: () => portfolioApi.quotes(symbols),
    enabled: symbols.length > 0,
    staleTime: 15_000,
    refetchInterval: 30_000,
  })
  const view = useMemo(
    () => buildPortfolioView(portfolio, quotesQuery.data ?? {}),
    [portfolio, quotesQuery.data],
  )
  const liveCount = view.positions.filter(position => position.is_live).length
  const hasPricedPosition = view.positions.length > view.summary.unpriced_count
  const floatingReturn = view.summary.priced_cost_value > 0
    ? view.summary.unrealized_pnl / view.summary.priced_cost_value
    : null

  const remove = useMutation({
    mutationFn: portfolioApi.deleteTrade,
    onSuccess: async ({ portfolio: updated }) => {
      queryClient.setQueryData(PORTFOLIO_QUERY_KEY, updated)
      setDeleteTarget(null)
      await queryClient.invalidateQueries({ queryKey: ['sycee', 'portfolio-quotes'] })
      toast('交易记录已删除', 'success')
    },
  })

  const openPositionTrade = (position: PortfolioPositionView, side: PortfolioTradeSide) => {
    setEditor({
      trade: null,
      initial: { symbol: position.symbol, name: position.name, side },
    })
  }

  const pendingQuoteHint = view.summary.unpriced_count > 0
    ? `${view.summary.unpriced_count} 只待补价`
    : liveCount > 0 ? `${liveCount} 只实盘报价` : '最近可用收盘价'

  if (portfolioQuery.isLoading) {
    return (
      <div className="flex min-h-[50vh] items-center justify-center gap-2 text-sm text-muted">
        <Loader2 className="h-4 w-4 animate-spin" />读取持仓数据
      </div>
    )
  }

  if (portfolioQuery.isError) {
    return (
      <div className="flex min-h-[50vh] flex-col items-center justify-center px-6 text-center">
        <WalletCards className="h-9 w-9 text-danger" />
        <h1 className="mt-4 text-base font-semibold text-foreground">持仓数据读取失败</h1>
        <p className="mt-2 max-w-md text-sm leading-6 text-muted">
          {portfolioQuery.error instanceof Error ? portfolioQuery.error.message : '请检查服务状态后重试。'}
        </p>
        <button type="button" onClick={() => portfolioQuery.refetch()} className="mt-5 inline-flex min-h-11 items-center gap-2 rounded-btn border border-border px-4 text-sm text-secondary hover:bg-elevated focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent">
          <RefreshCw className="h-4 w-4" />重新读取
        </button>
      </div>
    )
  }

  return (
    <>
      <PageHeader
        title="我的持仓"
        subtitle="按交易流水计算移动成本、浮动盈亏与已实现盈亏。"
        icon={navMeta?.icon}
        group={navMeta?.group}
        right={
          <div className="flex min-w-max items-center justify-end gap-2">
            <button
              type="button"
              onClick={() => quotesQuery.refetch()}
              disabled={symbols.length === 0 || quotesQuery.isFetching}
              className="flex h-11 w-11 items-center justify-center rounded-btn border border-border text-muted transition-colors hover:bg-elevated hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent disabled:cursor-not-allowed disabled:opacity-40 lg:h-9 lg:w-9"
              aria-label="刷新持仓行情"
              title="刷新行情"
            >
              <RefreshCw className={cn('h-4 w-4', quotesQuery.isFetching && 'animate-spin')} />
            </button>
            <button
              type="button"
              onClick={() => setEditor({ trade: null })}
              className="inline-flex min-h-11 items-center justify-center gap-2 rounded-btn bg-accent px-4 text-sm font-medium text-white transition-colors hover:bg-accent/90 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent focus-visible:ring-offset-2 focus-visible:ring-offset-base lg:min-h-9"
            >
              <Plus className="h-4 w-4" />
              记录交易
            </button>
          </div>
        }
      />

      <section aria-label="组合摘要" className="grid grid-cols-2 divide-x divide-y divide-border border-b border-border bg-surface/40 lg:grid-cols-4 lg:divide-y-0">
        <SummaryMetric label="持仓成本" value={money(view.summary.cost_value)} hint={`${portfolio.summary.position_count} 只当前持仓`} />
        <SummaryMetric label="当前市值" value={hasPricedPosition ? money(view.summary.market_value) : '--'} hint={pendingQuoteHint} />
        <SummaryMetric
          label="浮动盈亏"
          value={hasPricedPosition ? signedMoney(view.summary.unrealized_pnl) : '--'}
          hint={hasPricedPosition ? `收益率 ${percent(floatingReturn)}` : '取得行情后计算'}
          valueClassName={hasPricedPosition ? pnlClass(view.summary.unrealized_pnl) : undefined}
        />
        <SummaryMetric label="已实现盈亏" value={signedMoney(view.summary.realized_pnl)} hint={`${portfolio.summary.trade_count} 笔累计流水`} valueClassName={pnlClass(view.summary.realized_pnl)} />
      </section>

      <div className="space-y-4 p-3 lg:p-5">
        <PortfolioSellAlertPanel portfolio={portfolio} />

        <section aria-labelledby="portfolio-positions-title" className="overflow-hidden rounded-card border border-border bg-surface">
          <div className="flex min-h-12 items-center justify-between gap-3 border-b border-border px-4 py-2.5">
            <div className="min-w-0">
              <h2 id="portfolio-positions-title" className="text-sm font-semibold text-foreground">当前持仓</h2>
              <p className="mt-0.5 text-[11px] text-muted">行情每 30 秒尝试更新，非实时数据标注收盘日期。</p>
            </div>
            <div className="flex shrink-0 items-center gap-2 text-[11px] text-muted">
              {liveCount > 0 && <Wifi className="h-3.5 w-3.5 text-bull" />}
              <span className="font-mono">{portfolio.summary.position_count}</span> 只
            </div>
          </div>

          {view.positions.length === 0 ? (
            <div className="flex min-h-64 flex-col items-center justify-center px-6 py-12 text-center">
              <WalletCards className="h-9 w-9 text-muted" />
              <h3 className="mt-4 text-base font-semibold text-foreground">还没有持仓</h3>
              <p className="mt-2 max-w-md text-sm leading-6 text-muted">记录第一笔买入后，这里会自动计算持仓数量、移动均价和浮动盈亏。</p>
              <button type="button" onClick={() => setEditor({ trade: null })} className="mt-5 inline-flex min-h-11 items-center gap-2 rounded-btn bg-accent px-4 text-sm font-medium text-white hover:bg-accent/90 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent">
                <Plus className="h-4 w-4" />记录买入
              </button>
            </div>
          ) : (
            <>
              <div className="hidden overflow-x-auto md:block">
                <table className="w-full min-w-[820px] border-collapse text-sm">
                  <thead className="bg-base/60 text-[11px] font-medium text-muted">
                    <tr>
                      <th className="px-4 py-2 text-left">标的</th>
                      <th className="px-4 py-2 text-right">持仓 / 均价</th>
                      <th className="px-4 py-2 text-right">最新价</th>
                      <th className="px-4 py-2 text-right">持仓市值</th>
                      <th className="px-4 py-2 text-right">浮动盈亏</th>
                      <th className="w-24 px-4 py-2 text-right">操作</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-border">
                    {view.positions.map(position => (
                      <tr key={position.symbol} className="transition-colors hover:bg-elevated/35">
                        <td className="px-4 py-3">
                          <div className="font-medium text-foreground">{position.name || position.symbol}</div>
                          <div className="mt-0.5 font-mono text-[11px] text-muted">{position.symbol}</div>
                        </td>
                        <td className="px-4 py-3 text-right">
                          <div className="font-mono tabular-nums text-foreground">{numberFormatter.format(position.quantity)} 股</div>
                          <div className="mt-0.5 font-mono text-[11px] text-muted">均价 {price(position.average_cost)}</div>
                        </td>
                        <td className="px-4 py-3 text-right">
                          <div className="font-mono tabular-nums text-foreground">{price(position.current_price)}</div>
                          <div className="mt-0.5 flex justify-end gap-2"><QuoteLabel position={position} /></div>
                        </td>
                        <td className="px-4 py-3 text-right font-mono tabular-nums text-foreground">{money(position.market_value)}</td>
                        <td className={cn('px-4 py-3 text-right font-mono tabular-nums', pnlClass(position.unrealized_pnl))}>
                          <div>{signedMoney(position.unrealized_pnl)}</div>
                          <div className="mt-0.5 text-[11px]">{percent(position.return_pct)}</div>
                        </td>
                        <td className="px-4 py-3"><PositionActions position={position} onTrade={openPositionTrade} /></td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>

              <div className="divide-y divide-border md:hidden">
                {view.positions.map(position => (
                  <article key={position.symbol} className="px-4 py-4">
                    <div className="flex items-start justify-between gap-3">
                      <div className="min-w-0">
                        <h3 className="truncate text-sm font-semibold text-foreground">{position.name || position.symbol}</h3>
                        <p className="mt-0.5 font-mono text-[11px] text-muted">{position.symbol}</p>
                      </div>
                      <div className="shrink-0 text-right">
                        <div className="font-mono text-base font-semibold tabular-nums text-foreground">{price(position.current_price)}</div>
                        <QuoteLabel position={position} />
                      </div>
                    </div>
                    <dl className="mt-4 grid grid-cols-3 gap-3 border-y border-border py-3">
                      <div><dt className="text-[10px] text-muted">持仓</dt><dd className="mt-1 font-mono text-xs text-secondary">{numberFormatter.format(position.quantity)} 股</dd></div>
                      <div><dt className="text-[10px] text-muted">移动均价</dt><dd className="mt-1 font-mono text-xs text-secondary">{price(position.average_cost)}</dd></div>
                      <div className="text-right"><dt className="text-[10px] text-muted">持仓市值</dt><dd className="mt-1 font-mono text-xs text-secondary">{money(position.market_value)}</dd></div>
                    </dl>
                    <div className="mt-3 flex items-center justify-between gap-3">
                      <div>
                        <div className="text-[10px] text-muted">浮动盈亏</div>
                        <div className={cn('mt-0.5 font-mono text-sm font-semibold', pnlClass(position.unrealized_pnl))}>{signedMoney(position.unrealized_pnl)} <span className="text-xs font-normal">{percent(position.return_pct)}</span></div>
                      </div>
                      <PositionActions position={position} onTrade={openPositionTrade} />
                    </div>
                  </article>
                ))}
              </div>
            </>
          )}
        </section>

        <section aria-labelledby="portfolio-history-title" className="overflow-hidden rounded-card border border-border bg-surface">
          <div className="flex min-h-12 items-center justify-between gap-3 border-b border-border px-4 py-2.5">
            <div className="flex items-center gap-2">
              <History className="h-4 w-4 text-g-core" />
              <h2 id="portfolio-history-title" className="text-sm font-semibold text-foreground">交易流水</h2>
            </div>
            <span className="font-mono text-[11px] text-muted">{portfolio.summary.trade_count} 笔</span>
          </div>

          {portfolio.trades.length === 0 ? (
            <div className="px-4 py-10 text-center text-sm text-muted">保存交易后，流水会按交易日期倒序显示。</div>
          ) : (
            <>
              <div className="hidden overflow-x-auto md:block">
                <table className="w-full min-w-[860px] border-collapse text-sm">
                  <thead className="bg-base/60 text-[11px] font-medium text-muted">
                    <tr>
                      <th className="px-4 py-2 text-left">日期</th>
                      <th className="px-4 py-2 text-left">方向</th>
                      <th className="px-4 py-2 text-left">标的</th>
                      <th className="px-4 py-2 text-right">数量</th>
                      <th className="px-4 py-2 text-right">成交价</th>
                      <th className="px-4 py-2 text-right">费用</th>
                      <th className="px-4 py-2 text-left">备注</th>
                      <th className="w-24 px-4 py-2 text-right">操作</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-border">
                    {portfolio.trades.map(trade => (
                      <tr key={trade.id} className="transition-colors hover:bg-elevated/35">
                        <td className="px-4 py-3 font-mono text-xs text-secondary">{trade.trade_date}</td>
                        <td className="px-4 py-3">
                          <span className={cn('inline-flex items-center gap-1 rounded border px-2 py-0.5 text-[11px] font-medium', trade.side === 'buy' ? 'border-bull/25 bg-bull/10 text-bull' : 'border-bear/25 bg-bear/10 text-bear')}>
                            {trade.side === 'buy' ? <ArrowDownLeft className="h-3 w-3" /> : <ArrowUpRight className="h-3 w-3" />}
                            {trade.side === 'buy' ? '买入' : '卖出'}
                          </span>
                        </td>
                        <td className="px-4 py-3"><div className="font-medium text-foreground">{trade.name || trade.symbol}</div><div className="mt-0.5 font-mono text-[10px] text-muted">{trade.symbol}</div></td>
                        <td className="px-4 py-3 text-right font-mono tabular-nums text-secondary">{numberFormatter.format(trade.quantity)}</td>
                        <td className="px-4 py-3 text-right font-mono tabular-nums text-secondary">{price(trade.price)}</td>
                        <td className="px-4 py-3 text-right font-mono tabular-nums text-muted">{money(trade.fees)}</td>
                        <td className="max-w-56 truncate px-4 py-3 text-xs text-muted" title={trade.note}>{trade.note || '--'}</td>
                        <td className="px-4 py-3">
                          <div className="flex items-center justify-end gap-1">
                            <button type="button" onClick={() => setEditor({ trade })} className="flex h-9 w-9 items-center justify-center rounded-btn text-muted hover:bg-elevated hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent" aria-label="编辑交易" title="编辑"><Pencil className="h-3.5 w-3.5" /></button>
                            <button type="button" onClick={() => setDeleteTarget(trade)} className="flex h-9 w-9 items-center justify-center rounded-btn text-muted hover:bg-danger/10 hover:text-danger focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-danger" aria-label="删除交易" title="删除"><Trash2 className="h-3.5 w-3.5" /></button>
                          </div>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>

              <div className="divide-y divide-border md:hidden">
                {portfolio.trades.map(trade => (
                  <article key={trade.id} className="px-4 py-3.5">
                    <div className="flex items-start justify-between gap-3">
                      <div className="min-w-0">
                        <div className="flex items-center gap-2">
                          <span className={cn('text-xs font-semibold', trade.side === 'buy' ? 'text-bull' : 'text-bear')}>{trade.side === 'buy' ? '买入' : '卖出'}</span>
                          <span className="truncate text-sm font-medium text-foreground">{trade.name || trade.symbol}</span>
                        </div>
                        <div className="mt-1 font-mono text-[10px] text-muted">{trade.trade_date} · {trade.symbol}</div>
                      </div>
                      <div className="shrink-0 text-right font-mono text-xs text-secondary">
                        <div>{numberFormatter.format(trade.quantity)} 股 × {price(trade.price)}</div>
                        <div className="mt-1 text-[10px] text-muted">费用 {money(trade.fees)}</div>
                      </div>
                    </div>
                    {trade.note && <p className="mt-2 line-clamp-2 text-xs leading-5 text-muted">{trade.note}</p>}
                    <div className="mt-2 flex justify-end gap-1">
                      <button type="button" onClick={() => setEditor({ trade })} className="flex h-9 w-9 items-center justify-center rounded-btn text-muted hover:bg-elevated hover:text-foreground" aria-label="编辑交易"><Pencil className="h-3.5 w-3.5" /></button>
                      <button type="button" onClick={() => setDeleteTarget(trade)} className="flex h-9 w-9 items-center justify-center rounded-btn text-muted hover:bg-danger/10 hover:text-danger" aria-label="删除交易"><Trash2 className="h-3.5 w-3.5" /></button>
                    </div>
                  </article>
                ))}
              </div>
            </>
          )}
        </section>
      </div>

      {editor && (
        <TradeEditorDialog
          key={`${editor.trade?.id ?? 'new'}-${editor.initial?.symbol ?? ''}-${editor.initial?.side ?? ''}`}
          trade={editor.trade}
          initial={editor.initial}
          positions={portfolio.positions}
          onClose={() => setEditor(null)}
          onSaved={() => setEditor(null)}
        />
      )}

      <ConfirmDialog
        open={deleteTarget !== null}
        title="删除这笔交易？"
        message={deleteTarget ? `${deleteTarget.trade_date} 的${deleteTarget.side === 'buy' ? '买入' : '卖出'}记录将被删除，持仓成本和盈亏会重新计算。若删除后出现历史超卖，系统会拒绝操作。` : ''}
        confirmText="删除交易"
        danger
        pending={remove.isPending}
        onCancel={() => setDeleteTarget(null)}
        onConfirm={() => { if (deleteTarget) remove.mutate(deleteTarget.id) }}
      />
    </>
  )
}

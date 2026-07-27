import { useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import ReactECharts from 'echarts-for-react'
import {
  Check,
  CircleAlert,
  Clock3,
  Download,
  GitCompareArrows,
  ListPlus,
  Loader2,
  Play,
  Square,
  X,
} from 'lucide-react'

import { PageHeader } from '@/components/PageHeader'
import { toast } from '@/components/Toast'
import { cn } from '@/lib/cn'
import { getNavIconMeta } from '@/lib/navRegistry'
import { useChartTheme } from '@/lib/theme'
import { strategyCompareApi, type BacktestProgress, type ComparableStrategy } from './api'
import {
  buildMetricRow,
  buildStrategyDefaults,
  normalizeEquityCurve,
  validateComparison,
  type ComparisonMetricRow,
  type ComparisonResult,
  type ComparisonSettings,
} from './comparison'
import { downloadComparisonCsv } from './comparisonCsv'
import {
  ComparisonCancelledError,
  startStrategyBacktest,
  type StrategyRunHandle,
} from './comparisonStream'

type QueueStatus = 'pending' | 'running' | 'completed' | 'error' | 'cancelled'

interface QueueItem {
  strategyId: string
  strategyName: string
  status: QueueStatus
  progress: BacktestProgress | null
  result: ComparisonResult | null
  error: string | null
}

const FIELD_CLASS = 'min-h-10 w-full rounded-input border border-border bg-base px-3 text-sm text-foreground outline-none transition-colors focus:border-accent focus:ring-2 focus:ring-accent/20 disabled:cursor-not-allowed disabled:opacity-50'
const CURVE_COLORS = ['#3b82f6', '#f04438', '#12b76a', '#f59e0b', '#8b5cf6']

function isoDate(date: Date): string {
  return new Intl.DateTimeFormat('en-CA', {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
  }).format(date)
}

function defaultDates(): { start: string; end: string } {
  const end = new Date()
  const start = new Date(end)
  start.setMonth(start.getMonth() - 3)
  return { start: isoDate(start), end: isoDate(end) }
}

function percent(value: number | null, signed = true): string {
  if (value == null) return '--'
  return `${signed && value > 0 ? '+' : ''}${(value * 100).toFixed(2)}%`
}

function number(value: number | null, digits = 2): string {
  return value == null ? '--' : value.toFixed(digits)
}

function statusLabel(status: QueueStatus): string {
  return {
    pending: '等待',
    running: '运行中',
    completed: '完成',
    error: '失败',
    cancelled: '已取消',
  }[status]
}

function statusClass(status: QueueStatus): string {
  if (status === 'completed') return 'text-bear'
  if (status === 'error') return 'text-danger'
  if (status === 'running') return 'text-accent'
  return 'text-muted'
}

function EquityComparisonChart({ results }: { results: ComparisonResult[] }) {
  const chartTheme = useChartTheme()
  const curves = useMemo(() => results.map(normalizeEquityCurve), [results])
  const dates = useMemo(() => Array.from(new Set(
    curves.flatMap(curve => curve.points.map(point => point.date)),
  )).sort(), [curves])
  const option = useMemo(() => ({
    animation: false,
    color: CURVE_COLORS,
    tooltip: {
      trigger: 'axis',
      backgroundColor: chartTheme.tooltipBg,
      borderColor: chartTheme.tooltipBorder,
      textStyle: { color: chartTheme.tooltipText, fontSize: 12 },
      valueFormatter: (value: number | string) => {
        const numeric = Number(value)
        return Number.isFinite(numeric) ? `${((numeric - 1) * 100).toFixed(2)}%` : '--'
      },
    },
    legend: {
      top: 4,
      type: 'scroll',
      textStyle: { color: chartTheme.text, fontSize: 11 },
    },
    grid: { left: 54, right: 18, top: 48, bottom: 42 },
    xAxis: {
      type: 'category',
      data: dates,
      boundaryGap: false,
      axisLine: { lineStyle: { color: chartTheme.border } },
      axisTick: { show: false },
      axisLabel: { color: chartTheme.text, fontSize: 10, hideOverlap: true },
    },
    yAxis: {
      type: 'value',
      scale: true,
      axisLine: { show: false },
      axisTick: { show: false },
      axisLabel: {
        color: chartTheme.text,
        fontSize: 10,
        formatter: (value: number) => `${((value - 1) * 100).toFixed(0)}%`,
      },
      splitLine: { lineStyle: { color: chartTheme.grid } },
    },
    dataZoom: [{ type: 'inside', filterMode: 'none' }],
    series: curves.map(curve => {
      const byDate = new Map(curve.points.map(point => [point.date, point.value]))
      return {
        name: curve.strategyName,
        type: 'line',
        data: dates.map(date => byDate.get(date) ?? null),
        showSymbol: false,
        connectNulls: false,
        lineStyle: { width: 2 },
        emphasis: { focus: 'series' },
      }
    }),
  }), [chartTheme, curves, dates])

  return (
    <ReactECharts
      option={option}
      notMerge
      lazyUpdate
      className="h-[300px] w-full sm:h-[360px]"
      style={{ height: '100%', minHeight: 300, width: '100%' }}
    />
  )
}

function StrategyPicker({
  strategies,
  selected,
  disabled,
  onToggle,
}: {
  strategies: ComparableStrategy[]
  selected: string[]
  disabled: boolean
  onToggle: (strategyId: string) => void
}) {
  return (
    <fieldset className="border-b border-border px-4 py-4">
      <div className="flex items-center justify-between gap-3">
        <legend className="text-xs font-semibold text-secondary">策略</legend>
        <span className="font-mono text-[11px] text-muted">{selected.length} / 5</span>
      </div>
      <div className="mt-3 max-h-64 space-y-1 overflow-y-auto pr-1">
        {strategies.map(strategy => {
          const checked = selected.includes(strategy.id)
          const limitReached = selected.length >= 5 && !checked
          return (
            <label
              key={strategy.id}
              className={cn(
                'flex min-h-12 cursor-pointer items-start gap-3 rounded-btn px-2.5 py-2 transition-colors',
                checked ? 'bg-accent/10' : 'hover:bg-elevated/60',
                (disabled || limitReached) && 'cursor-not-allowed opacity-50',
              )}
            >
              <input
                type="checkbox"
                checked={checked}
                disabled={disabled || limitReached}
                onChange={() => onToggle(strategy.id)}
                className="sr-only"
              />
              <span className={cn(
                'mt-0.5 flex h-4 w-4 shrink-0 items-center justify-center rounded-input border',
                checked ? 'border-accent bg-accent text-white' : 'border-border bg-base',
              )}>
                {checked && <Check className="h-3 w-3" strokeWidth={3} />}
              </span>
              <span className="min-w-0">
                <span className="block truncate text-sm font-medium text-foreground">{strategy.name}</span>
                <span className="mt-0.5 block truncate text-[10px] text-muted">{strategy.description}</span>
              </span>
            </label>
          )
        })}
      </div>
    </fieldset>
  )
}

export function StrategyComparePage() {
  const dates = useMemo(defaultDates, [])
  const navMeta = getNavIconMeta('/strategy-compare')
  const [selectedIds, setSelectedIds] = useState<string[]>([])
  const [symbols, setSymbols] = useState<string[]>([])
  const [symbolDraft, setSymbolDraft] = useState('')
  const [start, setStart] = useState(dates.start)
  const [end, setEnd] = useState(dates.end)
  const [initialCapital, setInitialCapital] = useState('1000000')
  const [maxPositions, setMaxPositions] = useState('10')
  const [commission, setCommission] = useState('2')
  const [stampTax, setStampTax] = useState('1')
  const [slippage, setSlippage] = useState('5')
  const [queue, setQueue] = useState<QueueItem[]>([])
  const [isRunning, setIsRunning] = useState(false)
  const [resultSettings, setResultSettings] = useState<ComparisonSettings | null>(null)
  const activeHandle = useRef<StrategyRunHandle | null>(null)
  const runToken = useRef(0)
  const initializedStrategies = useRef(false)
  const initializedWatchlist = useRef(false)

  useLayoutEffect(() => {
    document.querySelector('main')?.scrollTo({ top: 0 })
  }, [])

  useEffect(() => () => {
    runToken.current += 1
    activeHandle.current?.cancel()
  }, [])

  const strategiesQuery = useQuery({
    queryKey: ['sycee', 'strategy-compare', 'strategies'],
    queryFn: strategyCompareApi.strategies,
    staleTime: 60_000,
  })
  const watchlistQuery = useQuery({
    queryKey: ['sycee', 'strategy-compare', 'watchlist'],
    queryFn: strategyCompareApi.watchlist,
    staleTime: 30_000,
  })
  const strategies = useMemo(() => strategiesQuery.data?.strategies ?? [], [strategiesQuery.data])

  useEffect(() => {
    if (initializedStrategies.current || strategies.length < 2) return
    initializedStrategies.current = true
    const preferred = ['bullish_alignment', 'high_turnover_surge']
      .filter(id => strategies.some(strategy => strategy.id === id))
    setSelectedIds(preferred.length === 2 ? preferred : strategies.slice(0, 2).map(strategy => strategy.id))
  }, [strategies])

  useEffect(() => {
    const watchlist = watchlistQuery.data?.symbols
    if (initializedWatchlist.current || !watchlist) return
    initializedWatchlist.current = true
    setSymbols(watchlist.map(item => item.symbol))
  }, [watchlistQuery.data])

  const completedResults = useMemo(() => queue.flatMap(item => item.result ? [item.result] : []), [queue])
  const metricRows = useMemo(() => completedResults.map(buildMetricRow), [completedResults])

  const settings = (): ComparisonSettings => ({
    symbols,
    start,
    end,
    initialCapital: Number(initialCapital),
    maxPositions: Number(maxPositions),
    commissionPct: Number(commission) / 10_000,
    stampTaxPct: Number(stampTax) / 1_000,
    slippageBps: Number(slippage),
  })

  const toggleStrategy = (strategyId: string) => {
    setSelectedIds(current => current.includes(strategyId)
      ? current.filter(id => id !== strategyId)
      : [...current, strategyId])
  }

  const addSymbols = (raw: string) => {
    const additions = raw
      .split(/[\s,，;；]+/)
      .map(symbol => symbol.trim().toUpperCase())
      .filter(symbol => /^[0-9A-Z._-]{2,32}$/.test(symbol))
    if (additions.length === 0) return
    setSymbols(current => Array.from(new Set([...current, ...additions])))
    setSymbolDraft('')
  }

  const loadWatchlist = () => {
    const items = watchlistQuery.data?.symbols ?? []
    if (items.length === 0) {
      toast('自选股为空', 'error')
      return
    }
    setSymbols(items.map(item => item.symbol))
  }

  const updateQueue = (strategyId: string, changes: Partial<QueueItem>) => {
    setQueue(current => current.map(item => (
      item.strategyId === strategyId ? { ...item, ...changes } : item
    )))
  }

  const runComparison = async () => {
    const runSettings = settings()
    const error = validateComparison(selectedIds, runSettings)
    if (error) {
      toast(error, 'error')
      return
    }
    const selected = selectedIds
      .map(id => strategies.find(strategy => strategy.id === id))
      .filter((strategy): strategy is ComparableStrategy => !!strategy)
    if (selected.length !== selectedIds.length) {
      toast('策略列表已变化，请刷新后重试', 'error')
      return
    }

    const token = ++runToken.current
    setResultSettings(runSettings)
    setQueue(selected.map(strategy => ({
      strategyId: strategy.id,
      strategyName: strategy.name,
      status: 'pending',
      progress: null,
      result: null,
      error: null,
    })))
    setIsRunning(true)

    for (const strategy of selected) {
      if (runToken.current !== token) break
      updateQueue(strategy.id, { status: 'running', progress: null })
      const defaults = buildStrategyDefaults(strategy)
      const handle = startStrategyBacktest({
        strategyId: strategy.id,
        ...runSettings,
        params: defaults.params,
        overrides: defaults.overrides,
      }, progress => updateQueue(strategy.id, { progress }))
      activeHandle.current = handle
      try {
        const result = await handle.promise
        if (runToken.current !== token) break
        updateQueue(strategy.id, {
          status: 'completed',
          result: { strategyId: strategy.id, strategyName: strategy.name, result },
          progress: null,
        })
      } catch (caught) {
        if (runToken.current !== token) break
        if (caught instanceof ComparisonCancelledError) {
          updateQueue(strategy.id, { status: 'cancelled', progress: null })
          break
        }
        updateQueue(strategy.id, {
          status: 'error',
          error: caught instanceof Error ? caught.message : '回测失败',
          progress: null,
        })
      } finally {
        if (activeHandle.current === handle) activeHandle.current = null
      }
    }
    if (runToken.current === token) setIsRunning(false)
  }

  const cancelComparison = () => {
    runToken.current += 1
    activeHandle.current?.cancel()
    activeHandle.current = null
    setQueue(current => current.map(item => (
      item.status === 'running' || item.status === 'pending'
        ? { ...item, status: 'cancelled', progress: null }
        : item
    )))
    setIsRunning(false)
  }

  const exportCsv = () => {
    if (!resultSettings || metricRows.length === 0) return
    downloadComparisonCsv(metricRows, resultSettings)
    toast('策略对比 CSV 已导出', 'success')
  }

  if (strategiesQuery.isLoading) {
    return (
      <div className="flex min-h-[50vh] items-center justify-center gap-2 text-sm text-muted">
        <Loader2 className="h-4 w-4 animate-spin" />读取策略
      </div>
    )
  }

  if (strategiesQuery.isError) {
    return (
      <div className="flex min-h-[50vh] flex-col items-center justify-center px-6 text-center">
        <CircleAlert className="h-9 w-9 text-danger" />
        <h1 className="mt-4 text-base font-semibold text-foreground">策略列表读取失败</h1>
        <button type="button" onClick={() => strategiesQuery.refetch()} className="mt-5 min-h-11 rounded-btn border border-border px-4 text-sm text-secondary hover:bg-elevated">
          重新读取
        </button>
      </div>
    )
  }

  return (
    <div className="min-w-0">
      <PageHeader
        title="策略对比"
        subtitle="统一股票池、区间、资金与成交成本"
        icon={navMeta?.icon}
        group={navMeta?.group}
        right={(
          <div className="flex items-center justify-end gap-2">
            <button
              type="button"
              onClick={exportCsv}
              disabled={metricRows.length === 0}
              className="inline-flex min-h-10 items-center gap-2 rounded-btn border border-border px-3 text-sm text-secondary transition-colors hover:bg-elevated hover:text-foreground disabled:cursor-not-allowed disabled:opacity-40"
            >
              <Download className="h-4 w-4" />导出 CSV
            </button>
            {isRunning ? (
              <button type="button" onClick={cancelComparison} className="inline-flex min-h-10 items-center gap-2 rounded-btn bg-danger px-4 text-sm font-medium text-white hover:bg-danger/90">
                <Square className="h-3.5 w-3.5" fill="currentColor" />停止
              </button>
            ) : (
              <button type="button" onClick={runComparison} className="inline-flex min-h-10 items-center gap-2 rounded-btn bg-accent px-4 text-sm font-medium text-white hover:bg-accent/90">
                <Play className="h-4 w-4" fill="currentColor" />开始对比
              </button>
            )}
          </div>
        )}
      />

      <div className="grid min-w-0 xl:grid-cols-[340px_minmax(0,1fr)]">
        <aside className="min-w-0 border-b border-border bg-surface/35 xl:border-b-0 xl:border-r">
          <StrategyPicker strategies={strategies} selected={selectedIds} disabled={isRunning} onToggle={toggleStrategy} />

          <section className="border-b border-border px-4 py-4">
            <div className="flex items-center justify-between gap-3">
              <h2 className="text-xs font-semibold text-secondary">股票池</h2>
              <button
                type="button"
                onClick={loadWatchlist}
                disabled={isRunning || watchlistQuery.isLoading}
                className="inline-flex min-h-8 items-center gap-1.5 rounded-btn px-2 text-[11px] text-accent hover:bg-accent/10 disabled:opacity-40"
              >
                <ListPlus className="h-3.5 w-3.5" />载入自选
              </button>
            </div>
            <div className="mt-3 flex gap-2">
              <input
                value={symbolDraft}
                disabled={isRunning}
                onChange={event => setSymbolDraft(event.target.value)}
                onKeyDown={event => {
                  if (event.key === 'Enter' || event.key === ',') {
                    event.preventDefault()
                    addSymbols(symbolDraft)
                  }
                }}
                className={cn(FIELD_CLASS, 'min-w-0 font-mono')}
                placeholder="代码，回车添加"
                aria-label="添加股票代码"
              />
              <button type="button" disabled={isRunning || !symbolDraft.trim()} onClick={() => addSymbols(symbolDraft)} className="min-h-10 shrink-0 rounded-btn border border-border px-3 text-xs text-secondary hover:bg-elevated disabled:opacity-40">
                添加
              </button>
            </div>
            <div className="mt-3 flex min-h-8 flex-wrap gap-1.5">
              {symbols.length === 0 ? (
                <span className="py-1 text-xs text-muted">未选择股票</span>
              ) : symbols.map(symbol => (
                <span key={symbol} className="inline-flex min-h-7 items-center gap-1 rounded-btn border border-border bg-base pl-2 font-mono text-[10px] text-secondary">
                  {symbol}
                  <button type="button" disabled={isRunning} onClick={() => setSymbols(current => current.filter(item => item !== symbol))} className="flex h-7 w-7 items-center justify-center text-muted hover:text-foreground disabled:opacity-40" aria-label={`移除 ${symbol}`} title="移除">
                    <X className="h-3 w-3" />
                  </button>
                </span>
              ))}
            </div>
          </section>

          <section className="px-4 py-4">
            <h2 className="text-xs font-semibold text-secondary">统一参数</h2>
            <div className="mt-3 grid grid-cols-2 gap-3">
              <label className="text-[11px] text-muted">开始日期<input type="date" value={start} disabled={isRunning} onChange={event => setStart(event.target.value)} className={cn(FIELD_CLASS, 'mt-1.5')} /></label>
              <label className="text-[11px] text-muted">结束日期<input type="date" value={end} disabled={isRunning} onChange={event => setEnd(event.target.value)} className={cn(FIELD_CLASS, 'mt-1.5')} /></label>
              <label className="col-span-2 text-[11px] text-muted">初始资金<input type="number" min="1" value={initialCapital} disabled={isRunning} onChange={event => setInitialCapital(event.target.value)} className={cn(FIELD_CLASS, 'mt-1.5 font-mono')} /></label>
              <label className="text-[11px] text-muted">最大持仓<input type="number" min="1" step="1" value={maxPositions} disabled={isRunning} onChange={event => setMaxPositions(event.target.value)} className={cn(FIELD_CLASS, 'mt-1.5 font-mono')} /></label>
              <label className="text-[11px] text-muted">滑点（bp）<input type="number" min="0" value={slippage} disabled={isRunning} onChange={event => setSlippage(event.target.value)} className={cn(FIELD_CLASS, 'mt-1.5 font-mono')} /></label>
              <label className="text-[11px] text-muted">佣金（万分）<input type="number" min="0" step="0.1" value={commission} disabled={isRunning} onChange={event => setCommission(event.target.value)} className={cn(FIELD_CLASS, 'mt-1.5 font-mono')} /></label>
              <label className="text-[11px] text-muted">印花税（千分）<input type="number" min="0" step="0.1" value={stampTax} disabled={isRunning} onChange={event => setStampTax(event.target.value)} className={cn(FIELD_CLASS, 'mt-1.5 font-mono')} /></label>
            </div>
          </section>
        </aside>

        <section className="min-w-0" aria-label="策略对比结果">
          {queue.length > 0 && (
            <section aria-label="执行队列" className="border-b border-border bg-surface/20 px-3 py-3 sm:px-5">
              <div className="flex flex-wrap gap-2">
                {queue.map(item => {
                  const progress = item.progress && item.progress.total > 0
                    ? Math.min(100, Math.round(item.progress.day / item.progress.total * 100))
                    : null
                  return (
                    <div key={item.strategyId} className="flex min-h-9 min-w-0 flex-1 basis-44 items-center gap-2 rounded-btn border border-border bg-base px-3">
                      {item.status === 'running' ? <Loader2 className="h-3.5 w-3.5 shrink-0 animate-spin text-accent" />
                        : item.status === 'completed' ? <Check className="h-3.5 w-3.5 shrink-0 text-bear" />
                          : item.status === 'error' ? <CircleAlert className="h-3.5 w-3.5 shrink-0 text-danger" />
                            : <Clock3 className="h-3.5 w-3.5 shrink-0 text-muted" />}
                      <span className="min-w-0 flex-1 truncate text-xs text-secondary" title={item.error ?? item.strategyName}>{item.strategyName}</span>
                      <span className={cn('shrink-0 font-mono text-[10px]', statusClass(item.status))}>{progress == null ? statusLabel(item.status) : `${progress}%`}</span>
                    </div>
                  )
                })}
              </div>
            </section>
          )}

          {completedResults.length === 0 ? (
            <div className="flex min-h-[520px] flex-col items-center justify-center px-6 text-center">
              <GitCompareArrows className="h-10 w-10 text-muted" />
              <h2 className="mt-4 text-base font-semibold text-foreground">等待策略对比</h2>
              <p className="mt-2 text-sm text-muted">选择 2–5 个策略和股票池后开始。</p>
            </div>
          ) : (
            <div className="min-w-0">
              <section aria-labelledby="comparison-chart-title" className="border-b border-border px-3 py-4 sm:px-5">
                <div className="mb-2 flex items-center justify-between gap-3">
                  <h2 id="comparison-chart-title" className="text-sm font-semibold text-foreground">归一化净值</h2>
                  <span className="font-mono text-[10px] text-muted">起点 = 0%</span>
                </div>
                <div className="h-[300px] min-w-0 sm:h-[360px]"><EquityComparisonChart results={completedResults} /></div>
              </section>

              <section aria-labelledby="comparison-table-title" className="min-w-0 px-3 py-4 sm:px-5">
                <div className="mb-3 flex items-center justify-between gap-3">
                  <h2 id="comparison-table-title" className="text-sm font-semibold text-foreground">绩效指标</h2>
                  <span className="text-[10px] text-muted">共 {metricRows.length} 个有效结果</span>
                </div>
                <div className="overflow-x-auto rounded-card border border-border">
                  <table className="w-full min-w-[820px] border-collapse text-sm">
                    <thead className="bg-base/70 text-[11px] text-muted">
                      <tr>
                        <th className="px-4 py-2.5 text-left font-medium">策略</th>
                        <th className="px-4 py-2.5 text-right font-medium">总收益</th>
                        <th className="px-4 py-2.5 text-right font-medium">年化收益</th>
                        <th className="px-4 py-2.5 text-right font-medium">夏普</th>
                        <th className="px-4 py-2.5 text-right font-medium">最大回撤</th>
                        <th className="px-4 py-2.5 text-right font-medium">胜率</th>
                        <th className="px-4 py-2.5 text-right font-medium">交易数</th>
                        <th className="px-4 py-2.5 text-right font-medium">耗时</th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-border bg-surface">
                      {metricRows.map(row => <MetricTableRow key={row.strategyId} row={row} />)}
                    </tbody>
                  </table>
                </div>
              </section>
            </div>
          )}
        </section>
      </div>
    </div>
  )
}

function MetricTableRow({ row }: { row: ComparisonMetricRow }) {
  const returnClass = row.totalReturn == null || row.totalReturn === 0
    ? 'text-foreground'
    : row.totalReturn > 0 ? 'text-bull' : 'text-bear'
  return (
    <tr className="hover:bg-elevated/30">
      <td className="px-4 py-3">
        <div className="font-medium text-foreground">{row.strategyName}</div>
        <div className="mt-0.5 font-mono text-[10px] text-muted">{row.strategyId}</div>
      </td>
      <td className={cn('px-4 py-3 text-right font-mono tabular-nums', returnClass)}>{percent(row.totalReturn)}</td>
      <td className="px-4 py-3 text-right font-mono tabular-nums text-foreground">{percent(row.annualReturn)}</td>
      <td className="px-4 py-3 text-right font-mono tabular-nums text-foreground">{number(row.sharpe)}</td>
      <td className="px-4 py-3 text-right font-mono tabular-nums text-bear">{percent(row.maxDrawdown)}</td>
      <td className="px-4 py-3 text-right font-mono tabular-nums text-foreground">{percent(row.winRate, false)}</td>
      <td className="px-4 py-3 text-right font-mono tabular-nums text-foreground">{number(row.tradeCount, 0)}</td>
      <td className="px-4 py-3 text-right font-mono tabular-nums text-muted">{(row.elapsedMs / 1000).toFixed(1)}s</td>
    </tr>
  )
}

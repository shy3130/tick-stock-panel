import { useMemo, useState, type FormEvent } from 'react'
import { useQuery, type UseQueryResult } from '@tanstack/react-query'
import type { EChartsOption } from 'echarts'
import {
  AlertCircle,
  BarChart3,
  Database,
  GitCompareArrows,
  Loader2,
  Network,
  Radar,
  RefreshCw,
  Search,
  ShieldCheck,
  SlidersHorizontal,
  TableProperties,
  TrendingUp,
  type LucideIcon,
} from 'lucide-react'
import { EmptyState } from '@/components/EmptyState'
import { PageHeader } from '@/components/PageHeader'
import { toast } from '@/components/Toast'
import { api, type CrossCorrelationResponse, type CrossPeerResponse, type CrossRelativeStrengthResponse, type CrossReverseScreenResponse } from '@/lib/api'
import { cn } from '@/lib/cn'
import { fmtBigNum, fmtPrice } from '@/lib/format'
import { useECharts } from '@/pages/backtest/charts/useECharts'

const INPUT = 'w-full rounded-btn border border-border bg-base px-2.5 py-1.5 text-xs text-foreground outline-none transition-colors placeholder:text-muted/60 focus:border-accent/50 disabled:cursor-not-allowed disabled:opacity-50'
const SELECT = 'rounded-btn border border-border bg-base px-2.5 py-1.5 text-xs text-foreground outline-none transition-colors focus:border-accent/50 disabled:cursor-not-allowed disabled:opacity-50'
const BTN_PRIMARY = 'inline-flex items-center justify-center gap-1.5 rounded-btn bg-accent px-3 py-1.5 text-xs font-medium text-base transition-colors hover:bg-accent/90 disabled:cursor-not-allowed disabled:opacity-50'
const BTN_GHOST = 'inline-flex items-center justify-center gap-1.5 rounded-btn border border-border bg-elevated px-3 py-1.5 text-xs text-secondary transition-colors hover:text-foreground disabled:cursor-not-allowed disabled:opacity-50'
const CARD = 'rounded-card border border-border bg-surface/70 shadow-[0_1px_2px_hsl(var(--border)/0.35)]'
const TABLE_HEAD = 'border-b border-border bg-elevated/45 text-left text-[10px] font-medium uppercase tracking-wide text-muted'
const NUM_CELL = 'text-right font-mono tabular-nums text-secondary'

type Tab = 'correlation' | 'relativeStrength' | 'peers' | 'reverse'
type PeerMode = 'industry' | 'amount' | 'board' | 'concept'
type PeerSortKey = 'amount' | 'change_pct' | 'turnover_rate' | 'roe' | 'pe' | 'pb' | 'market_cap' | 'score'

const TABS: Array<{ id: Tab; label: string; icon: LucideIcon }> = [
  { id: 'correlation', label: '相关矩阵', icon: Network },
  { id: 'relativeStrength', label: '相对强度', icon: TrendingUp },
  { id: 'peers', label: '同业对比', icon: GitCompareArrows },
  { id: 'reverse', label: '以股找股', icon: Radar },
]

const PEER_MODES: Array<{ value: PeerMode; label: string }> = [
  { value: 'industry', label: '同行业' },
  { value: 'amount', label: '全市场' },
  { value: 'board', label: '同板块' },
  { value: 'concept', label: '同概念' },
]

const PEER_SORTS: Array<{ value: PeerSortKey; label: string }> = [
  { value: 'amount', label: '成交额' },
  { value: 'change_pct', label: '涨跌幅' },
  { value: 'turnover_rate', label: '换手率' },
  { value: 'roe', label: 'ROE' },
  { value: 'pe', label: '市盈率' },
  { value: 'pb', label: '市净率' },
  { value: 'market_cap', label: '市值' },
  { value: 'score', label: '综合分' },
]

function errorMessage(error: unknown): string {
  return error instanceof Error && error.message ? error.message : '请求未完成，请稍后重试。'
}

function numberOf(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null
}

function stringOf(value: unknown): string | null {
  return typeof value === 'string' && value.trim() ? value : null
}

function fmtPoint(value: number | null | undefined, digits = 2, signed = false): string {
  if (value == null || !Number.isFinite(value)) return '—'
  return `${signed && value > 0 ? '+' : ''}${value.toFixed(digits)}%`
}

function priceTone(value: number | null | undefined): string {
  if (value == null || value === 0) return 'text-muted'
  return value > 0 ? 'text-bull' : 'text-bear'
}

function correlationTone(value: number | null): string {
  if (value == null) return 'bg-elevated/70 text-muted'
  if (value >= 0.7) return 'bg-bull/30 text-foreground'
  if (value >= 0.3) return 'bg-bull/15 text-foreground'
  if (value > -0.3) return 'bg-elevated text-secondary'
  if (value > -0.7) return 'bg-bear/15 text-foreground'
  return 'bg-bear/30 text-foreground'
}

/**
 * 本地 enriched 横截面研究工作台。所有查询均需要用户显式提交标的，
 * 只读取本地 DuckDB/enriched 数据，不产生荐股、下单或写入操作。
 */
export function CrossSection() {
  const [symbolInput, setSymbolInput] = useState('')
  const [submittedSymbol, setSubmittedSymbol] = useState('')
  const [requestVersion, setRequestVersion] = useState(0)
  const [tab, setTab] = useState<Tab>('correlation')
  const [peerMode, setPeerMode] = useState<PeerMode>('industry')
  const [peerSortKey, setPeerSortKey] = useState<PeerSortKey>('amount')

  const enabled = Boolean(submittedSymbol)
  const correlation = useQuery({
    queryKey: ['cross-section', 'correlation', submittedSymbol, requestVersion],
    queryFn: () => api.crossCorrelation(submittedSymbol),
    enabled,
  })
  const relativeStrength = useQuery({
    queryKey: ['cross-section', 'relative-strength', submittedSymbol, requestVersion],
    queryFn: () => api.crossRelativeStrength(submittedSymbol),
    enabled,
  })
  const peers = useQuery({
    queryKey: ['cross-section', 'peers', submittedSymbol, peerMode, peerSortKey, requestVersion],
    queryFn: () => api.crossPeerComparison(submittedSymbol, peerMode, peerSortKey),
    enabled,
  })
  const reverseScreen = useQuery({
    queryKey: ['cross-section', 'reverse-screen', submittedSymbol, requestVersion],
    queryFn: () => api.crossReverseScreen(submittedSymbol),
    enabled,
  })

  const handleSubmit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    const symbol = symbolInput.trim().toUpperCase()
    if (!symbol) {
      toast('请输入标的代码后再查询。', 'error')
      return
    }
    setSubmittedSymbol(symbol)
    setRequestVersion(value => value + 1)
  }

  const isFetching = correlation.isFetching || relativeStrength.isFetching || peers.isFetching || reverseScreen.isFetching

  return (
    <div className="flex h-full min-h-0 flex-col">
      <PageHeader
        title="横截面研究"
        subtitle="本地 enriched 数据的只读比较研究"
        right={isFetching ? <span className="inline-flex items-center gap-1.5 text-xs text-muted" role="status"><Loader2 className="h-3.5 w-3.5 animate-spin" />读取中</span> : undefined}
      />
      <main className="min-h-0 flex-1 overflow-auto px-5 py-4">
        <div className="mx-auto flex max-w-7xl flex-col gap-4">
          <section className="rounded-card border border-accent/25 bg-accent/5 px-3 py-2.5 text-xs leading-relaxed text-secondary">
            <div className="flex items-start gap-2">
              <ShieldCheck className="mt-0.5 h-4 w-4 shrink-0 text-accent" />
              <div>
                <p className="font-medium text-foreground">研究边界：仅本地 enriched 横截面研究</p>
                <p className="mt-0.5">页面仅读取本地 DuckDB/enriched 快照；分析结果用于复核数据关系，不构成荐股、买卖建议或下单指令。</p>
              </div>
            </div>
          </section>

          <form onSubmit={handleSubmit} className={cn(CARD, 'flex flex-col gap-3 p-3 sm:flex-row sm:items-end')}>
            <label className="min-w-0 flex-1">
              <span className="mb-1.5 block text-xs font-medium text-secondary">标的代码</span>
              <input
                className={INPUT}
                value={symbolInput}
                onChange={event => setSymbolInput(event.target.value)}
                placeholder="例如 000001.SZ"
                aria-label="标的代码"
                autoCapitalize="characters"
              />
            </label>
            <button type="submit" className={BTN_PRIMARY} disabled={!symbolInput.trim()}>
              <Search className="h-3.5 w-3.5" />明确查询
            </button>
          </form>

          <nav className="flex gap-1 overflow-x-auto border-b border-border pb-2" aria-label="横截面分析区域">
            {TABS.map(({ id, label, icon: Icon }) => (
              <button
                key={id}
                type="button"
                aria-current={tab === id ? 'page' : undefined}
                onClick={() => setTab(id)}
                className={cn(
                  'inline-flex shrink-0 items-center gap-1.5 rounded-btn px-3 py-1.5 text-xs font-medium transition-colors',
                  tab === id ? 'bg-accent/15 text-accent' : 'text-secondary hover:bg-elevated/60 hover:text-foreground',
                )}
              >
                <Icon className="h-3.5 w-3.5" />{label}
              </button>
            ))}
          </nav>

          {!enabled ? (
            <EmptyState icon={BarChart3} title="输入标的代码以开始研究" hint="点击“明确查询”后，才会读取本地横截面数据并展示四类分析。" />
          ) : (
            <>
              {tab === 'correlation' && <CorrelationPanel query={correlation} />}
              {tab === 'relativeStrength' && <RelativeStrengthPanel query={relativeStrength} />}
              {tab === 'peers' && (
                <PeerComparisonPanel
                  query={peers}
                  mode={peerMode}
                  sortKey={peerSortKey}
                  onModeChange={setPeerMode}
                  onSortKeyChange={setPeerSortKey}
                  onRun={() => setRequestVersion(value => value + 1)}
                />
              )}
              {tab === 'reverse' && <ReverseScreenPanel query={reverseScreen} />}
            </>
          )}
        </div>
      </main>
    </div>
  )
}

function CorrelationPanel({ query }: { query: UseQueryResult<CrossCorrelationResponse, Error> }) {
  if (query.isLoading) return <LoadingState label="正在读取相关性矩阵…" />
  if (query.isError) return <QueryError message={errorMessage(query.error)} onRetry={() => void query.refetch()} />
  const data = query.data
  if (!data) return null

  const cells = data.matrix.correlation
  const hasMatrix = data.matrix.instruments.length > 1 && cells.some(row => row.some(value => value != null))

  return (
    <section className="space-y-4">
      <div className="grid grid-cols-1 gap-3 lg:grid-cols-3">
        <Metric label="行业范围" value={data.industry ?? '—'} />
        <Metric label="平均相关系数" value={data.averageCorrelation == null ? '—' : data.averageCorrelation.toFixed(3)} />
        <Metric label="共同交易日" value={data.alignedDays > 0 ? `${data.alignedDays} / 至少 ${data.minSamples}` : '数据不足'} />
      </div>
      <section className={cn(CARD, 'overflow-hidden')}>
        <SectionHeading icon={Network} title="收益率相关矩阵" hint={`最近 ${data.window} 个共同收益日；空值表示样本不足或零方差。`} />
        {hasMatrix ? (
          <div className="overflow-x-auto">
            <table className="min-w-full border-collapse text-xs">
              <thead className={TABLE_HEAD}>
                <tr>
                  <th className="sticky left-0 z-10 bg-elevated/95 px-3 py-2 text-left font-medium">标的</th>
                  {data.matrix.instruments.map(symbol => <th key={symbol} className="min-w-20 px-3 py-2 text-right font-medium">{symbol}</th>)}
                </tr>
              </thead>
              <tbody>
                {data.matrix.instruments.map((symbol, rowIndex) => (
                  <tr key={symbol} className="border-b border-border/60 last:border-0">
                    <th scope="row" className="sticky left-0 z-10 bg-surface px-3 py-2 text-left font-mono font-medium text-foreground">{symbol}</th>
                    {data.matrix.instruments.map((columnSymbol, columnIndex) => {
                      const value = cells[rowIndex]?.[columnIndex] ?? null
                      const sample = data.matrix.samples[rowIndex]?.[columnIndex] ?? null
                      return (
                        <td key={columnSymbol} className="px-1.5 py-1.5 text-right">
                          <span title={sample == null ? '样本不足' : `共同样本 ${sample} 日`} className={cn('inline-flex min-w-14 justify-end rounded px-2 py-1 font-mono tabular-nums', correlationTone(value))}>
                            {value == null ? '—' : value.toFixed(3)}
                          </span>
                        </td>
                      )
                    })}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <EmptyState icon={Network} title="相关性数据不足" hint="需要至少两只可比标的，以及满足最小共同交易日的收益率序列；空值不会被替换为结论。" />
        )}
      </section>
      <section className={cn(CARD, 'overflow-hidden')}>
        <SectionHeading icon={TableProperties} title="标的对统计" hint="Beta、协方差和前窗口变化均仅在有效共同样本足够时提供。" />
        {data.pairRows.length > 0 ? (
          <div className="overflow-x-auto">
            <table className="min-w-[720px] w-full text-xs">
              <thead className={TABLE_HEAD}><tr><th className="px-3 py-2">同业标的</th><th className="px-3 py-2 text-right">相关系数</th><th className="px-3 py-2 text-right">前窗口</th><th className="px-3 py-2 text-right">变化</th><th className="px-3 py-2 text-right">Beta</th><th className="px-3 py-2 text-right">共同样本</th></tr></thead>
              <tbody>{data.pairRows.map(row => <tr key={row.peer} className="border-b border-border/60 last:border-0"><td className="px-3 py-2 font-mono text-foreground">{row.peer}</td><td className={cn('px-3 py-2 font-mono text-right tabular-nums', priceTone(row.correlation))}>{row.correlation?.toFixed(3) ?? '—'}</td><td className={NUM_CELL}>{row.previousCorrelation?.toFixed(3) ?? '—'}</td><td className={cn('px-3 py-2 text-right font-mono tabular-nums', priceTone(row.correlationDelta))}>{row.correlationDelta == null ? '—' : `${row.correlationDelta > 0 ? '+' : ''}${row.correlationDelta.toFixed(3)}`}</td><td className={NUM_CELL}>{row.beta?.toFixed(3) ?? '—'}</td><td className={NUM_CELL}>{row.samples ?? '—'}</td></tr>)}</tbody>
            </table>
          </div>
        ) : <EmptyState icon={TableProperties} title="暂无可比较标的" hint="本地数据未形成有效标的对时，页面仅显示空态。" />}
      </section>
      <DataBoundary notes={data.boundaryNotes} />
    </section>
  )
}

function RelativeStrengthPanel({ query }: { query: UseQueryResult<CrossRelativeStrengthResponse, Error> }) {
  if (query.isLoading) return <LoadingState label="正在读取相对强度…" />
  if (query.isError) return <QueryError message={errorMessage(query.error)} onRetry={() => void query.refetch()} />
  const data = query.data
  if (!data) return null

  return (
    <section className="space-y-4">
      <section className={cn(CARD, 'p-4')}>
        <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
          <div><p className="text-xs text-muted">相对强度汇总</p><p className="mt-1 text-base font-semibold text-foreground">{data.summary.label}</p><p className="mt-1 text-xs text-secondary">{data.summary.detail}</p></div>
          <span className={cn('w-fit rounded-btn px-2 py-1 text-xs font-medium', data.summary.tone === 'bull' ? 'bg-bull/15 text-bull' : data.summary.tone === 'risk' ? 'bg-bear/15 text-bear' : 'bg-elevated text-secondary')}>{data.summary.latestDate ?? '日期未知'}</span>
        </div>
        {data.summary.dataLimitations.length > 0 && <p className="mt-3 border-t border-border pt-2 text-xs text-muted">数据限制：{data.summary.dataLimitations.join('；')}</p>}
      </section>
      <RelativeStrengthChart benchmarks={data.benchmarks} />
      <section className={cn(CARD, 'overflow-hidden')}>
        <SectionHeading icon={TableProperties} title="窗口回报比较" hint="窗口回报按对齐后的交易日计算；缺失值不作补齐。" />
        {data.windows.length > 0 ? <div className="overflow-x-auto"><table className="min-w-[680px] w-full text-xs"><thead className={TABLE_HEAD}><tr><th className="px-3 py-2">窗口</th><th className="px-3 py-2 text-right">标的回报</th>{data.benchmarks.map(item => <th key={item.key} className="px-3 py-2 text-right">相对 {item.label}</th>)}</tr></thead><tbody>{data.windows.map(window => <tr key={window.days} className="border-b border-border/60 last:border-0"><td className="px-3 py-2 font-medium text-foreground">{window.label}</td><td className={cn('px-3 py-2 text-right font-mono tabular-nums', priceTone(window.stockReturnPct))}>{fmtPoint(window.stockReturnPct, 2, true)}</td>{data.benchmarks.map(benchmark => { const value = window.benchmarks.find(item => item.key === benchmark.key)?.relativeReturnPct ?? null; return <td key={benchmark.key} className={cn('px-3 py-2 text-right font-mono tabular-nums', priceTone(value))}>{fmtPoint(value, 2, true)}</td> })}</tr>)}</tbody></table></div> : <EmptyState icon={TrendingUp} title="相对强度数据不足" hint="需要标的与基准同时具备可对齐的本地日线数据。" />}
      </section>
      <DataBoundary notes={data.boundaryNotes} />
    </section>
  )
}

function RelativeStrengthChart({ benchmarks }: { benchmarks: CrossRelativeStrengthResponse['benchmarks'] }) {
  const primary = benchmarks[0]
  const option = useMemo<EChartsOption | null>(() => {
    if (!primary || primary.points.length === 0) return null
    return {
      backgroundColor: 'transparent',
      tooltip: { trigger: 'axis', backgroundColor: 'rgba(24,24,27,0.92)', borderColor: 'rgba(82,82,91,0.6)', textStyle: { color: '#a1a1aa' } },
      legend: { top: 0, data: ['标的净值', `${primary.label}净值`], textStyle: { color: '#a1a1aa', fontSize: 10 } },
      grid: { left: 48, right: 20, top: 36, bottom: 32 },
      xAxis: { type: 'category', boundaryGap: false, data: primary.points.map(point => point.date), axisLabel: { color: '#a1a1aa', fontSize: 10, formatter: (value: string) => value.slice(5) }, axisLine: { lineStyle: { color: 'rgba(160,160,170,0.18)' } } },
      yAxis: { type: 'value', scale: true, axisLabel: { color: '#a1a1aa', fontSize: 10 }, splitLine: { lineStyle: { color: 'rgba(160,160,170,0.18)' } } },
      series: [
        { name: '标的净值', type: 'line', data: primary.points.map(point => point.stockNav), symbol: 'none', lineStyle: { width: 1.75, color: '#e4e4e7' } },
        { name: `${primary.label}净值`, type: 'line', data: primary.points.map(point => point.benchmarkNav), symbol: 'none', lineStyle: { width: 1.5, color: '#60a5fa', type: 'dashed' } },
      ],
    }
  }, [primary])
  const chartRef = useECharts(option, [option])

  return <section className={cn(CARD, 'p-3')}><SectionHeading icon={TrendingUp} title="归一净值曲线" hint={primary ? `基准：${primary.label}；最新相对变化 ${fmtPoint(primary.latestRelativePct, 2, true)}。` : '无可用基准曲线。'} />{option ? <div ref={chartRef} className="h-72 w-full" aria-label="标的和基准的归一净值曲线" /> : <EmptyState icon={TrendingUp} title="暂无可绘制曲线" hint="本地数据不足时不绘制推断曲线。" />}</section>
}

function PeerComparisonPanel({ query, mode, sortKey, onModeChange, onSortKeyChange, onRun }: { query: UseQueryResult<CrossPeerResponse, Error>; mode: PeerMode; sortKey: PeerSortKey; onModeChange: (value: PeerMode) => void; onSortKeyChange: (value: PeerSortKey) => void; onRun: () => void }) {
  if (query.isLoading) return <LoadingState label="正在读取同业对比…" />
  if (query.isError) return <QueryError message={errorMessage(query.error)} onRetry={() => void query.refetch()} />
  const data = query.data
  if (!data) return null

  return <section className="space-y-4"><section className={cn(CARD, 'flex flex-col gap-3 p-3 sm:flex-row sm:items-end')}><label className="flex-1"><span className="mb-1.5 block text-xs text-secondary">比较范围</span><select className={cn(SELECT, 'w-full')} value={mode} onChange={event => onModeChange(event.target.value as PeerMode)}>{PEER_MODES.map(item => <option key={item.value} value={item.value}>{item.label}</option>)}</select></label><label className="flex-1"><span className="mb-1.5 block text-xs text-secondary">排序指标</span><select className={cn(SELECT, 'w-full')} value={sortKey} onChange={event => onSortKeyChange(event.target.value as PeerSortKey)}>{PEER_SORTS.map(item => <option key={item.value} value={item.value}>{item.label}</option>)}</select></label><button type="button" className={BTN_PRIMARY} onClick={onRun}><SlidersHorizontal className="h-3.5 w-3.5" />应用条件</button></section><div className="grid grid-cols-1 gap-3 sm:grid-cols-3"><Metric label="比较范围" value={data.universe ?? '数据不足'} /><Metric label="当前排名" value={data.summary.currentRank == null ? '—' : `${data.summary.currentRank} / ${data.summary.currentTotal}`} /><Metric label="显示标的" value={`${data.summary.displayed} / ${data.summary.total}`} /></div><section className={cn(CARD, 'overflow-hidden')}><SectionHeading icon={GitCompareArrows} title="排名表" hint="当前标的固定在首行；指标缺失保持为空，不使用估算值。" />{data.rows.length > 0 ? <PeerTable rows={data.rows} /> : <EmptyState icon={GitCompareArrows} title="当前范围没有可比标的" hint="可能缺少行业、板块或概念归属；请调整范围后明确应用条件。" />}</section><DataBoundary notes={data.boundaryNotes} /></section>
}

function PeerTable({ rows }: { rows: CrossPeerResponse['rows'] }) {
  return <div className="overflow-x-auto"><table className="min-w-[940px] w-full text-xs"><thead className={TABLE_HEAD}><tr><th className="px-3 py-2">排名</th><th className="px-3 py-2">标的</th><th className="px-3 py-2 text-right">收盘</th><th className="px-3 py-2 text-right">涨跌幅</th><th className="px-3 py-2 text-right">成交额</th><th className="px-3 py-2 text-right">换手率</th><th className="px-3 py-2 text-right">ROE</th><th className="px-3 py-2 text-right">PE / PB</th></tr></thead><tbody>{rows.map((row, index) => { const rank = numberOf(row.rank); const change = numberOf(row.change_pct); const amount = numberOf(row.amount_yi); const turnover = numberOf(row.turnover_rate); const roe = numberOf(row.roe); const pe = numberOf(row.pe); const pb = numberOf(row.pb); return <tr key={`${stringOf(row.symbol) ?? 'unknown'}-${index}`} className={cn('border-b border-border/60 last:border-0', row.isCurrent ? 'bg-accent/5' : '')}><td className={NUM_CELL}>{rank ?? '—'}</td><td className="px-3 py-2"><p className="font-mono text-foreground">{stringOf(row.symbol) ?? '—'}</p>{stringOf(row.name) && <p className="mt-0.5 text-[10px] text-muted">{stringOf(row.name)}</p>}</td><td className={NUM_CELL}>{fmtPrice(numberOf(row.close))}</td><td className={cn('px-3 py-2 text-right font-mono tabular-nums', priceTone(change))}>{fmtPoint(change, 2, true)}</td><td className={NUM_CELL}>{amount == null ? '—' : `${amount.toFixed(2)}亿`}</td><td className={NUM_CELL}>{fmtPoint(turnover)}</td><td className={NUM_CELL}>{fmtPoint(roe)}</td><td className={NUM_CELL}>{pe == null ? '—' : pe.toFixed(2)} / {pb == null ? '—' : pb.toFixed(2)}</td></tr> })}</tbody></table></div>
}

function ReverseScreenPanel({ query }: { query: UseQueryResult<CrossReverseScreenResponse, Error> }) {
  if (query.isLoading) return <LoadingState label="正在读取以股找股结果…" />
  if (query.isError) return <QueryError message={errorMessage(query.error)} onRetry={() => void query.refetch()} />
  const data = query.data
  if (!data) return null
  const rows = data.result?.rows ?? []
  const conditions = data.request?.conditions ?? []
  const orderBy = data.request?.order_by

  return <section className="space-y-4"><section className={cn(CARD, 'p-4')}><SectionHeading icon={Radar} title="以股找股条件" hint="条件由标的现有特征生成，保留原始范围，不自动扩展或补全。" /><div className="mt-3 flex flex-wrap gap-2">{conditions.length > 0 ? conditions.map((condition, index) => <span key={index} className="rounded-btn border border-border bg-base px-2 py-1 font-mono text-[11px] text-secondary">{formatCondition(condition)}</span>) : <span className="text-xs text-muted">没有可生成的筛选条件。</span>}</div>{orderBy && <p className="mt-3 text-xs text-muted">原始排序：{orderBy.field ?? '—'} / {orderBy.direction ?? '—'}；上限 {data.request?.limit ?? '—'} 条。</p>}</section><section className={cn(CARD, 'p-4')}><SectionHeading icon={ShieldCheck} title="生成原因" hint="这些原因解释条件来源，不代表任何交易判断。" />{data.reasons.length > 0 ? <ul className="mt-3 space-y-2 text-xs leading-relaxed text-secondary">{data.reasons.map((reason, index) => <li key={`${reason}-${index}`} className="flex gap-2"><span className="mt-1 h-1.5 w-1.5 shrink-0 rounded-full bg-accent" />{reason}</li>)}</ul> : <p className="mt-3 text-xs text-muted">本地特征不足，未产生原因或筛选范围。</p>}</section><section className={cn(CARD, 'overflow-hidden')}><SectionHeading icon={TableProperties} title="本地候选结果" hint={`结果仅为研究排序候选，共 ${data.result?.total ?? 0} 条；不构成投资推荐。`} />{rows.length > 0 ? <ReverseRowsTable rows={rows} /> : <EmptyState icon={Search} title="没有本地候选结果" hint="可能是本地横截面数据、标的特征或筛选结果不足；页面不会用替代标的填充。" />}</section><DataBoundary notes={data.boundaryNotes} /></section>
}

function ReverseRowsTable({ rows }: { rows: Array<Record<string, unknown>> }) {
  const columns = ['symbol', 'name', 'close', 'change_pct', 'amount', 'turnover_rate']
  const labels: Record<string, string> = { symbol: '标的', name: '名称', close: '收盘', change_pct: '涨跌幅', amount: '成交额', turnover_rate: '换手率' }
  return <div className="overflow-x-auto"><table className="min-w-[760px] w-full text-xs"><thead className={TABLE_HEAD}><tr>{columns.map(column => <th key={column} className={cn('px-3 py-2', column === 'symbol' || column === 'name' ? 'text-left' : 'text-right')}>{labels[column]}</th>)}</tr></thead><tbody>{rows.map((row, index) => { const change = numberOf(row.change_pct); return <tr key={`${stringOf(row.symbol) ?? 'row'}-${index}`} className="border-b border-border/60 last:border-0">{columns.map(column => { const value = row[column]; if (column === 'change_pct') return <td key={column} className={cn('px-3 py-2 text-right font-mono tabular-nums', priceTone(change))}>{fmtPoint(change, 2, true)}</td>; if (column === 'amount') return <td key={column} className={NUM_CELL}>{fmtBigNum(numberOf(value))}</td>; if (column === 'turnover_rate') return <td key={column} className={NUM_CELL}>{fmtPoint(numberOf(value))}</td>; if (column === 'close') return <td key={column} className={NUM_CELL}>{fmtPrice(numberOf(value))}</td>; return <td key={column} className={cn('px-3 py-2', column === 'symbol' ? 'font-mono text-foreground' : 'text-secondary')}>{stringOf(value) ?? '—'}</td> })}</tr> })}</tbody></table></div>
}

function formatCondition(condition: Record<string, unknown>): string {
  const field = stringOf(condition.field) ?? '字段未知'
  const operator = stringOf(condition.operator) ?? stringOf(condition.op) ?? '—'
  const value = condition.value
  return `${field} ${operator} ${typeof value === 'number' ? value.toFixed(2) : stringOf(value) ?? '—'}`
}

function SectionHeading({ icon: Icon, title, hint }: { icon: LucideIcon; title: string; hint?: string }) {
  return <div className="flex flex-col gap-1 px-4 pt-3 sm:flex-row sm:items-center sm:justify-between"><div className="flex items-center gap-2"><Icon className="h-4 w-4 text-accent" /><h2 className="text-sm font-semibold text-foreground">{title}</h2></div>{hint && <p className="text-xs text-muted">{hint}</p>}</div>
}

function Metric({ label, value }: { label: string; value: string }) {
  return <div className={cn(CARD, 'px-3 py-2.5')}><p className="text-[11px] text-muted">{label}</p><p className="mt-1 truncate text-sm font-semibold tabular-nums text-foreground">{value}</p></div>
}

function DataBoundary({ notes }: { notes: string[] }) {
  return <section className="rounded-card border border-border bg-base/45 px-3 py-2.5"><div className="flex items-start gap-2"><Database className="mt-0.5 h-4 w-4 shrink-0 text-muted" /><div><p className="text-xs font-medium text-secondary">数据来源与边界（Provenance）</p><p className="mt-0.5 text-xs leading-relaxed text-muted">来源：本地 DuckDB/enriched 横截面及其本地财务快照；无外部拉取、无写入。</p>{notes.length > 0 && <ul className="mt-2 space-y-1 text-xs leading-relaxed text-muted">{notes.map((note, index) => <li key={`${note}-${index}`}>• {note}</li>)}</ul>}</div></div></section>
}

function LoadingState({ label }: { label: string }) {
  return <div className="flex min-h-56 items-center justify-center gap-2 text-xs text-muted" role="status"><Loader2 className="h-4 w-4 animate-spin" />{label}</div>
}

function QueryError({ message, onRetry }: { message: string; onRetry: () => void }) {
  return <section className="rounded-card border border-danger/40 bg-danger/5 p-5 text-center" role="alert"><AlertCircle className="mx-auto h-5 w-5 text-danger" /><p className="mt-2 text-sm font-medium text-foreground">研究数据读取失败</p><p className="mt-1 break-words text-xs text-danger">{message}</p><button type="button" onClick={onRetry} className={cn(BTN_GHOST, 'mt-3')}><RefreshCw className="h-3.5 w-3.5" />重试</button></section>
}

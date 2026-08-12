import { useState, type FormEvent, type ReactNode } from 'react'
import { useQuery, type UseQueryResult } from '@tanstack/react-query'
import {
  AlertCircle,
  BarChart3,
  CandlestickChart,
  CircleAlert,
  Database,
  FileBarChart2,
  ListOrdered,
  Loader2,
  RefreshCw,
  Search,
} from 'lucide-react'
import { EmptyState } from '@/components/EmptyState'
import {
  api,
  type MarketDataCallAuctionResponse,
  type MarketDataCallAuctionSession,
  type MarketDataCapabilities,
  type MarketDataCapability,
  type MarketDataCapabilityKey,
  type MarketDataChipRow,
  type MarketDataFrequency,
  type MarketDataMoneyflowBlockRow,
  type MarketDataMoneyflowStockRow,
  type MarketDataResponse,
  type MarketDataTransactionsResponse,
} from '@/lib/api'
import { cn } from '@/lib/cn'
import { fmtBigNum, fmtPrice, fmtVolume } from '@/lib/format'
import { QK } from '@/lib/queryKeys'

const INPUT = 'control w-full text-xs'
const BTN_PRIMARY = 'btn-primary text-xs'
const BTN_GHOST = 'btn-secondary text-xs'
const SYMBOL_PATTERN = /^\d{6}\.(SH|SZ|BJ)$/
const MAX_RANGE_DAYS = 366
const CHIP_LIMIT = 500
const BLOCK_LIMIT = 100
const TICK_LIMIT = 5_000
const CN_DATE_FORMATTER = new Intl.DateTimeFormat('en-CA', {
  timeZone: 'Asia/Shanghai',
  year: 'numeric',
  month: '2-digit',
  day: '2-digit',
})
const TODAY = CN_DATE_FORMATTER.format(new Date())

type DateRange = { start: string; end: string }
type CapabilityState = 'available' | 'empty' | 'unavailable'

const CAPABILITY_STATE_META: Record<CapabilityState, { label: string; className: string }> = {
  available: { label: '可用', className: 'bg-success/10 text-success' },
  empty: { label: '已发布，无数据', className: 'bg-warning/10 text-warning' },
  unavailable: { label: '不可用', className: 'bg-danger/10 text-danger' },
}

function isoDaysAgo(days: number): string {
  return CN_DATE_FORMATTER.format(new Date(Date.now() - days * 86_400_000))
}

function formatNumber(value: number | null | undefined, digits = 3): string {
  if (value == null || !Number.isFinite(value)) return '—'
  return value.toLocaleString('zh-CN', { maximumFractionDigits: digits })
}

function formatDate(value: string | null | undefined): string {
  return value || '—'
}


function flowTone(value: number | null | undefined): string {
  if (value == null || value === 0) return 'text-muted'
  return value > 0 ? 'text-success' : 'text-danger'
}

function dateRangeError(range: DateRange): string | null {
  if (!range.start || !range.end) return '请选择起止日期。'
  const start = new Date(`${range.start}T00:00:00`).getTime()
  const end = new Date(`${range.end}T00:00:00`).getTime()
  if (!Number.isFinite(start) || !Number.isFinite(end) || start > end) return '起始日期不能晚于结束日期。'
  if ((end - start) / 86_400_000 + 1 > MAX_RANGE_DAYS) return `单次最多查询 ${MAX_RANGE_DAYS} 天，避免无边界扫描。`
  return null
}

function normalizedSymbol(symbol: string): string | null {
  const value = symbol.trim().toUpperCase()
  return SYMBOL_PATTERN.test(value) ? value : null
}

function capabilityState(capability: MarketDataCapability | undefined): CapabilityState {
  if (!capability?.available) return 'unavailable'
  return capability.rows === 0 ? 'empty' : 'available'
}


function latestChip(rows: MarketDataChipRow[]): MarketDataChipRow | null {
  return rows.reduce<MarketDataChipRow | null>((latest, row) => {
    if (!latest || (row.trade_date ?? '') > (latest.trade_date ?? '')) return row
    return latest
  }, null)
}

function ResultFrame<Response extends MarketDataResponse<unknown>>({
  query,
  idleText,
  emptyTitle,
  emptyHint,
  children,
}: {
  query: UseQueryResult<Response, Error>
  idleText: string
  emptyTitle: string
  emptyHint: string
  children: (data: Response) => ReactNode
}) {
  if (!query.data && query.fetchStatus === 'idle' && !query.isError) {
    return <p className="px-3 py-8 text-center text-xs text-muted">{idleText}</p>
  }
  if (query.isFetching) {
    return <div className="flex items-center justify-center gap-2 px-3 py-9 text-xs text-muted" role="status"><Loader2 className="h-4 w-4 animate-spin" />正在读取已发布快照</div>
  }
  if (query.isError) {
    const message = query.error instanceof Error ? query.error.message : '请求未完成，请稍后重试。'
    return (
      <div className="px-3 py-5" role="alert">
        <p className="flex items-start gap-1.5 text-xs leading-relaxed text-danger"><AlertCircle className="mt-0.5 h-3.5 w-3.5 shrink-0" />{message}</p>
        <button type="button" onClick={() => void query.refetch()} className={cn(BTN_GHOST, 'mt-3 px-2 py-1')}><RefreshCw className="h-3 w-3" />重试</button>
      </div>
    )
  }
  const data = query.data
  if (!data) return null
  if (!data.available) {
    return <UnavailableResult source={data.source} reason={data.reason} />
  }
  if (data.rows.length === 0) {
    return <div className="py-2"><EmptyState icon={Database} title={emptyTitle} hint={emptyHint} /></div>
  }
  return <>{children(data)}</>
}

function UnavailableResult({ source, reason }: { source: string | null; reason?: string | null }) {
  return (
    <div className="px-3 py-5" role="alert">
      <p className="flex items-start gap-1.5 text-xs font-medium leading-relaxed text-danger"><CircleAlert className="mt-0.5 h-3.5 w-3.5 shrink-0" />数据能力当前不可用</p>
      <p className="mt-1 text-xs leading-relaxed text-secondary">{reason || '上游发布快照未就绪；这不是“无数据”。'}</p>
      {source && <p className="mt-2 font-mono text-[10px] text-muted">来源：{source}</p>}
    </div>
  )
}

function ResultProvenance({ data }: { data: MarketDataResponse<unknown> }) {
  return (
    <p className="border-b border-border/60 px-3 py-2 text-[10px] text-muted">
      来源：<span className="font-mono text-secondary">{data.source || '未声明'}</span> · 返回 {data.rows.length} 行 · 仅读取上游已发布快照
    </p>
  )
}

function CapabilityStrip() {
  const statusQuery = useQuery({
    queryKey: QK.marketDataStatus,
    queryFn: api.marketDataStatus,
    staleTime: 60_000,
  })
  const capabilities = statusQuery.data?.capabilities
  const groups: { title: string; keys: MarketDataCapabilityKey[]; note?: string }[] = [
    { title: '筹码', keys: ['chip'] },
    { title: '个股资金流', keys: ['moneyflow_daily_stock', 'moneyflow_minute_stock'] },
    { title: '板块资金流', keys: ['moneyflow_daily_block', 'moneyflow_minute_block'] },
    { title: '集合竞价', keys: ['call_auction'] },
    { title: '逐笔成交', keys: ['transactions'] },
    { title: '港股复权', keys: ['hk_adjustment'], note: '能力边界' },
    { title: '港股财务', keys: ['hk_financial'], note: '能力边界' },
  ]
  const statusError = statusQuery.error instanceof Error ? statusQuery.error.message : '请求未完成，请稍后重试。'

  return (
    <section className="panel" aria-labelledby="market-data-capabilities-title">
      <div className="panel-header">
        <div className="min-w-0">
          <h2 id="market-data-capabilities-title" className="text-sm font-semibold text-foreground">市场数据能力</h2>
          <p className="mt-0.5 text-[10px] text-muted">状态来自 provider 发布快照；不可用与无数据严格区分。</p>
        </div>
        <button type="button" onClick={() => void statusQuery.refetch()} disabled={statusQuery.isFetching} className={cn(BTN_GHOST, 'px-2 py-1')}>
          {statusQuery.isFetching ? <Loader2 className="h-3 w-3 animate-spin" /> : <RefreshCw className="h-3 w-3" />}刷新状态
        </button>
      </div>
      {statusQuery.isPending ? (
        <div className="flex items-center gap-2 px-3 py-5 text-xs text-muted" role="status"><Loader2 className="h-3.5 w-3.5 animate-spin" />正在读取能力状态</div>
      ) : statusQuery.isError ? (
        <div className="px-3 py-4" role="alert"><p className="flex gap-1.5 text-xs text-danger"><AlertCircle className="h-3.5 w-3.5 shrink-0" />能力状态读取失败：{statusError}</p></div>
      ) : (
        <div className="grid grid-cols-1 divide-y divide-border/60 sm:grid-cols-2 sm:divide-x sm:divide-y-0 lg:grid-cols-4">
          {groups.map((group) => <CapabilityCell key={group.title} group={group} capabilities={capabilities} />)}
        </div>
      )}
    </section>
  )
}

function CapabilityCell({
  group,
  capabilities,
}: {
  group: { title: string; keys: MarketDataCapabilityKey[]; note?: string }
  capabilities: Partial<MarketDataCapabilities> | undefined
}) {
  const unavailableCapability = group.keys
    .map((key) => capabilities?.[key])
    .find((item) => !item?.available)
  const coverage = group.keys
    .map((key) => {
      const item = capabilities?.[key]
      return item?.earliest_date || item?.latest_date
        ? `${item.earliest_date || '—'} 至 ${item.latest_date || '—'}`
        : null
    })
    .filter(Boolean)

  return (
    <div className="min-w-0 px-3 py-2.5">
      <div className="flex items-center gap-1.5">
        <p className="text-xs font-medium text-foreground">{group.title}</p>
        {group.note && <span className="rounded bg-muted/15 px-1 py-0.5 text-[9px] text-muted">{group.note}</span>}
      </div>
      <div className="mt-1.5 space-y-1">
        {group.keys.map((key) => {
          const item = capabilities?.[key]
          const state = capabilityState(item)
          const label = key.includes('_daily_') ? '日级' : key.includes('_minute_') ? '分钟' : null
          return (
            <div key={key} className="flex min-w-0 items-center justify-between gap-2">
              <span className="truncate text-[10px] text-muted">{label || item?.source || '发布状态'}</span>
              <span className={cn('shrink-0 rounded px-1.5 py-0.5 text-[9px] font-medium', CAPABILITY_STATE_META[state].className)}>{CAPABILITY_STATE_META[state].label}</span>
            </div>
          )
        })}
      </div>
      {unavailableCapability?.reason ? (
        <p className="mt-1.5 line-clamp-2 text-[10px] leading-relaxed text-secondary">{unavailableCapability.reason}</p>
      ) : (
        <p className="mt-1.5 truncate font-mono text-[10px] text-muted">{coverage.join(' · ') || '覆盖范围未声明'}</p>
      )}
    </div>
  )
}

export function MarketDataPanel() {
  const [symbol, setSymbol] = useState('600519.SH')
  const [range, setRange] = useState<DateRange>({ start: isoDaysAgo(90), end: TODAY })
  const [date, setDate] = useState(TODAY)
  const [freq, setFreq] = useState<MarketDataFrequency>('daily')
  const [session, setSession] = useState<MarketDataCallAuctionSession | ''>('open')
  const [blockType, setBlockType] = useState<string>('')
  const [inputError, setInputError] = useState<string | null>(null)
  const [chipRequest, setChipRequest] = useState<{ symbol: string; range: DateRange } | null>(null)
  const [stockFlowRequest, setStockFlowRequest] = useState<{ symbol: string; range: DateRange; freq: MarketDataFrequency } | null>(null)
  const [blockFlowRequest, setBlockFlowRequest] = useState<{ date: string; freq: MarketDataFrequency; blockType?: number } | null>(null)
  const [auctionRequest, setAuctionRequest] = useState<{ symbol: string; date: string; session?: MarketDataCallAuctionSession } | null>(null)
  const [transactionsRequest, setTransactionsRequest] = useState<{ symbol: string; date: string } | null>(null)

  const chipQuery = useQuery({
    queryKey: QK.marketDataChip(chipRequest?.symbol ?? '', chipRequest?.range.start ?? '', chipRequest?.range.end ?? '', CHIP_LIMIT),
    queryFn: () => api.marketDataChip(chipRequest!.symbol, { ...chipRequest!.range, limit: CHIP_LIMIT }),
    enabled: chipRequest !== null,
    retry: false,
  })
  const stockFlowQuery = useQuery({
    queryKey: QK.marketDataMoneyflowStock(stockFlowRequest?.symbol ?? '', stockFlowRequest?.freq ?? 'daily', stockFlowRequest?.range.start ?? '', stockFlowRequest?.range.end ?? ''),
    queryFn: () => api.marketDataMoneyflowStock(stockFlowRequest!.symbol, { ...stockFlowRequest!.range, freq: stockFlowRequest!.freq }),
    enabled: stockFlowRequest !== null,
    retry: false,
  })
  const blockFlowQuery = useQuery({
    queryKey: QK.marketDataMoneyflowBlocks(blockFlowRequest?.freq ?? 'daily', blockFlowRequest?.date ?? '', blockFlowRequest?.blockType, BLOCK_LIMIT),
    queryFn: () => api.marketDataMoneyflowBlocks({ ...blockFlowRequest!, limit: BLOCK_LIMIT }),
    enabled: blockFlowRequest !== null,
    retry: false,
  })
  const auctionQuery = useQuery({
    queryKey: QK.marketDataCallAuction(auctionRequest?.symbol ?? '', auctionRequest?.date ?? '', auctionRequest?.session, TICK_LIMIT),
    queryFn: () => api.marketDataCallAuction(auctionRequest!.symbol, { date: auctionRequest!.date, session: auctionRequest!.session, limit: TICK_LIMIT }),
    enabled: auctionRequest !== null,
    retry: false,
  })
  const transactionsQuery = useQuery({
    queryKey: QK.marketDataTransactions(transactionsRequest?.symbol ?? '', transactionsRequest?.date ?? '', TICK_LIMIT),
    queryFn: () => api.marketDataTransactions(transactionsRequest!.symbol, { date: transactionsRequest!.date, limit: TICK_LIMIT }),
    enabled: transactionsRequest !== null,
    retry: false,
  })

  const validateSymbolRange = (): { symbol: string; range: DateRange } | null => {
    const validSymbol = normalizedSymbol(symbol)
    if (!validSymbol) {
      setInputError('A 股代码须为 6 位代码加交易所后缀，例如 600519.SH、000001.SZ 或 430047.BJ。')
      return null
    }
    const error = dateRangeError(range)
    if (error) {
      setInputError(error)
      return null
    }
    setInputError(null)
    return { symbol: validSymbol, range }
  }
  const validateSymbolDate = (): { symbol: string; date: string } | null => {
    const validSymbol = normalizedSymbol(symbol)
    if (!validSymbol) {
      setInputError('A 股代码须为 6 位代码加交易所后缀，例如 600519.SH、000001.SZ 或 430047.BJ。')
      return null
    }
    if (!date || date > TODAY) {
      setInputError('请选择不晚于今天的交易日期。')
      return null
    }
    setInputError(null)
    return { symbol: validSymbol, date }
  }
  const submitChip = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    const request = validateSymbolRange()
    if (request) setChipRequest(request)
  }
  const submitStockFlow = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    const request = validateSymbolRange()
    if (!request) return
    if (freq === 'minute' && request.range.start !== request.range.end) {
      setInputError('分钟资金流仅支持单个交易日；请将起止日期设为同一天。')
      return
    }
    setStockFlowRequest({ ...request, freq })
  }
  const submitBlocks = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    if (!date || date > TODAY) {
      setInputError('请选择不晚于今天的交易日期。')
      return
    }
    setInputError(null)
    setBlockFlowRequest({ date, freq, blockType: blockType ? Number(blockType) : undefined })
  }
  const submitAuction = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    const request = validateSymbolDate()
    if (request) setAuctionRequest({ ...request, session: session || undefined })
  }
  const submitTransactions = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    const request = validateSymbolDate()
    if (request) setTransactionsRequest(request)
  }

  return (
    <div className="space-y-4">
      <section className="panel" aria-label="市场数据读取边界">
        <div className="panel-body flex items-start gap-2.5">
          <Database className="mt-0.5 h-4 w-4 shrink-0 text-accent" />
          <p className="text-xs leading-relaxed text-secondary">仅查询上游已发布快照，不会占用或修改用户 <span className="font-mono text-foreground">data/</span>。个股、日期与频率均需在下方显式提交；不会发起全市场扫描。</p>
        </div>
      </section>

      <CapabilityStrip />

      <section className="panel" aria-labelledby="market-data-controls-title">
        <div className="panel-header">
          <div>
            <h2 id="market-data-controls-title" className="text-sm font-semibold text-foreground">查询条件</h2>
            <p className="mt-0.5 text-[10px] text-muted">范围查询单次最多 {MAX_RANGE_DAYS} 天；服务端仍会按可用性和覆盖范围拒绝无效请求。</p>
          </div>
        </div>
        <div className="panel-body grid gap-3 md:grid-cols-2 xl:grid-cols-4">
          <label className="grid gap-1 text-xs text-secondary">
            证券代码
            <input value={symbol} onChange={(event) => setSymbol(event.target.value.toUpperCase())} onBlur={() => setSymbol((value) => value.trim().toUpperCase())} className={cn(INPUT, 'font-mono')} inputMode="text" placeholder="600519.SH" aria-describedby="market-symbol-hint" />
            <span id="market-symbol-hint" className="text-[10px] text-muted">仅接受 000001.SZ / 600519.SH / 430047.BJ</span>
          </label>
          <label className="grid gap-1 text-xs text-secondary">起始日期<input type="date" value={range.start} max={range.end || TODAY} onChange={(event) => setRange((value) => ({ ...value, start: event.target.value }))} className={INPUT} /></label>
          <label className="grid gap-1 text-xs text-secondary">结束日期<input type="date" value={range.end} min={range.start || undefined} max={TODAY} onChange={(event) => setRange((value) => ({ ...value, end: event.target.value }))} className={INPUT} /></label>
          <label className="grid gap-1 text-xs text-secondary">单日日期<input type="date" value={date} max={TODAY} onChange={(event) => setDate(event.target.value)} className={INPUT} /></label>
          <label className="grid gap-1 text-xs text-secondary">资金流频率<select value={freq} onChange={(event) => setFreq(event.target.value as MarketDataFrequency)} className={INPUT}><option value="daily">日级</option><option value="minute">分钟</option></select></label>
          <label className="grid gap-1 text-xs text-secondary">板块类型<select value={blockType} onChange={(event) => setBlockType(event.target.value)} className={INPUT}><option value="">全部类型</option><option value="40">行业（40）</option><option value="41">概念（41）</option><option value="42">地域（42）</option></select></label>
          <label className="grid gap-1 text-xs text-secondary">集合竞价时段<select value={session} onChange={(event) => setSession(event.target.value as MarketDataCallAuctionSession | '')} className={INPUT}><option value="">全部时段</option><option value="open">开盘</option><option value="close">收盘</option></select></label>
          <div className="flex items-end">{inputError ? <p className="rounded-btn bg-danger/10 px-2.5 py-2 text-xs leading-relaxed text-danger" role="alert">{inputError}</p> : <p className="text-[10px] leading-relaxed text-muted">逐笔、竞价单日最多 {TICK_LIMIT.toLocaleString()} 行；板块榜最多 {BLOCK_LIMIT} 行。</p>}</div>
        </div>
      </section>

      <div className="grid gap-4 2xl:grid-cols-2">
        <MarketPanel title="筹码分布" hint="最新指标与已发布时序" icon={<CandlestickChart className="h-3.5 w-3.5 text-accent" />}>
          <form onSubmit={submitChip} className="flex flex-wrap items-center gap-2 border-b border-border/60 px-3 py-2"><span className="text-[10px] text-muted">{range.start || '—'} 至 {range.end || '—'}</span><button type="submit" disabled={chipQuery.isFetching} className={cn(BTN_PRIMARY, 'ml-auto px-2 py-1')}><Search className="h-3 w-3" />查询筹码</button></form>
          <ResultFrame query={chipQuery} idleText="设置 A 股代码与范围后查询筹码分布。" emptyTitle="该范围内没有已发布筹码分布" emptyHint="能力可用但本次条件没有命中行；请调整代码或覆盖范围。">{(data) => <ChipResults data={data} />}</ResultFrame>
        </MarketPanel>
        <MarketPanel title="个股资金流" hint="净额口径保持上游字段" icon={<FileBarChart2 className="h-3.5 w-3.5 text-accent" />}>
          <form onSubmit={submitStockFlow} className="flex flex-wrap items-center gap-2 border-b border-border/60 px-3 py-2"><span className="text-[10px] text-muted">{freq === 'daily' ? '日级' : '分钟'} · {range.start || '—'} 至 {range.end || '—'}</span><button type="submit" disabled={stockFlowQuery.isFetching} className={cn(BTN_PRIMARY, 'ml-auto px-2 py-1')}><Search className="h-3 w-3" />查询资金流</button></form>
          <ResultFrame query={stockFlowQuery} idleText="设置 A 股代码、范围与频率后查询个股资金流。" emptyTitle="该范围内没有个股资金流" emptyHint="能力可用但本次条件没有命中行；请调整代码、频率或覆盖范围。">{(data) => <StockMoneyflowResults data={data} />}</ResultFrame>
        </MarketPanel>
        <MarketPanel title="板块净流入排名" hint="按净额降序展示，非全市场扫描" icon={<BarChart3 className="h-3.5 w-3.5 text-accent" />}>
          <form onSubmit={submitBlocks} className="flex flex-wrap items-center gap-2 border-b border-border/60 px-3 py-2"><span className="text-[10px] text-muted">{freq === 'daily' ? '日级' : '分钟'} · {date || '—'} · {blockType ? `类型 ${blockType}` : '全部类型'}</span><button type="submit" disabled={blockFlowQuery.isFetching} className={cn(BTN_PRIMARY, 'ml-auto px-2 py-1')}><Search className="h-3 w-3" />查询板块</button></form>
          <ResultFrame query={blockFlowQuery} idleText="选择单日、频率与板块类型后查询净流入排名。" emptyTitle="该日期没有板块资金流" emptyHint="能力可用但本次条件没有命中行；请调整日期、频率或板块类型。">{(data) => <BlockMoneyflowResults data={data} />}</ResultFrame>
        </MarketPanel>
        <MarketPanel title="集合竞价与逐笔成交" hint="方向码保持上游原始编码" icon={<ListOrdered className="h-3.5 w-3.5 text-accent" />}>
          <div className="grid border-b border-border/60 sm:grid-cols-2">
            <form onSubmit={submitAuction} className="flex flex-wrap items-center gap-2 border-b border-border/60 px-3 py-2 sm:border-b-0 sm:border-r"><span className="text-[10px] text-muted">集合竞价 · {session || '全部时段'}</span><button type="submit" disabled={auctionQuery.isFetching} className={cn(BTN_PRIMARY, 'ml-auto px-2 py-1')}><Search className="h-3 w-3" />查询</button></form>
            <form onSubmit={submitTransactions} className="flex flex-wrap items-center gap-2 px-3 py-2"><span className="text-[10px] text-muted">逐笔成交 · {date || '—'}</span><button type="submit" disabled={transactionsQuery.isFetching} className={cn(BTN_PRIMARY, 'ml-auto px-2 py-1')}><Search className="h-3 w-3" />查询</button></form>
          </div>
          <div className="divide-y divide-border/60">
            <section aria-labelledby="auction-results-title"><h3 id="auction-results-title" className="px-3 pt-3 text-xs font-medium text-secondary">集合竞价</h3><ResultFrame query={auctionQuery} idleText="设置 A 股代码、日期与时段后查询集合竞价。" emptyTitle="该日期没有集合竞价记录" emptyHint="能力可用但本次条件没有命中行；请调整代码、日期或时段。">{(data) => <AuctionResults data={data} />}</ResultFrame></section>
            <section aria-labelledby="transactions-results-title"><h3 id="transactions-results-title" className="px-3 pt-3 text-xs font-medium text-secondary">逐笔成交</h3><ResultFrame query={transactionsQuery} idleText="设置 A 股代码与日期后查询逐笔成交。" emptyTitle="该日期没有逐笔成交记录" emptyHint="能力可用但本次条件没有命中行；请调整代码或日期。">{(data) => <TransactionsResults data={data} />}</ResultFrame></section>
          </div>
        </MarketPanel>
      </div>
    </div>
  )
}

function MarketPanel({ title, hint, icon, children }: { title: string; hint: string; icon: ReactNode; children: ReactNode }) {
  return (
    <section className="panel overflow-hidden" aria-label={title}>
      <div className="panel-header">
        <div className="flex min-w-0 items-center gap-2">
          {icon}
          <div className="min-w-0"><h2 className="text-sm font-semibold text-foreground">{title}</h2><p className="mt-0.5 text-[10px] text-muted">{hint}</p></div>
        </div>
      </div>
      {children}
    </section>
  )
}

function ChipResults({ data }: { data: MarketDataResponse<MarketDataChipRow> }) {
  const latest = latestChip(data.rows)
  return (
    <>
      <ResultProvenance data={data} />
      {latest && (
        <div className="grid grid-cols-2 gap-px border-b border-border/60 bg-border/60 sm:grid-cols-4">
          <Metric label="最新日期" value={formatDate(latest.trade_date)} />
          <Metric label="平均成本" value={fmtPrice(latest.avg_cost)} />
          <Metric label="主峰价格" value={fmtPrice(latest.peak_price)} />
          <Metric label="获利比例" value={formatNumber(latest.profit_ratio)} />
          <Metric label="90% 集中度" value={formatNumber(latest.concentration_90)} />
          <Metric label="70% 集中度" value={formatNumber(latest.concentration_70)} />
          <Metric label="CR10" value={formatNumber(latest.cr10)} />
          <Metric label="基尼系数" value={formatNumber(latest.gini)} />
        </div>
      )}
      <TableWrap label="筹码分布时序">
        <thead><tr className="border-b border-border text-[10px] text-secondary"><Head>日期</Head><Head align="right">平均成本</Head><Head align="right">主峰价格</Head><Head align="right">峰占比</Head><Head align="right">获利比例</Head><Head align="right">90%集中度</Head><Head align="right">CR10</Head></tr></thead>
        <tbody>{data.rows.map((row, index) => <tr key={`${row.trade_date ?? 'row'}-${index}`} className="border-b border-border/40 last:border-0 hover:bg-elevated/40"><Cell>{formatDate(row.trade_date)}</Cell><Cell align="right">{fmtPrice(row.avg_cost)}</Cell><Cell align="right">{fmtPrice(row.peak_price)}</Cell><Cell align="right">{formatNumber(row.peak_ratio)}</Cell><Cell align="right">{formatNumber(row.profit_ratio)}</Cell><Cell align="right">{formatNumber(row.concentration_90)}</Cell><Cell align="right">{formatNumber(row.cr10)}</Cell></tr>)}</tbody>
      </TableWrap>
    </>
  )
}

function StockMoneyflowResults({ data }: { data: MarketDataResponse<MarketDataMoneyflowStockRow> }) {
  return (
    <>
      <ResultProvenance data={data} />
      <TableWrap label="个股资金流">
        <thead><tr className="border-b border-border text-[10px] text-secondary"><Head>日期 / 时段</Head><Head align="right">总额</Head><Head align="right">净额</Head><Head align="right">传统主力净额</Head><Head align="right">宽口径主力</Head><Head align="right">散户净额</Head></tr></thead>
        <tbody>{data.rows.map((row, index) => <tr key={`${row.trade_date ?? 'row'}-${row.bucket_time ?? index}`} className="border-b border-border/40 last:border-0 hover:bg-elevated/40"><Cell>{row.bucket_time ? `${formatDate(row.trade_date)} ${row.bucket_time}` : formatDate(row.trade_date)}</Cell><Cell align="right">{fmtBigNum(row.total_amount)}</Cell><Cell align="right" className={flowTone(row.net_amount)}>{fmtBigNum(row.net_amount)}</Cell><Cell align="right" className={flowTone(row.main_traditional_net)}>{fmtBigNum(row.main_traditional_net)}</Cell><Cell align="right" className={flowTone(row.main_broad_net)}>{fmtBigNum(row.main_broad_net)}</Cell><Cell align="right" className={flowTone(row.retail_net)}>{fmtBigNum(row.retail_net)}</Cell></tr>)}</tbody>
      </TableWrap>
    </>
  )
}

function BlockMoneyflowResults({ data }: { data: MarketDataResponse<MarketDataMoneyflowBlockRow> }) {
  const rows = [...data.rows].sort((left, right) => (right.net_amount ?? Number.NEGATIVE_INFINITY) - (left.net_amount ?? Number.NEGATIVE_INFINITY))
  return (
    <>
      <ResultProvenance data={data} />
      <TableWrap label="板块净流入排名">
        <thead><tr className="border-b border-border text-[10px] text-secondary"><Head align="right">排名</Head><Head>日期 / 时段</Head><Head>板块</Head><Head>类型</Head><Head align="right">净额</Head><Head align="right">传统主力</Head><Head align="right">散户净额</Head></tr></thead>
        <tbody>{rows.map((row, index) => <tr key={`${row.block_code ?? 'block'}-${row.trade_date ?? 'date'}-${row.bucket_time ?? index}`} className="border-b border-border/40 last:border-0 hover:bg-elevated/40"><Cell align="right">{String(index + 1)}</Cell><Cell>{row.bucket_time ? `${formatDate(row.trade_date)} ${row.bucket_time}` : formatDate(row.trade_date)}</Cell><Cell><span className="font-medium text-foreground">{row.block_name || '—'}</span><span className="ml-1.5 font-mono text-[10px] text-muted">{row.block_code || ''}</span></Cell><Cell>{row.block_type == null ? '—' : String(row.block_type)}</Cell><Cell align="right" className={flowTone(row.net_amount)}>{fmtBigNum(row.net_amount)}</Cell><Cell align="right" className={flowTone(row.main_traditional_net)}>{fmtBigNum(row.main_traditional_net)}</Cell><Cell align="right" className={flowTone(row.retail_net)}>{fmtBigNum(row.retail_net)}</Cell></tr>)}</tbody>
      </TableWrap>
    </>
  )
}

function AuctionResults({ data }: { data: MarketDataCallAuctionResponse }) {
  return (
    <>
      <ResultProvenance data={data} />
      <TableWrap label="集合竞价记录">
        <thead><tr className="border-b border-border text-[10px] text-secondary"><Head>日期 / 时间</Head><Head>时段</Head><Head align="right">价格</Head><Head align="right">数量</Head><Head align="right">金额</Head><Head align="right">方向码</Head></tr></thead>
        <tbody>{data.rows.map((row, index) => <tr key={`${row.event_time}-${index}`} className="border-b border-border/40 last:border-0 hover:bg-elevated/40"><Cell>{`${data.date} ${row.event_time}`}</Cell><Cell>{row.session}</Cell><Cell align="right">{fmtPrice(row.price)}</Cell><Cell align="right">{fmtVolume(row.volume)}</Cell><Cell align="right">{fmtBigNum(row.amount)}</Cell><Cell align="right">{row.direction == null ? '—' : String(row.direction)}</Cell></tr>)}</tbody>
      </TableWrap>
    </>
  )
}

function TransactionsResults({ data }: { data: MarketDataTransactionsResponse }) {
  return (
    <>
      <ResultProvenance data={data} />
      <TableWrap label="逐笔成交记录">
        <thead><tr className="border-b border-border text-[10px] text-secondary"><Head>日期 / 时间</Head><Head align="right">价格</Head><Head align="right">数量</Head><Head align="right">金额</Head><Head align="right">方向码</Head><Head align="right">序号</Head></tr></thead>
        <tbody>
          {data.rows.map((row, index) => (
            <tr
              key={`${row.datetime}-${row.order_count ?? 'na'}-${index}`}
              className="border-b border-border/40 last:border-0 hover:bg-elevated/40"
            >
              <Cell>{row.datetime}</Cell>
              <Cell align="right">{fmtPrice(row.price)}</Cell>
              <Cell align="right">{fmtVolume(row.volume)}</Cell>
              <Cell align="right">{fmtBigNum(row.amount)}</Cell>
              <Cell align="right">{row.direction == null ? '—' : String(row.direction)}</Cell>
              <Cell align="right">{formatNumber(row.order_count, 0)}</Cell>
            </tr>
          ))}
        </tbody>
      </TableWrap>
    </>
  )
}



function Metric({ label, value }: { label: string; value: string }) {
  return <div className="bg-surface px-3 py-2"><p className="text-[10px] text-muted">{label}</p><p className="mt-0.5 truncate font-mono text-xs tabular-nums text-foreground">{value}</p></div>
}

function TableWrap({ label, children }: { label: string; children: ReactNode }) {
  return <div className="max-h-80 overflow-auto"><table className="w-full min-w-[38rem] border-collapse text-[11px]"><caption className="sr-only">{label}</caption>{children}</table></div>
}

function Head({ align = 'left', children }: { align?: 'left' | 'right'; children: ReactNode }) {
  return <th className={cn('sticky top-0 z-10 bg-surface px-3 py-1.5 font-normal', align === 'right' ? 'text-right' : 'text-left')}>{children}</th>
}

function Cell({ align = 'left', className, children }: { align?: 'left' | 'right'; className?: string; children: ReactNode }) {
  return <td className={cn('px-3 py-1.5 font-mono tabular-nums text-secondary', align === 'right' ? 'text-right' : 'text-left', className)}>{children}</td>
}

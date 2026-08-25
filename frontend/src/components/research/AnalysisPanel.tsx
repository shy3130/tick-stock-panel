import { useState, type FormEvent, type ReactNode } from 'react'
import { useQuery, type UseQueryResult } from '@tanstack/react-query'
import {
  AlertCircle,
  CircleAlert,
  Database,
  Loader2,
  RefreshCw,
  Search,
  ShieldAlert,
  Sigma,
  TrendingUp,
} from 'lucide-react'
import { EmptyState } from '@/components/EmptyState'
import { InstrumentSearchInput } from '@/components/instruments/InstrumentSearchInput'
import {
  api,
  type ResearchSymbolAnalysisAvailableResponse,
  type ResearchSymbolAnalysisResponse,
} from '@/lib/api'
import { cn } from '@/lib/cn'
import { QK } from '@/lib/queryKeys'

const INPUT = 'control w-full text-xs'
const BTN_PRIMARY = 'btn-primary text-xs'
const BTN_GHOST = 'btn-secondary text-xs'
const SYMBOL_PATTERN = /^[0-9]{6}\.(SH|SZ|BJ)$/
const CN_DATE_FORMATTER = new Intl.DateTimeFormat('en-CA', {
  timeZone: 'Asia/Shanghai',
  year: 'numeric',
  month: '2-digit',
  day: '2-digit',
})
const TODAY = CN_DATE_FORMATTER.format(new Date())

type DateRange = { start: string; end: string }
type AnalysisRequest = { symbol: string; range: DateRange }

function isoOneYearAgo(): string {
  const date = new Date()
  date.setFullYear(date.getFullYear() - 1)
  return CN_DATE_FORMATTER.format(date)
}

function normalizedSymbol(symbol: string): string | null {
  const value = symbol.trim().toUpperCase()
  return SYMBOL_PATTERN.test(value) ? value : null
}

function dateRangeError(range: DateRange): string | null {
  if (!range.start || !range.end) return '请选择起止日期。'
  const start = new Date(`${range.start}T00:00:00`)
  const end = new Date(`${range.end}T00:00:00`)
  if (!Number.isFinite(start.getTime()) || !Number.isFinite(end.getTime()) || start > end) {
    return '起始日期不能晚于结束日期。'
  }
  if (range.end > TODAY) return '结束日期不能晚于今天。'
  const maxEnd = new Date(start)
  maxEnd.setFullYear(maxEnd.getFullYear() + 5)
  if (end > maxEnd) return '单次分析区间最多五年，避免无边界扫描。'
  return null
}

function formatNumber(value: number | null | undefined, digits = 4): string {
  if (value == null || !Number.isFinite(value)) return '—'
  return value.toLocaleString('zh-CN', { maximumFractionDigits: digits })
}

function formatPercent(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value)) return '—'
  return `${(value * 100).toLocaleString('zh-CN', { maximumFractionDigits: 2 })}%`
}

function statusLabel(status: string): string {
  if (status === 'ok') return '计算完成'
  if (status === 'insufficient_data') return '样本不足'
  return status || '状态未知'
}

function hasInsufficientData(data: ResearchSymbolAnalysisAvailableResponse): boolean {
  const { risk, performance, statistics } = data.result
  return [risk.status, performance.status, statistics.adf.status, statistics.garch.status]
    .some((status) => status === 'insufficient_data')
}

export function AnalysisPanel() {
  const [symbol, setSymbol] = useState('600519.SH')
  const [range, setRange] = useState<DateRange>({ start: isoOneYearAgo(), end: TODAY })
  const [inputError, setInputError] = useState<string | null>(null)
  const [request, setRequest] = useState<AnalysisRequest | null>(null)

  const analysisQuery = useQuery({
    queryKey: QK.researchSymbolAnalysis(request?.symbol ?? '', request?.range.start ?? '', request?.range.end ?? ''),
    queryFn: () => api.researchSymbolAnalysis(request!.symbol, request!.range),
    enabled: request !== null,
    retry: false,
  })

  const submit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    const validSymbol = normalizedSymbol(symbol)
    if (!validSymbol) {
      setInputError('A 股代码须为 6 位代码加交易所后缀，例如 600519.SH、000001.SZ 或 430047.BJ。')
      return
    }
    const rangeError = dateRangeError(range)
    if (rangeError) {
      setInputError(rangeError)
      return
    }
    const nextRequest = { symbol: validSymbol, range }
    const isSameRequest = request !== null
      && request.symbol === nextRequest.symbol
      && request.range.start === nextRequest.range.start
      && request.range.end === nextRequest.range.end
    setInputError(null)
    if (isSameRequest) {
      void analysisQuery.refetch()
      return
    }
    setRequest(nextRequest)
  }

  return (
    <div className="space-y-4">
      <section className="panel" aria-label="研究分析边界">
        <div className="panel-body flex items-start gap-2.5">
          <ShieldAlert className="mt-0.5 h-4 w-4 shrink-0 text-accent" />
          <p className="text-xs leading-relaxed text-secondary">仅从 canonical enriched 日 K 提取单标的收益并计算风险、绩效与统计结果。需显式提交代码和日期范围；不会自动请求或发起全市场扫描。研究计算，不构成交易建议。</p>
        </div>
      </section>

      <section className="panel" aria-labelledby="analysis-controls-title">
        <div className="panel-header">
          <div>
            <h2 id="analysis-controls-title" className="text-sm font-semibold text-foreground">分析条件</h2>
            <p className="mt-0.5 text-[10px] text-muted">仅接受 canonical A 股代码；单次范围最多五年，服务端会再次校验。</p>
          </div>
        </div>
        <form onSubmit={submit} className="panel-body grid gap-3 md:grid-cols-2 xl:grid-cols-4">
          <label className="grid gap-1 text-xs text-secondary">
            证券代码
            <InstrumentSearchInput
              value={symbol}
              onChange={setSymbol}
              assetTypes={['stock']}
              placeholder="输入代码、名称、全拼或简拼"
              ariaLabel="证券代码"
              inputClassName={cn(INPUT, 'font-mono')}
            />
            <span id="analysis-symbol-hint" className="text-[10px] text-muted">可按代码、名称、全拼或简拼搜索；提交时仅接受 000001.SZ / 600519.SH / 430047.BJ</span>
          </label>
          <label className="grid gap-1 text-xs text-secondary">
            起始日期
            <input type="date" value={range.start} max={range.end || TODAY} onChange={(event) => setRange((value) => ({ ...value, start: event.target.value }))} className={INPUT} />
          </label>
          <label className="grid gap-1 text-xs text-secondary">
            结束日期
            <input type="date" value={range.end} min={range.start || undefined} max={TODAY} onChange={(event) => setRange((value) => ({ ...value, end: event.target.value }))} className={INPUT} />
          </label>
          <div className="flex items-end">
            <button type="submit" disabled={analysisQuery.isFetching} className={cn(BTN_PRIMARY, 'w-full justify-center px-3 py-2 xl:w-auto')}>
              <Search className="h-3.5 w-3.5" />计算分析
            </button>
          </div>
          {inputError && <p className="md:col-span-2 xl:col-span-4 rounded-btn bg-danger/10 px-2.5 py-2 text-xs leading-relaxed text-danger" role="alert">{inputError}</p>}
        </form>
      </section>

      <AnalysisResult query={analysisQuery} />
    </div>
  )
}

function AnalysisResult({ query }: { query: UseQueryResult<ResearchSymbolAnalysisResponse, Error> }) {
  if (!query.data && query.fetchStatus === 'idle' && !query.isError) {
    return <p className="px-3 py-8 text-center text-xs text-muted">设置单个 A 股代码与日期范围后，显式提交研究计算。</p>
  }
  if (query.isFetching) {
    return <div className="flex items-center justify-center gap-2 px-3 py-9 text-xs text-muted" role="status"><Loader2 className="h-4 w-4 animate-spin" />正在读取 canonical enriched 日 K</div>
  }
  if (query.isError) {
    const message = query.error instanceof Error ? query.error.message : '请求未完成，请稍后重试。'
    return (
      <div className="panel-body" role="alert">
        <p className="flex items-start gap-1.5 text-xs leading-relaxed text-danger"><AlertCircle className="mt-0.5 h-3.5 w-3.5 shrink-0" />{message}</p>
        <button type="button" onClick={() => void query.refetch()} className={cn(BTN_GHOST, 'mt-3 px-2 py-1')}><RefreshCw className="h-3 w-3" />重试</button>
      </div>
    )
  }
  const data = query.data
  if (!data) return null
  if (!data.available) return <UnavailableResult data={data} />
  return <AnalysisResults data={data} />
}

function UnavailableResult({ data }: { data: ResearchSymbolAnalysisResponse }) {
  return (
    <div className="panel-body" role="alert">
      <p className="flex items-start gap-1.5 text-xs font-medium leading-relaxed text-danger"><CircleAlert className="mt-0.5 h-3.5 w-3.5 shrink-0" />研究数据当前不可用</p>
      <p className="mt-1 text-xs leading-relaxed text-secondary">{data.reason || 'canonical enriched 日 K 当前无法读取；这不是“收益为空”。'}</p>
      {data.source && <p className="mt-2 font-mono text-[10px] text-muted">来源：{data.source}</p>}
    </div>
  )
}

function AnalysisResults({ data }: { data: ResearchSymbolAnalysisAvailableResponse }) {
  const insufficient = hasInsufficientData(data)
  const { risk, performance, statistics } = data.result

  return (
    <div className="space-y-4">
      <ResultProvenance data={data} />
      {insufficient && <EmptyState icon={Database} title="有效收益样本不足" hint="服务端未伪造数值；各计算项会分别保留其样本不足状态。" />}
      {data.warnings.length > 0 && <p className="rounded-btn bg-warning/10 px-3 py-2 text-xs leading-relaxed text-warning" role="status">{data.warnings.join(' · ')}</p>}
      <div className="grid gap-4 xl:grid-cols-3">
        <MetricPanel title="风险" hint="基于单标的日收益" icon={<ShieldAlert className="h-3.5 w-3.5 text-accent" />} status={risk.status}>
          <Metric label="年化波动率" value={formatPercent(risk.descriptive.annualizedVolatility)} />
          <Metric label="历史 VaR" value={formatPercent(risk.historicalVar)} />
          <Metric label="历史 CVaR" value={formatPercent(risk.historicalCvar)} />
          <Metric label="参数 VaR" value={formatPercent(risk.parametricVar)} />
          <Metric label="日均收益" value={formatPercent(risk.descriptive.mean)} />
          <Metric label="风险样本" value={formatNumber(risk.observations, 0)} />
        </MetricPanel>
        <MetricPanel title="绩效" hint="收益路径统计，非交易回测" icon={<TrendingUp className="h-3.5 w-3.5 text-accent" />} status={performance.status}>
          <Metric label="Sortino" value={formatNumber(performance.sortino)} />
          <Metric label="Omega" value={formatNumber(performance.omega)} />
          <Metric label="最大回撤" value={formatPercent(performance.max_drawdown)} />
          <Metric label="Calmar" value={formatNumber(performance.calmar)} />
          <Metric label="Ulcer 指数" value={formatNumber(performance.ulcer_index)} />
          <Metric label="有效收益" value={formatNumber(data.observations, 0)} />
        </MetricPanel>
        <MetricPanel title="统计" hint="ADF 与 GARCH(1,1)" icon={<Sigma className="h-3.5 w-3.5 text-accent" />} status={`${statusLabel(statistics.adf.status)} · ${statusLabel(statistics.garch.status)}`}>
          <Metric label="ADF 统计量" value={formatNumber(statistics.adf.adf_statistic)} />
          <Metric label="ADF p 值" value={formatNumber(statistics.adf.p_value)} />
          <Metric label="ADF 平稳" value={statistics.adf.is_stationary == null ? '—' : statistics.adf.is_stationary ? '是' : '否'} />
          <Metric label="GARCH 当前波动率" value={formatPercent(statistics.garch.current_volatility)} />
          <Metric label="GARCH 长期波动率" value={formatPercent(statistics.garch.long_run_volatility)} />
          <Metric label="GARCH 持续度" value={formatNumber(statistics.garch.persistence)} />
        </MetricPanel>
      </div>
      <p className="text-center text-[10px] text-muted">研究计算，不构成交易建议。</p>
    </div>
  )
}

function ResultProvenance({ data }: { data: ResearchSymbolAnalysisAvailableResponse }) {
  return (
    <p className="border-b border-border/60 px-3 py-2 text-[10px] text-muted">
      来源：<span className="font-mono text-secondary">{data.source || '未声明'}</span> · <span className="font-mono text-secondary">{data.symbol}</span> · {data.start} 至 {data.end} · 有效收益 {formatNumber(data.observations, 0)} 个 · 数据截至 {data.data_as_of || '—'} · 仅读取 canonical enriched 日 K
    </p>
  )
}

function MetricPanel({ title, hint, icon, status, children }: { title: string; hint: string; icon: ReactNode; status: string; children: ReactNode }) {
  return (
    <section className="panel overflow-hidden" aria-label={title}>
      <div className="panel-header">
        <div className="flex min-w-0 items-center gap-2">
          {icon}
          <div className="min-w-0"><h2 className="text-sm font-semibold text-foreground">{title}</h2><p className="mt-0.5 text-[10px] text-muted">{hint}</p></div>
        </div>
        <span className={cn('shrink-0 rounded-full px-2 py-0.5 text-[10px]', status.includes('样本不足') ? 'bg-warning/10 text-warning' : 'bg-success/10 text-success')}>{status.includes(' · ') ? status : statusLabel(status)}</span>
      </div>
      <div className="grid grid-cols-2 divide-x divide-y divide-border/60">{children}</div>
    </section>
  )
}

function Metric({ label, value }: { label: string; value: string }) {
  return <div className="min-w-0 bg-surface px-3 py-2"><p className="text-[10px] text-muted">{label}</p><p className="mt-0.5 truncate font-mono text-xs tabular-nums text-foreground">{value}</p></div>
}

import { useEffect, useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { motion } from 'framer-motion'
import { Play, BarChart3, Clock, Printer, FileDown, Loader2 } from 'lucide-react'
import { api, type FactorColumn, type GroupStat } from '@/lib/api'
import { downloadRunReportHtml } from '@/lib/backtestReportDownload'
import type { RunConnectionState } from '@/lib/runStatus'
import {
  startFactorBacktest,
  stopFactorBacktest,
  tryReconnectFactorBacktest,
  useFactorBacktestTask,
} from '@/lib/factorBacktestTask'
import { fmtPct, priceColorClass } from '@/lib/format'
import { EmptyState } from '@/components/EmptyState'
import { DatePicker } from '@/components/DatePicker'
import { InstrumentSearchAdder } from '@/components/instruments/InstrumentSearchInput'
import { FactorICChart } from './charts/FactorICChart'
import type { ScreenerBacktestHandoff } from '@/lib/screenerBacktestHandoff'
import { FactorGroupNavChart } from './charts/FactorGroupNavChart'
import { BacktestWarnings } from './components/BacktestWarnings'
import { FactorDiagnostics } from './components/FactorDiagnostics'
import { BacktestRunStatus } from '@/components/backtest/BacktestRunStatus'

const formatDate = (date: Date) => date.toISOString().slice(0, 10)
const monthsAgo = (months: number) => {
  const date = new Date()
  date.setMonth(date.getMonth() - months)
  return formatDate(date)
}
const TODAY = formatDate(new Date())
const THREE_MONTHS_AGO = monthsAgo(3)

/** 成本占比：无符号、4 位小数（费用量级通常在万分位） */
const fmtCostPct = (v: number | null | undefined) => {
  if (v == null || Number.isNaN(v)) return '—'
  return `${(v * 100).toFixed(4)}%`
}

const INPUT_CLS = 'control w-full text-xs'
const appendUniqueSymbol = (symbolsText: string, symbol: string) => {
  const key = symbol.trim().toUpperCase()
  const symbols = symbolsText.split(',').map(value => value.trim()).filter(Boolean)
  return symbols.some(s => s.toUpperCase() === key) ? symbolsText : symbolsText ? `${symbolsText},${symbol}` : symbol
}


function StatCard({ label, value, highlight }: {
  label: string
  value: string | null | undefined
  highlight?: 'bull' | 'bear' | 'neutral'
}) {
  const colorCls = highlight === 'bull'
    ? 'text-bull' : highlight === 'bear' ? 'text-bear' : ''
  return (
    <div className="min-w-0">
      <div className="text-[11px] text-muted">{label}</div>
      <div className={`metric-value mt-1 !text-base ${colorCls}`}>
        {value ?? '—'}
      </div>
    </div>
  )
}

function LoadingPanel({
  symbolsText,
  progress,
  startedAt,
  connectionState,
  onCancel,
}: {
  symbolsText: string
  progress?: { stage?: string; label: string; completed: number; total: number; elapsed_ms?: number } | null
  startedAt?: string | null
  connectionState?: RunConnectionState
  onCancel: () => void
}) {
  const stageLabels = ['加载因子面板', '整理有效样本', '计算调仓期收益', '计算截面 IC', '计算分层组合', '汇总多空与风险指标']
  const currentStage = progress?.label ?? '正在连接计算任务'
  const currentStageIndex = Math.max(0, stageLabels.indexOf(currentStage))

  return (
    <div className="space-y-3">
      <BacktestRunStatus
        status="running"
        title="正在计算因子分析"
        runtime={progress ? {
          stage: progress.stage,
          label: progress.label,
          current: symbolsText,
          completed: progress.completed,
          total: progress.total,
          elapsed_ms: progress.elapsed_ms,
        } : {
          label: '正在连接计算任务',
          current: symbolsText,
        }}
        connectionState={connectionState}
        startedAt={startedAt}
        extras={[{ label: '阶段', value: `${Math.max(1, currentStageIndex + 1)}/${stageLabels.length}` }]}
        onCancel={onCancel}
      />

      <div className="grid grid-cols-2 gap-2 md:grid-cols-3">
        {stageLabels.map((item, index) => (
          <div key={item} className={`rounded-btn border px-3 py-2 ${index <= currentStageIndex ? 'border-accent/35 bg-accent/5' : 'border-border bg-elevated'}`}>
            <div className={`h-2 w-10 rounded ${index <= currentStageIndex ? 'bg-accent/70' : 'bg-border'}`} />
            <div className={`mt-2 text-xs ${index <= currentStageIndex ? 'text-secondary' : 'text-muted'}`}>{item}</div>
          </div>
        ))}
      </div>
    </div>
  )
}

interface FactorBacktestProps {
  screenerHandoff?: ScreenerBacktestHandoff | null
  onScreenerHandoffApplied?: () => void
}

export function FactorBacktest({
  screenerHandoff = null,
  onScreenerHandoffApplied,
}: FactorBacktestProps) {
  const factorTask = useFactorBacktestTask()
  const restoredPayload = factorTask?.payload
  const [screenerPool] = useState(() => screenerHandoff
    ? { count: screenerHandoff.symbols.length, asOf: screenerHandoff.asOf }
    : null)
  const [factorName, setFactorName] = useState(() => restoredPayload?.factor_name ?? 'momentum_20d')
  const [symbols, setSymbols] = useState(() => screenerHandoff?.symbols.join(',') ?? restoredPayload?.symbols?.join(',') ?? '')
  const [start, setStart] = useState(() => screenerHandoff?.asOf ?? restoredPayload?.start ?? THREE_MONTHS_AGO)
  const [end, setEnd] = useState(() => restoredPayload?.end ?? TODAY)
  const [nGroups, setNGroups] = useState(() => restoredPayload?.n_groups ?? 5)
  const [weight, setWeight] = useState<'equal' | 'factor_weight'>(() => restoredPayload?.weight ?? 'equal')
  const [fees, setFees] = useState(() => String((restoredPayload?.fees_pct ?? 0.0002) * 10000))
  const [riskFreeRate, setRiskFreeRate] = useState(() => {
    const v = (restoredPayload?.risk_free_rate ?? 0) * 100
    return String(Math.round(v * 100) / 100)
  })
  const [validationError, setValidationError] = useState('')
  const [reportDownloading, setReportDownloading] = useState(false)
  const [reportDownloadError, setReportDownloadError] = useState('')
  const result = factorTask?.result ?? null
  const isPending = factorTask?.isPending ?? false
  const [pendingStartedAt, setPendingStartedAt] = useState<string | null>(null)
  useEffect(() => {
    if (isPending) setPendingStartedAt(prev => prev ?? new Date().toISOString())
    else setPendingStartedAt(null)
  }, [isPending])
  const resultPersisted = result?.persisted !== false
    && !result?.warnings?.some(warning => warning.startsWith('persistence_failed:'))

  useEffect(() => {
    if (screenerHandoff) onScreenerHandoffApplied?.()
  }, [onScreenerHandoffApplied, screenerHandoff])

  useEffect(() => {
    tryReconnectFactorBacktest()
  }, [])

  const columns = useQuery({
    queryKey: ['backtest-factor-columns'],
    queryFn: api.factorColumns,
  })

  // 按 group 分类的因子
  const factorGroups = useMemo(() => {
    const cols = columns.data?.columns ?? []
    const groups: Record<string, FactorColumn[]> = {}
    for (const c of cols) {
      ;(groups[c.group] ??= []).push(c)
    }
    return groups
  }, [columns.data])

  // 当前因子描述
  const factorDesc = useMemo(() => {
    return columns.data?.columns.find(c => c.id === factorName)?.desc ?? ''
  }, [columns.data, factorName])

  const clampStartToScreenerPool = (value: string) => {
    const asOf = screenerPool?.asOf
    return asOf && (!value || value < asOf) ? asOf : value
  }

  const handleDownloadReport = async () => {
    if (!result?.run_id || result.error || !resultPersisted || reportDownloading) return
    setReportDownloadError('')
    setReportDownloading(true)
    try {
      const full = await api.backtestRunGet(result.run_id)
      downloadRunReportHtml(full)
    } catch {
      setReportDownloadError('完整运行记录暂不可读取，无法下载报告；可继续使用“打印 / PDF”。')
    } finally {
      setReportDownloading(false)
    }
  }

  const handleCancel = () => {
    void stopFactorBacktest()
  }

  const handleRun = () => {
    const num = Number(riskFreeRate)
    if (!Number.isFinite(num) || num <= -100 || num > 100) {
      setValidationError('无风险利率需为 (-100, 100] 范围内的数值')
      return
    }
    setValidationError('')
    setReportDownloadError('')
    void startFactorBacktest({
      factor_name: factorName,
      symbols: symbols ? symbols.split(',').map(s => s.trim()).filter(Boolean) : null,
      start: clampStartToScreenerPool(start) || null,
      end: end || undefined,
      n_groups: nGroups,
      rebalance: 'daily',
      weight,
      fees_pct: Number(fees) / 10000,
      risk_free_rate: num / 100,
    })
  }

  const applyRange = (months: number) => {
    setStart(clampStartToScreenerPool(monthsAgo(months)))
    setEnd(formatDate(new Date()))
  }

  const applyAllRange = () => {
    setStart(clampStartToScreenerPool(''))
    setEnd(formatDate(new Date()))
  }

  const rangeKey = end === TODAY && start === THREE_MONTHS_AGO
    ? '3m'
    : end === TODAY && start === monthsAgo(6)
      ? '6m'
      : end === TODAY && start === monthsAgo(12)
        ? '1y'
        : end === TODAY && start === ''
          ? 'all'
          : 'custom'
  const rangeTitle = rangeKey === '3m'
    ? '近 3 个月'
    : rangeKey === '6m'
      ? '近 6 个月'
      : rangeKey === '1y'
        ? '近 1 年'
        : rangeKey === 'all'
          ? '全部历史'
          : '自定义区间'
  const rangeButtonCls = (key: string) => `rounded-btn px-2 py-1 text-[11px] font-medium transition-colors ${rangeKey === key
    ? 'bg-accent/15 text-accent'
    : 'text-muted hover:bg-elevated hover:text-secondary'
  }`

  return (
    <div className="h-full min-h-0 min-w-0 grid grid-cols-1 xl:grid-cols-[18rem_minmax(0,1fr)] gap-3">
      <section className="backtest-config-panel panel flex flex-col min-h-0 xl:overflow-y-auto">
        <div className="panel-header">
          <div>
            <div className="section-kicker">Parameters</div>
            <h2 className="section-title">因子配置</h2>
          </div>
        </div>
        <div className="panel-body space-y-3">
          <p className="text-[11px] leading-4 text-muted">选择因子、区间和分组方式。默认最近 3 个月。</p>
          {screenerPool && (
            <div className="rounded-input border border-accent/25 bg-accent/5 px-2.5 py-2 text-[11px] leading-4 text-secondary" role="status">
              <span className="font-medium text-foreground">已载入条件选股股票池 · {screenerPool.count} 只</span>
              {screenerPool.asOf && (
                <span>。筛选截止日为 {screenerPool.asOf}，回测起点不会早于该日，以避免前视偏差。</span>
              )}
            </div>
          )}

          <div>
            <label className="text-xs font-medium text-secondary block mb-1.5">因子</label>
            <select
              value={factorName}
              onChange={e => setFactorName(e.target.value)}
              className={INPUT_CLS}
            >
              {Object.entries(factorGroups).map(([group, cols]) => (
                <optgroup key={group} label={group}>
                  {cols.map(c => (
                    <option key={c.id} value={c.id}>{c.label}</option>
                  ))}
                </optgroup>
              ))}
            </select>
            {factorDesc && (
              <p className="mt-1 text-[11px] text-muted">{factorDesc}</p>
            )}
          </div>

          <div>
            <label className="text-xs font-medium text-secondary block mb-1.5">
              标的(逗号分隔，留空=全市场)
            </label>
            <input
              type="text"
              value={symbols}
              onChange={e => setSymbols(e.target.value)}
              placeholder="留空则使用全市场，建议最近3个月"
              className={`${INPUT_CLS} font-mono`}
            />
            <InstrumentSearchAdder
              onAdd={result => setSymbols(previous => appendUniqueSymbol(previous, result.symbol))}
              assetTypes={['stock']}
              placeholder="搜索名称或拼音后添加"
              ariaLabel="添加因子回测标的"
              className="mt-2"
            />
          </div>

          <div className="rounded-btn border border-border bg-elevated/40 p-2.5">
            <div className="flex items-center justify-between gap-2">
              <div className="text-xs font-medium text-foreground">回测区间</div>
              <span className="shrink-0 rounded-btn border border-accent/25 bg-accent/10 px-2 py-0.5 text-[10px] font-medium text-accent">
                {rangeTitle}
              </span>
            </div>

            <div className="mt-2 grid grid-cols-2 gap-2">
              <div>
                <label className="text-[11px] text-secondary block mb-1">开始</label>
                <DatePicker
                  value={start}
                  onChange={value => setStart(clampStartToScreenerPool(value))}
                  min={screenerPool?.asOf ?? undefined}
                  max={end || undefined}
                  placeholder="全部历史"
                  className="w-full"
                  buttonClassName="w-full justify-start"
                  align="left"
                />
              </div>
              <div>
                <label className="text-[11px] text-secondary block mb-1">结束</label>
                <DatePicker
                  value={end}
                  onChange={setEnd}
                  min={start || undefined}
                  className="w-full"
                  buttonClassName="w-full justify-start"
                />
              </div>
            </div>

            <div className="mt-2 flex rounded-input bg-base p-0.5">
              <button type="button" onClick={() => applyRange(3)} className={`${rangeButtonCls('3m')} flex-1`}>3个月</button>
              <button type="button" onClick={() => applyRange(6)} className={`${rangeButtonCls('6m')} flex-1`}>6个月</button>
              <button type="button" onClick={() => applyRange(12)} className={`${rangeButtonCls('1y')} flex-1`}>1年</button>
              <button type="button" onClick={applyAllRange} className={`${rangeButtonCls('all')} flex-1`}>全部</button>
            </div>
          </div>

          <div className="grid grid-cols-2 gap-2">
            <div>
              <label className="text-xs font-medium text-secondary block mb-1.5">分组数</label>
              <select value={nGroups} onChange={e => setNGroups(Number(e.target.value))} className={INPUT_CLS}>
                <option value={3}>3组</option>
                <option value={5}>5组</option>
                <option value={10}>10组</option>
              </select>
            </div>
            <div>
              <label className="text-xs font-medium text-secondary block mb-1.5">权重</label>
              <select value={weight} onChange={e => setWeight(e.target.value as 'equal' | 'factor_weight')} className={INPUT_CLS}>
                <option value="equal">等权</option>
                <option value="factor_weight">因子加权</option>
              </select>
            </div>
            <div>
              <label className="text-xs font-medium text-secondary block mb-1.5">佣金(万分之)</label>
              <input type="number" value={fees} onChange={e => setFees(e.target.value)}
                className={INPUT_CLS} />
            </div>
            <div>
              <label className="text-xs font-medium text-secondary block mb-1.5">无风险年化(%)</label>
              <input
                type="number"
                min={-99}
                max={100}
                step={0.1}
                value={riskFreeRate}
                onChange={event => { setRiskFreeRate(event.target.value); setValidationError('') }}
                className={INPUT_CLS}
              />
            </div>
          </div>

          <button
            onClick={handleRun}
            disabled={isPending}
            className="btn-primary w-full"
          >
            <Play className="h-3.5 w-3.5" />
            {isPending ? '分析中…' : '开始因子分析'}
          </button>
        </div>
      </section>

      <section className="backtest-report panel flex flex-col min-h-0 min-w-0 xl:overflow-y-auto">
        <div className="panel-header">
          <div>
            <div className="section-kicker">Results</div>
            <h2 className="section-title">分析结果</h2>
          </div>
          {result && !result.error && result.run_id && (
            <div className="no-print flex items-center gap-2">
              <button
                type="button"
                onClick={() => window.print()}
                className="inline-flex items-center gap-1 rounded-btn border border-border bg-surface px-2 py-1 text-[11px] text-secondary transition-colors hover:border-accent/40 hover:text-accent"
              >
                <Printer className="h-3 w-3" />
                打印 / PDF
              </button>
              {resultPersisted && (
                <button
                  type="button"
                  onClick={() => { void handleDownloadReport() }}
                  disabled={reportDownloading}
                  aria-busy={reportDownloading}
                  aria-label={reportDownloading ? '报告生成中' : '下载报告'}
                  className="inline-flex items-center gap-1 rounded-btn border border-border bg-surface px-2 py-1 text-[11px] text-secondary transition-colors hover:border-accent/40 hover:text-accent disabled:opacity-50"
                >
                  {reportDownloading ? <Loader2 className="h-3 w-3 animate-spin" /> : <FileDown className="h-3 w-3" />}
                  {reportDownloading ? '生成中…' : '下载报告'}
                </button>
              )}
            </div>
          )}
        </div>
        <div className="panel-body space-y-3">
        {result?.error && !result.ic_mean && (
          <div className="text-sm text-danger bg-danger/10 border border-danger/30 rounded-btn px-3 py-2">
            {result.error}
          </div>
        )}

        {factorTask?.error && (
          <div className="text-sm text-danger bg-danger/10 border border-danger/30 rounded-btn px-3 py-2">
            {factorTask.error}
          </div>
        )}
        {validationError && (
          <div className="text-sm text-danger bg-danger/10 border border-danger/30 rounded-btn px-3 py-2">
            {validationError}
          </div>
        )}
        {reportDownloadError && (
          <div className="text-sm text-warning bg-warning/10 border border-warning/30 rounded-btn px-3 py-2">
            {reportDownloadError}
          </div>
        )}

        {!result && !isPending && (
          <EmptyState
            icon={BarChart3}
            title="选择因子并开始分析"
            hint="因子回测分析因子的预测能力 ( IC/IR ) 和分层收益差异。服务器建议优先使用最近3个月；长周期建议本机或 8GB 以上内存环境运行。"
          />
        )}

        {isPending && result && (
          <BacktestRunStatus
            status="running"
            title="正在重新计算因子分析"
            runtime={factorTask?.progress ? {
              stage: factorTask.progress.stage,
              label: factorTask.progress.label,
              current: '当前暂时展示上一次结果',
              completed: factorTask.progress.completed,
              total: factorTask.progress.total,
              elapsed_ms: factorTask.progress.elapsed_ms,
            } : { label: '等待服务端任务', current: '当前暂时展示上一次结果' }}
            connectionState={factorTask?.connectionState}
            startedAt={pendingStartedAt}
            onCancel={handleCancel}
          />
        )}

        {isPending && !result && (
          <LoadingPanel
            symbolsText={factorTask?.payload.symbols?.length ? `${factorTask.payload.symbols.length} 只标的` : '全市场 · 当前区间'}
            progress={factorTask?.progress}
            connectionState={factorTask?.connectionState}
            startedAt={pendingStartedAt}
            onCancel={handleCancel}
          />
        )}

        {result && !result.error && (
          <BacktestWarnings warnings={result.warnings} dataSnapshot={result.data_snapshot} />
        )}

        {result && result.ic_mean != null && (
          <motion.div
            initial={{ opacity: 0, y: 6 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ duration: 0.2, ease: [0.16, 1, 0.3, 1] }}
            className="space-y-3"
          >
            <div className="rounded-btn border border-border bg-elevated/30 p-3">
              <div className="flex items-center justify-between mb-3">
                <h3 className="section-title">因子预测能力</h3>
                <div className="flex items-center gap-2">
                  <span className="text-[11px] text-muted">
                    Rank IC · 日度调仓
                  </span>
                  {result.elapsed_ms > 0 && (
                    <span className="flex items-center gap-1 text-[11px] text-muted">
                      <Clock className="h-3 w-3" />
                      <span className="num">{result.elapsed_ms.toFixed(0)} ms</span>
                    </span>
                  )}
                </div>
              </div>
              <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
                <StatCard
                  label="IC 均值"
                  value={result.ic_mean != null ? fmtPct(result.ic_mean) : null}
                  highlight={result.ic_mean != null
                    ? result.ic_mean > 0.03 ? 'bull' : result.ic_mean < -0.03 ? 'bear' : 'neutral'
                    : undefined}
                />
                <StatCard label="IC 标准差" value={result.ic_std != null ? fmtPct(result.ic_std) : null} />
                <StatCard
                  label="ICIR"
                  value={result.ir != null ? result.ir.toFixed(2) : null}
                  highlight={result.ir != null
                    ? Math.abs(result.ir) > 0.5 ? (result.ir > 0 ? 'bull' : 'bear') : 'neutral'
                    : undefined}
                />
                <StatCard label="IC 胜率" value={result.ic_win_rate != null ? fmtPct(result.ic_win_rate) : null} />
              </div>
            </div>
            <FactorDiagnostics result={result} />

            {result.ic_series.length > 0 && (
              <div className="rounded-btn border border-border overflow-hidden">
                <div className="border-b border-border px-3 py-2">
                  <span className="text-xs font-medium text-secondary">IC 时序</span>
                </div>
                <div className="p-2">
                  <FactorICChart result={result} />
                </div>
              </div>
            )}

            {result.group_nav.length > 0 && (
              <div className="rounded-btn border border-border overflow-hidden">
                <div className="border-b border-border px-3 py-2">
                  <span className="text-xs font-medium text-secondary">扣费后分层净值曲线</span>
                  <span className="ml-2 text-[10px] text-muted">含手续费、滑点与调仓换手</span>
                </div>
                <div className="p-2">
                  <FactorGroupNavChart result={result} />
                </div>
              </div>
            )}

            {result.group_stats.length > 0 && (
              <div className="data-table-scroll rounded-btn border border-border overflow-hidden">
                <table className="data-table">
                  <thead>
                    <tr>
                      <th>分组</th>
                      <th className="text-right">总收益</th>
                      <th className="text-right">年化</th>
                      <th className="text-right">最大回撤</th>
                      <th className="text-right">夏普</th>
                      <th className="text-right">胜率</th>
                      <th className="text-right">平均换手</th>
                      <th className="text-right">总成本</th>
                    </tr>
                  </thead>
                  <tbody>
                    {result.group_stats.map((g: GroupStat) => (
                      <tr key={g.group}>
                        <td className="font-medium">{g.label}</td>
                        <td className={`text-right num ${priceColorClass(g.total_return)}`}>
                          {fmtPct(g.total_return)}
                        </td>
                        <td className={`text-right num ${priceColorClass(g.annual_return)}`}>
                          {fmtPct(g.annual_return)}
                        </td>
                        <td className="text-right num text-bear">{fmtPct(g.max_drawdown)}</td>
                        <td className="text-right num">{g.sharpe != null ? g.sharpe.toFixed(2) : '—'}</td>
                        <td className="text-right num">{fmtPct(g.win_rate)}</td>
                        <td className="text-right num">{fmtPct(g.avg_turnover)}</td>
                        <td className="text-right num text-muted">{fmtCostPct(g.total_cost)}</td>
                      </tr>
                    ))}
                    {result.long_short_stats?.total_return != null && (
                      <tr className="bg-elevated/40">
                        <td className="font-medium text-accent">
                          多空({result.long_short_stats.top_group ?? ''}-{result.long_short_stats.bottom_group ?? ''})
                        </td>
                        <td className={`text-right num font-medium ${priceColorClass(result.long_short_stats.total_return)}`}>
                          {fmtPct(result.long_short_stats.total_return as number)}
                        </td>
                        <td className={`text-right num ${priceColorClass(result.long_short_stats.annual_return ?? null)}`}>
                          {fmtPct(result.long_short_stats.annual_return)}
                        </td>
                        <td className="text-right num text-bear">
                          {fmtPct(result.long_short_stats.max_drawdown as number)}
                        </td>
                        <td className="text-right num">
                          {result.long_short_stats.sharpe?.toFixed(2) ?? '—'}
                        </td>
                        <td className="text-right num">—</td>
                        <td className="text-right num">{fmtPct(result.long_short_stats.avg_turnover)}</td>
                        <td className="text-right num text-muted">{fmtCostPct(result.long_short_stats.total_cost)}</td>
                      </tr>
                    )}
                  </tbody>
                </table>
              </div>
            )}


            <div className="flex flex-wrap items-center gap-4 text-[11px] text-muted">
              <span className="num">{result.n_symbols} 只标的</span>
              <span className="num">{result.n_dates} 个交易日</span>
              <span className="font-mono">run_id: {result.run_id}</span>
            </div>
          </motion.div>
        )}
        </div>
      </section>
    </div>
  )
}

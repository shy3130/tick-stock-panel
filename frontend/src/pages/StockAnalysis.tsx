import { useEffect, useState, type KeyboardEvent } from 'react'
import { useQuery } from '@tanstack/react-query'
import {
  Sparkles,
  LineChart,
  History as HistoryIcon,
  Loader2,
  ExternalLink,
  GitCompare,
  X,
  Search,
  MessagesSquare,
} from 'lucide-react'
import { PageHeader } from '@/components/PageHeader'
import { EmptyState } from '@/components/EmptyState'
import { AiProviderSelector } from '@/components/AiProviderSelector'
import { StockFinancialSearch } from '@/components/financials/StockFinancialSearch'
import { StockPreviewDialog } from '@/components/StockPreviewDialog'
import { LastStockChip } from '@/components/LastStockChip'
import { StockIntradayChart } from '@/components/StockIntradayChart'
import { MarkdownRenderer } from '@/components/financials/MarkdownRenderer'
import { AnalysisKChart, type PriceLevel, type LevelType } from '@/components/stock-analysis/AnalysisKChart'
import { api } from '@/lib/api'
import { useLastStock } from '@/lib/useLastStock'
import { QK } from '@/lib/queryKeys'
import { toast } from '@/components/Toast'
import { resolveEntryProfile } from '@/lib/aiProfile'
import {
  startAnalysis, findTodayReport, useHistoryReports,
  deleteReport, openHistoryReport, type HistoryReport,
} from '@/lib/stockAnalysisStore'

type ResearchView = 'structure' | 'history'
const RESEARCH_VIEWS: ResearchView[] = ['structure', 'history']


/**
 * 个股分析页 —— 日 K + 关键价位(压力/支撑/密集区/枢轴/前高前低)+ AI 四维结构诊断。
 *
 * 与财务分析页的区别:
 *  - 以【行情 + 关键价位】为视觉主体(专用日 K 图表,不复用个股对话框图表)
 *  - AI 输出结构诊断与观察清单,不给买入/卖出/仓位指令
 *  - 报告胶囊用蓝色系,与财务分析(紫色)并存
 */
export function StockAnalysis() {
  const [symbol, setSymbol] = useState<string>('')
  const [name, setName] = useState<string>('')
  const [checking, setChecking] = useState(false)
  const [confirmReport, setConfirmReport] = useState<{ id: string; created_at: string; focus: string } | null>(null)
  const [view, setView] = useState<ResearchView>('structure')
  const [previewSymbol, setPreviewSymbol] = useState<string | null>(null)
  const [profileId, setProfileId] = useState<string>()
  const [publicResearchEnabled, setPublicResearchEnabled] = useState(false)
  const { last: lastStock, remember: rememberStock } = useLastStock('stock-analysis')
  const aiProfiles = useQuery({ queryKey: ['aiProfiles'], queryFn: api.aiProfiles, retry: false })
  const publicResearchHealth = useQuery({
    queryKey: ['stockAnalysisPublicResearchHealth'],
    queryFn: api.stockAnalysisPublicResearchHealth,
    enabled: publicResearchEnabled,
    retry: false,
    staleTime: 5 * 60 * 1000,
  })

  const onSelect = (sym: string, nm: string) => {
    setSymbol(sym)
    setName(nm)
    setView('structure')
    setConfirmReport(null)
    rememberStock(sym, nm)
  }

  const handleAnalyze = async () => {
    if (!symbol || checking) return
    setChecking(true)
    try {
      // 当日已分析过 → 二次确认(查看今日报告 / 重新分析)
      const today = await findTodayReport(symbol)
      if (today) {
        setConfirmReport({ id: today.id, created_at: today.created_at, focus: today.focus })
      } else {
        await doAnalysis()
      }
    } catch {
      await doAnalysis()
    } finally {
      setChecking(false)
    }
  }

  const doAnalysis = async () => {
    const resolvedProfileId = resolveEntryProfile('stock_analysis', aiProfiles.data?.profiles ?? [], aiProfiles.data?.default_id ?? '')
    const r = await startAnalysis(
      symbol,
      name,
      '',
      resolvedProfileId || profileId,
      { enabled: publicResearchEnabled, channels: ['twitter'] },
    )
    if (r.error) toast(r.error, 'error')
  }

  const panelId = 'stock-analysis-panel'
  const structureTabId = 'stock-analysis-tab-structure'
  const historyTabId = 'stock-analysis-tab-history'
  const selectView = (nextView: ResearchView, moveFocus = false) => {
    setView(nextView)
    if (moveFocus) {
      requestAnimationFrame(() => {
        document.getElementById(
          nextView === 'structure' ? structureTabId : historyTabId,
        )?.focus()
      })
    }
  }

  const onViewKeyDown = (
    event: KeyboardEvent<HTMLButtonElement>,
    currentView: ResearchView,
  ) => {
    const currentIndex = RESEARCH_VIEWS.indexOf(currentView)
    let nextIndex = -1
    if (event.key === 'ArrowRight' || event.key === 'ArrowDown') {
      nextIndex = (currentIndex + 1) % RESEARCH_VIEWS.length
    } else if (event.key === 'ArrowLeft' || event.key === 'ArrowUp') {
      nextIndex = (currentIndex - 1 + RESEARCH_VIEWS.length) % RESEARCH_VIEWS.length
    } else if (event.key === 'Home') {
      nextIndex = 0
    } else if (event.key === 'End') {
      nextIndex = RESEARCH_VIEWS.length - 1
    }
    if (nextIndex < 0) return
    event.preventDefault()
    selectView(RESEARCH_VIEWS[nextIndex], true)
  }


  return (
    <div className="workspace-page">
      <PageHeader
        title="个股分析"
        titleExtra={
          <span className="inline-flex items-center rounded-btn border border-warning/30 bg-warning/10 px-1.5 py-0.5 text-[9px] font-semibold uppercase tracking-wider text-warning">
            Beta
          </span>
        }
        subtitle="日 K · 关键价位 · AI 四维诊断 · 可选公开消息搜索（解释行情，不给交易指令）"
      />

      <div className="workspace-content mx-auto max-w-7xl gap-3">
        <div className="workspace-toolbar min-w-0" role="group" aria-label="个股研究命令">
          <div className="w-[36rem] max-w-full min-w-0 shrink">
            <StockFinancialSearch onSelect={onSelect} />
          </div>
          <LastStockChip stock={lastStock} onSelect={onSelect} />

          {symbol && (
            <>
              <button
                type="button"
                onClick={() => setPreviewSymbol(symbol)}
                title="查看个股日 K 详情"
                aria-label={`查看 ${name || symbol} ${symbol} 的日 K 详情`}
                className="group flex min-h-9 min-w-0 items-center gap-2 rounded-btn border border-border bg-elevated px-2.5 py-1 text-sm hover:border-border hover:bg-surface"
              >
                <span className="truncate font-medium text-foreground">{name || symbol}</span>
                <span className="shrink-0 font-mono text-[10px] text-muted">{symbol}</span>
                <span className="hidden shrink-0 text-[11px] text-secondary sm:inline">日 K 详情</span>
                <ExternalLink className="h-3.5 w-3.5 shrink-0 text-muted" aria-hidden="true" />
              </button>

              <div
                className="flex min-h-9 min-w-0 items-stretch rounded-btn border border-border bg-elevated p-0.5"
                role="tablist"
                aria-label="个股研究视图"
              >
                <button
                  type="button"
                  id={structureTabId}
                  role="tab"
                  aria-selected={view === 'structure'}
                  aria-controls={panelId}
                  tabIndex={view === 'structure' ? 0 : -1}
                  onClick={() => selectView('structure')}
                  onKeyDown={event => onViewKeyDown(event, 'structure')}
                  className={`inline-flex min-h-8 min-w-0 items-center gap-1 rounded-[4px] px-2.5 text-xs font-medium ${
                    view === 'structure' ? 'bg-surface text-foreground' : 'text-muted hover:text-secondary'
                  }`}
                >
                  <LineChart className="h-3.5 w-3.5 shrink-0" aria-hidden="true" />
                  行情研判
                </button>
                <button
                  type="button"
                  id={historyTabId}
                  role="tab"
                  aria-selected={view === 'history'}
                  aria-controls={panelId}
                  tabIndex={view === 'history' ? 0 : -1}
                  onClick={() => selectView('history')}
                  onKeyDown={event => onViewKeyDown(event, 'history')}
                  className={`inline-flex min-h-8 min-w-0 items-center gap-1 rounded-[4px] px-2.5 text-xs font-medium ${
                    view === 'history' ? 'bg-surface text-foreground' : 'text-muted hover:text-secondary'
                  }`}
                >
                  <HistoryIcon className="h-3.5 w-3.5 shrink-0" aria-hidden="true" />
                  历史报告
                </button>
              </div>

              <div className="ml-auto flex min-w-0 flex-wrap items-center gap-2">
                <label
                  className="inline-flex min-h-9 cursor-pointer items-center gap-1.5 rounded-btn border border-border bg-elevated/30 px-2 text-[11px] text-secondary transition-colors hover:border-sky-500/30 hover:text-foreground"
                  title="通过 Agent Reach 搜索 Twitter/X 公开消息；默认关闭，外部内容按 C 级未核验证据处理"
                >
                  <input
                    type="checkbox"
                    checked={publicResearchEnabled}
                    onChange={event => setPublicResearchEnabled(event.target.checked)}
                    className="h-3.5 w-3.5 rounded border-border accent-sky-500"
                  />
                  <MessagesSquare className="h-3.5 w-3.5 text-sky-300" aria-hidden="true" />
                  <span>公开消息</span>
                  {publicResearchEnabled && (
                    <span className="font-mono text-[9px] text-muted">
                      {publicResearchHealth.isLoading
                        ? '检查中'
                        : publicResearchHealth.data?.health.twitter?.status === 'ok'
                          ? `X · ${publicResearchHealth.data.health.twitter.active_backend ?? '可用'}`
                          : '源不可用'}
                    </span>
                  )}
                </label>
                <AiProviderSelector entry="stock_analysis" value={profileId} onChange={setProfileId} compact />
                <button
                  type="button"
                  onClick={handleAnalyze}
                  disabled={checking}
                  className="btn-primary min-h-9 text-xs"
                >
                  {checking ? <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden="true" /> : <Sparkles className="h-3.5 w-3.5" aria-hidden="true" />}
                  AI 个股分析
                </button>
              </div>
            </>
          )}
        </div>

        {!symbol ? (
          <EmptyGuide />
        ) : (
          <div
            id={panelId}
            role="tabpanel"
            aria-labelledby={view === 'history' ? historyTabId : structureTabId}
            className="min-w-0"
          >
            {view === 'history' ? (
              <HistoryList symbol={symbol} />
            ) : (
              <StockAnalysisBoard symbol={symbol} />
            )}
          </div>
        )}
      </div>

      {confirmReport && (
        <ConfirmModal
          report={confirmReport}
          onView={() => { openHistoryReport(confirmReport.id); setConfirmReport(null) }}
          onRedo={async () => { setConfirmReport(null); await doAnalysis() }}
          onClose={() => setConfirmReport(null)}
        />
      )}

      <StockPreviewDialog
        symbol={previewSymbol}
        name={previewSymbol === symbol ? name : undefined}
        triggerInfo={null}
        onClose={() => setPreviewSymbol(null)}
      />
    </div>
  )
}

function EmptyGuide() {
  return (
    <div className="grid min-w-0 place-items-center px-3 py-12 sm:px-8 sm:py-16">
      <div className="w-full max-w-lg min-w-0">
        <EmptyState
          icon={LineChart}
          title="搜索标的开始研究"
          hint="不造假数据。选股后查看日 K 结构，再生成四维诊断。"
          className="h-auto px-0 py-0"
        />
        <ol className="mt-6 grid min-w-0 gap-2 sm:grid-cols-3">
          <li className="min-w-0 border-l-2 border-border pl-3">
            <div className="flex items-center gap-1.5 text-[11px] font-medium text-foreground">
              <Search className="h-3.5 w-3.5 text-muted" aria-hidden="true" />
              1. 搜索标的
            </div>
            <p className="mt-1 text-[11px] leading-relaxed text-secondary">输入代码或名称</p>
          </li>
          <li className="min-w-0 border-l-2 border-border pl-3">
            <div className="flex items-center gap-1.5 text-[11px] font-medium text-foreground">
              <LineChart className="h-3.5 w-3.5 text-muted" aria-hidden="true" />
              2. 查看结构
            </div>
            <p className="mt-1 text-[11px] leading-relaxed text-secondary">日 K 与关键价位</p>
          </li>
          <li className="min-w-0 border-l-2 border-border pl-3">
            <div className="flex items-center gap-1.5 text-[11px] font-medium text-foreground">
              <Sparkles className="h-3.5 w-3.5 text-muted" aria-hidden="true" />
              3. 生成诊断
            </div>
            <p className="mt-1 text-[11px] leading-relaxed text-secondary">AI 四维结构，无交易指令</p>
          </li>
        </ol>
      </div>
    </div>
  )
}

// ===== 分析看板:日 K + 关键价位 =====
function StockAnalysisBoard({ symbol }: { symbol: string }) {
  const [selectedDate, setSelectedDate] = useState<string | null>(null)
  const kline = useQuery({
    queryKey: ['kline', symbol, ''],
    queryFn: () => api.klineDaily(symbol, 250),
    enabled: !!symbol,
    staleTime: 60_000,
  })

  const levelsQ = useQuery({
    queryKey: QK.stockLevels(symbol),
    queryFn: () => api.stockAnalysisLevels(symbol, 250),
    enabled: !!symbol,
    staleTime: 60_000,
  })

  useEffect(() => { setSelectedDate(null) }, [symbol])

  if (kline.isLoading) {
    return (
      <div className="flex items-center justify-center py-20" role="status" aria-label="正在加载日 K">
        <Loader2 className="h-5 w-5 animate-spin text-muted" aria-hidden="true" />
      </div>
    )
  }

  const rows = kline.data?.rows ?? []

  if (rows.length === 0) {
    return <EmptyState icon={LineChart} title="暂无日 K 数据" hint="该标的尚未同步日 K,请先在数据页或自选页同步。" />
  }

  const levels = (levelsQ.data?.levels ?? {}) as Record<LevelType, PriceLevel[]>

  // 涨跌色:最后一根 K 线收 vs 前一根收(无前日则按开收判断)
  const last = rows[rows.length - 1]
  const prev = rows[rows.length - 2]
  const curClose = levelsQ.data?.close
  const isUp = prev ? (last.close >= prev.close) : (last.close >= last.open)
  const selectedIdx = selectedDate ? rows.findIndex(r => r.date === selectedDate) : -1
  const prevClose = selectedIdx > 0 ? rows[selectedIdx - 1].close : undefined
  const sourceLabel = kline.data?.source === 'local_disk'
    ? '本地 DuckDB'
    : (kline.data?.source ?? '来源未知')
  const adjustmentLabel = kline.data?.adjustment ? ` · ${kline.data.adjustment}` : ''

  return (
    <div className="min-w-0 space-y-3">
      <dl className="grid min-w-0 grid-cols-2 gap-x-4 gap-y-2 sm:flex sm:flex-wrap sm:items-end sm:gap-x-6">
        <div className="min-w-0">
          <dt className="text-[10px] text-muted">当前价</dt>
          <dd className={`font-mono text-lg font-semibold leading-tight ${isUp ? 'text-bull' : 'text-bear'}`}>
            {curClose?.toFixed(2) ?? '—'}
          </dd>
        </div>
        <div className="min-w-0">
          <dt className="text-[10px] text-muted">数据截止</dt>
          <dd className="font-mono text-sm text-foreground">{last.date}</dd>
        </div>
        <div className="min-w-0">
          <dt className="text-[10px] text-muted">来源</dt>
          <dd className="truncate text-sm text-foreground">{sourceLabel}{adjustmentLabel}</dd>
        </div>
        <div className="min-w-0">
          <dt className="text-[10px] text-muted">样本</dt>
          <dd className="text-sm text-foreground">{rows.length} 个交易日</dd>
        </div>
      </dl>

      {selectedDate && (
        <div className="min-w-0">
          <div className="mb-1 flex items-center justify-between gap-2">
            <span className="text-[11px] text-muted">分时</span>
            <span className="font-mono text-[11px] text-secondary">{selectedDate}</span>
          </div>
          <StockIntradayChart
            symbol={symbol}
            date={selectedDate}
            height={260}
            prevClose={prevClose}
          />
        </div>
      )}

      <AnalysisKChart
        rows={rows}
        levels={levels}
        series={levelsQ.data?.series}
        seriesDates={levelsQ.data?.dates}
        defaultLevelTypes={['sr', 'pivot', 'keltner_s']}
        height={480}
        onDateClick={setSelectedDate}
      />
    </div>
  )
}

// ===== 历史报告列表 =====
function HistoryList({ symbol }: { symbol: string }) {
  const { reports, loaded } = useHistoryReports()
  const [selected, setSelected] = useState<string[]>([])
  const [comparing, setComparing] = useState(false)
  const mine = reports.filter(r => r.symbol === symbol)
  // 只保留仍在本标的列表内的勾选: 切换标的 / 删除报告后自动失效
  const selectedIds = selected.filter(id => mine.some(r => r.id === id))
  const canCompare = selectedIds.length === 2

  const toggle = (id: string) => {
    setSelected(prev => prev.includes(id)
      ? prev.filter(x => x !== id)
      : prev.length >= 2 ? prev : [...prev, id])
  }

  if (!loaded) {
    return (
      <div className="flex items-center justify-center py-20" role="status" aria-label="正在加载历史报告">
        <Loader2 className="h-5 w-5 animate-spin text-muted" aria-hidden="true" />
      </div>
    )
  }
  if (mine.length === 0) {
    return <EmptyState icon={HistoryIcon} title="暂无历史报告" hint={`还没有 ${symbol} 的个股分析报告,点击「AI 个股分析」生成第一份。`} />
  }

  const comparePair = mine
    .filter(r => selectedIds.includes(r.id))
    .sort((a, b) => a.created_at.localeCompare(b.created_at))

  return (
    <div className="min-w-0 space-y-2">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <span className="text-[11px] text-muted">
          {canCompare ? '已选 2 份，可开始对比' : `勾选 2 份同标的报告可对比（已选 ${selectedIds.length}/2）`}
        </span>
        <button
          type="button"
          onClick={() => setComparing(true)}
          disabled={!canCompare}
          className="btn-secondary min-h-9 text-xs"
        >
          <GitCompare className="h-3.5 w-3.5" aria-hidden="true" />
          对比
        </button>
      </div>
      {mine.map(r => (
        <div key={r.id} className="min-w-0 border-b border-border/60 py-3 last:border-b-0">
          <div className="flex items-center justify-between gap-3">
            <div className="flex min-w-0 flex-1 items-start gap-2">
              <input
                type="checkbox"
                checked={selectedIds.includes(r.id)}
                onChange={() => toggle(r.id)}
                disabled={!selectedIds.includes(r.id) && selectedIds.length >= 2}
                aria-label={`选择 ${fmtRelative(r.created_at)} 的报告用于对比`}
                className="mt-1 h-3.5 w-3.5 shrink-0 cursor-pointer accent-sky-400 disabled:cursor-not-allowed disabled:opacity-40"
              />
              <button type="button" onClick={() => openHistoryReport(r.id)} className="min-w-0 flex-1 text-left">
                <div className="flex flex-wrap items-center gap-2">
                  <span className="text-xs text-secondary">{fmtRelative(r.created_at)}</span>
                  {r.close && <span className="font-mono text-[10px] text-muted">价 {r.close.toFixed(2)}</span>}
                  {r.focus && <span className="truncate text-[10px] text-sky-300/70">关注: {r.focus}</span>}
                </div>
                <div className="mt-1 truncate text-xs text-muted">{r.summary || '点击查看完整报告'}</div>
              </button>
            </div>
            <button
              type="button"
              onClick={() => { deleteReport(r.id); toast('已删除', 'success') }}
              aria-label={`删除 ${fmtRelative(r.created_at)} 的报告`}
              className="min-h-9 shrink-0 px-2 py-1 text-[10px] text-muted hover:text-danger"
            >
              删除
            </button>
          </div>
        </div>
      ))}
      {comparing && comparePair.length === 2 && (
        <CompareReportsModal reports={[comparePair[0], comparePair[1]]} onClose={() => setComparing(false)} />
      )}
    </div>
  )
}

// ===== F15: 报告对比弹窗 (显式对比, 不是续写) =====
function CompareReportsModal({ reports, onClose }: {
  reports: [HistoryReport, HistoryReport]
  onClose: () => void
}) {
  const [older, newer] = reports
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4" onClick={onClose}>
      <div
        className="flex max-h-[88vh] w-full max-w-6xl min-w-0 flex-col overflow-hidden rounded-2xl border border-border/60 bg-surface shadow-2xl"
        onClick={e => e.stopPropagation()}
        role="dialog"
        aria-modal="true"
        aria-labelledby="compare-reports-title"
      >
        <div className="flex shrink-0 items-center justify-between gap-3 border-b border-border/50 px-5 py-3">
          <div className="min-w-0">
            <div id="compare-reports-title" className="truncate text-sm font-semibold text-foreground">报告对比 · {older.symbol}</div>
            <div className="mt-0.5 text-[11px] text-muted">显式对比，不是续写 — 两份独立报告并列阅读，左旧右新</div>
          </div>
          <button
            type="button"
            onClick={onClose}
            aria-label="关闭对比"
            className="btn-ghost min-h-9 min-w-9 p-1.5"
          >
            <X className="h-4 w-4 text-muted" aria-hidden="true" />
          </button>
        </div>
        <div className="grid min-h-0 flex-1 md:grid-cols-2 md:divide-x md:divide-border/40">
          {[older, newer].map((r, i) => (
            <div key={r.id} className="flex min-h-0 min-w-0 flex-col">
              <div className="flex shrink-0 items-center gap-2 border-b border-border/30 bg-elevated/30 px-4 py-2">
                <span className="text-[10px] font-semibold uppercase tracking-wider text-muted">{i === 0 ? '旧' : '新'}</span>
                <span className="text-xs text-secondary">{fmtRelative(r.created_at)}</span>
                {r.close != null && <span className="font-mono text-[10px] text-muted">价 {r.close.toFixed(2)}</span>}
                {r.focus && <span className="truncate text-[10px] text-sky-300/70">关注: {r.focus}</span>}
              </div>
              <div className="min-h-0 flex-1 overflow-y-auto px-4 py-3">
                <MarkdownRenderer content={r.content || '（无正文）'} />
              </div>
            </div>
          ))}
        </div>
      </div>
    </div>
  )
}

// ===== 二次确认弹窗 =====
function ConfirmModal({ report, onView, onRedo, onClose }: {
  report: { id: string; created_at: string; focus: string }
  onView: () => void
  onRedo: () => void
  onClose: () => void
}) {
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4" onClick={onClose}>
      <div
        className="panel w-full max-w-sm bg-surface p-5"
        onClick={e => e.stopPropagation()}
        role="dialog"
        aria-modal="true"
        aria-labelledby="confirm-today-title"
      >
        <div className="mb-2 flex items-center gap-2">
          <HistoryIcon className="h-4 w-4 text-sky-400" aria-hidden="true" />
          <span id="confirm-today-title" className="text-sm font-medium text-foreground">该个股已有分析报告</span>
        </div>
        <p className="mb-1 text-xs leading-relaxed text-secondary">
          最近一次报告生成于 <span className="text-foreground">{fmtRelative(report.created_at)}</span>。
        </p>
        {report.focus && <p className="mb-1 text-xs text-muted">关注点: {report.focus}</p>}
        <p className="mb-4 text-xs text-muted">可直接查看历史,或重新生成一份新报告。</p>
        <div className="flex gap-2">
          <button
            type="button"
            onClick={onView}
            className="h-9 min-h-9 flex-1 rounded-lg border border-border bg-elevated text-xs text-secondary hover:text-foreground"
          >
            查看历史
          </button>
          <button
            type="button"
            onClick={onRedo}
            className="btn-primary h-9 min-h-9 flex-1 text-xs"
          >
            重新分析
          </button>
        </div>
      </div>
    </div>
  )
}

function fmtRelative(iso: string): string {
  try {
    const t = new Date(iso).getTime()
    const diff = Date.now() - t
    if (diff < 60_000) return '刚刚'
    if (diff < 3600_000) return `${Math.floor(diff / 60_000)} 分钟前`
    if (diff < 86400_000) return `${Math.floor(diff / 3600_000)} 小时前`
    if (diff < 7 * 86400_000) return `${Math.floor(diff / 86400_000)} 天前`
    return new Date(iso).toLocaleDateString('zh-CN')
  } catch { return iso }
}

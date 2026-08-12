import { useEffect, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Sparkles, LineChart, History as HistoryIcon, Loader2, ExternalLink } from 'lucide-react'
import { PageHeader } from '@/components/PageHeader'
import { EmptyState } from '@/components/EmptyState'
import { AiProviderSelector } from '@/components/AiProviderSelector'
import { StockFinancialSearch } from '@/components/financials/StockFinancialSearch'
import { StockPreviewDialog } from '@/components/StockPreviewDialog'
import { StockIntradayChart } from '@/components/StockIntradayChart'
import { LastStockChip } from '@/components/LastStockChip'
import { AnalysisKChart, type PriceLevel, type LevelType } from '@/components/stock-analysis/AnalysisKChart'
import { api } from '@/lib/api'
import { useLastStock } from '@/lib/useLastStock'
import { QK } from '@/lib/queryKeys'
import { toast } from '@/components/Toast'
import { resolveEntryProfile } from '@/lib/aiProfile'
import {
  startAnalysis, findTodayReport, useHistoryReports,
  deleteReport, openHistoryReport,
} from '@/lib/stockAnalysisStore'

/**
 * 个股分析页 —— 日 K + 关键价位(压力/支撑/密集区/枢轴/前高前低)+ AI 四维分析。
 *
 * 与财务分析页的区别:
 *  - 以【行情 + 关键价位】为视觉主体(专用日 K 图表,不复用个股对话框图表)
 *  - AI 分析输出买卖区间 / 操作建议(非财务质量评级)
 *  - 报告胶囊用蓝色系,与财务分析(紫色)并存
 */
export function StockAnalysis() {
  const [symbol, setSymbol] = useState<string>('')
  const [name, setName] = useState<string>('')
  const [checking, setChecking] = useState(false)
  const [confirmReport, setConfirmReport] = useState<{ id: string; created_at: string; focus: string } | null>(null)
  const [showHistory, setShowHistory] = useState(false)
  const [previewSymbol, setPreviewSymbol] = useState<string | null>(null)
  const [profileId, setProfileId] = useState<string>()
  const { last: lastStock, remember: rememberStock } = useLastStock('stock-analysis')
  const aiProfiles = useQuery({ queryKey: ['aiProfiles'], queryFn: api.aiProfiles, retry: false })

  const onSelect = (sym: string, nm: string) => {
    setSymbol(sym)
    setName(nm)
    setShowHistory(false)
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
    const r = await startAnalysis(symbol, name, '', resolvedProfileId || profileId)
    if (r.error) toast(r.error, 'error')
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
        subtitle="日 K · 关键价位 · AI 四维分析(技术 / 基本面 / 财务 / 消息面)"
        right={
          <div className="workspace-toolbar">
            <LastStockChip stock={lastStock} onSelect={onSelect} />
            {symbol && (
              <button
                onClick={() => setShowHistory(v => !v)}
                className="btn-secondary text-xs"
              >
                <HistoryIcon className="h-3.5 w-3.5" />
                历史报告
              </button>
            )}
          </div>
        }
      />

      <div className="workspace-content mx-auto max-w-7xl gap-3">
        {/* 搜索栏 */}
        <div className="workspace-toolbar">
          <div className="w-[36rem] max-w-full shrink-0">
            <StockFinancialSearch onSelect={onSelect} />
          </div>
          {symbol && (
            <>
              <button
                onClick={() => setPreviewSymbol(symbol)}
                title="查看个股日 K 详情"
                className="group flex items-center gap-2 text-sm rounded-md px-1.5 py-0.5 -mx-1.5 hover:bg-elevated transition-colors"
              >
                <span className="text-foreground font-medium group-hover:text-sky-300 transition-colors">{name || symbol}</span>
                <span className="text-[10px] font-mono text-muted">{symbol}</span>
                <ExternalLink className="h-3 w-3 text-muted opacity-0 group-hover:opacity-100 transition-opacity" />
              </button>
              <button
                onClick={handleAnalyze}
                disabled={checking}
                className="btn-primary text-xs"
              >
                {checking ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Sparkles className="h-3.5 w-3.5" />}
                AI 个股分析
              </button>
              <AiProviderSelector entry="stock_analysis" value={profileId} onChange={setProfileId} compact />
            </>
          )}
        </div>

        {/* 主体 */}
        {!symbol ? (
          <EmptyState
            icon={LineChart}
            title="选择一只股票开始分析"
            hint="搜索代码或名称,查看日 K 与关键价位,并可让 AI 进行技术面 / 基本面 / 财务面 / 消息面四维综合分析。"
          />
        ) : showHistory ? (
          <HistoryList symbol={symbol} />
        ) : (
          <StockAnalysisBoard symbol={symbol} />
        )}
      </div>

      {/* 二次确认:已有历史报告 */}
      {confirmReport && (
        <ConfirmModal
          report={confirmReport}
          onView={() => { openHistoryReport(confirmReport.id); setConfirmReport(null) }}
          onRedo={async () => { setConfirmReport(null); await doAnalysis() }}
          onClose={() => setConfirmReport(null)}
        />
      )}

      {/* 个股日 K 详情对话框(点击名称/代码打开) */}
      <StockPreviewDialog
        symbol={previewSymbol}
        name={previewSymbol === symbol ? name : undefined}
        triggerInfo={null}
        onClose={() => setPreviewSymbol(null)}
      />
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
    return <div className="flex items-center justify-center py-20"><Loader2 className="h-5 w-5 animate-spin text-muted" /></div>
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
    <div className="rounded-card border border-border/60 bg-surface/40 overflow-hidden">
      <div className="px-4 py-3 border-b border-border/40">
        <div className="flex items-center justify-between gap-2">
          <div className="flex items-center gap-2 min-w-0">
            <LineChart className="h-4 w-4 text-sky-400 shrink-0" />
            <span className="text-sm font-medium text-foreground">关键价位分析</span>
          </div>
          <div className="flex shrink-0 flex-col items-end gap-0.5">
            <span className="text-[10px] text-muted">
              {rows.length} 个交易日 · 截至 {last.date} · {sourceLabel}{adjustmentLabel}
            </span>
            <div className="flex items-baseline gap-2">
              <span className="text-[10px] text-muted">当前价</span>
              <span className={`text-base font-mono font-bold ${isUp ? 'text-bull' : 'text-bear'}`}>
                {curClose?.toFixed(2) ?? '—'}
              </span>
            </div>
          </div>
        </div>
      </div>
      <div className="p-3">
        {selectedDate && (
          <div className="mb-3 rounded-lg border border-border/50 bg-base/30 p-2">
            <div className="mb-1 flex items-center justify-between px-1">
              <span className="text-[11px] font-mono text-muted">分时</span>
              <span className="text-[11px] font-mono text-secondary">{selectedDate}</span>
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
    </div>
  )
}

// ===== 历史报告列表 =====
function HistoryList({ symbol }: { symbol: string }) {
  const { reports, loaded } = useHistoryReports()
  const mine = reports.filter(r => r.symbol === symbol)

  if (!loaded) {
    return <div className="flex items-center justify-center py-20"><Loader2 className="h-5 w-5 animate-spin text-muted" /></div>
  }
  if (mine.length === 0) {
    return <EmptyState icon={HistoryIcon} title="暂无历史报告" hint={`还没有 ${symbol} 的个股分析报告,点击「AI 个股分析」生成第一份。`} />
  }

  return (
    <div className="space-y-2">
      {mine.map(r => (
        <div key={r.id} className="rounded-card border border-border/60 bg-surface/40 p-3 hover:border-border transition-colors">
          <div className="flex items-center justify-between gap-3">
            <button onClick={() => openHistoryReport(r.id)} className="flex-1 text-left min-w-0">
              <div className="flex items-center gap-2">
                <span className="text-xs text-secondary">{fmtRelative(r.created_at)}</span>
                {r.close && <span className="text-[10px] font-mono text-muted">价 {r.close.toFixed(2)}</span>}
                {r.focus && <span className="text-[10px] text-sky-300/70 truncate">关注: {r.focus}</span>}
              </div>
              <div className="mt-1 text-xs text-muted truncate">{r.summary || '点击查看完整报告'}</div>
            </button>
            <button
              onClick={() => { deleteReport(r.id); toast('已删除', 'success') }}
              className="shrink-0 text-[10px] text-muted hover:text-danger transition-colors px-2 py-1"
            >
              删除
            </button>
          </div>
        </div>
      ))}
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
      >
        <div className="flex items-center gap-2 mb-2">
          <HistoryIcon className="h-4 w-4 text-sky-400" />
          <span className="text-sm font-medium text-foreground">该个股已有分析报告</span>
        </div>
        <p className="text-xs text-secondary leading-relaxed mb-1">
          最近一次报告生成于 <span className="text-foreground">{fmtRelative(report.created_at)}</span>。
        </p>
        {report.focus && <p className="text-xs text-muted mb-1">关注点: {report.focus}</p>}
        <p className="text-xs text-muted mb-4">可直接查看历史,或重新生成一份新报告。</p>
        <div className="flex gap-2">
          <button onClick={onView}
            className="flex-1 h-8 rounded-lg bg-elevated border border-border text-xs text-secondary hover:text-foreground transition-colors">
            查看历史
          </button>
          <button onClick={onRedo}
            className="btn-primary flex-1 h-8 text-xs transition-all">
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

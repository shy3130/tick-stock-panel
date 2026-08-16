import { useState, useEffect } from 'react'
import { useQuery } from '@tanstack/react-query'
import { useNavigate } from 'react-router-dom'
import { Sparkles, LineChart, History as HistoryIcon, Loader2, ExternalLink, Bell, AlertTriangle } from 'lucide-react'
import { PageHeader } from '@/components/PageHeader'
import { EmptyState } from '@/components/EmptyState'
import { StockFinancialSearch } from '@/components/financials/StockFinancialSearch'
import { StockPreviewDialog } from '@/components/StockPreviewDialog'
import { LastStockChip } from '@/components/LastStockChip'
import { AnalysisKChart, type PriceLevel, type LevelType } from '@/components/stock-analysis/AnalysisKChart'
import { PriceAlertDialog } from '@/components/stock-analysis/PriceAlertDialog'
import { api } from '@/lib/api'
import { useLastStock } from '@/lib/useLastStock'
import { QK } from '@/lib/queryKeys'
import { toast } from '@/components/Toast'
import {
  useHistoryReports, deleteReport, openHistoryReport, loadHistory,
} from '@/lib/stockAnalysisStore'

/**
 * 个股分析页 —— 日 K + 关键价位(压力/支撑/密集区/枢轴/前高前低)+ AI 四维分析。
 *
 * 与财务分析页的区别:
 *  - 以【行情 + 关键价位】为视觉主体(专用日 K 图表,不复用个股对话框图表)
 *  - AI 分析输出客观技术状态与风险提示(非买卖建议、非财务质量评级)
 *  - 报告胶囊用蓝色系,与财务分析(紫色)并存
 */
export function StockAnalysis() {
  const navigate = useNavigate()
  const [symbol, setSymbol] = useState<string>('')
  const [name, setName] = useState<string>('')
  const [previewSymbol, setPreviewSymbol] = useState<string | null>(null)
  const [showPriceAlerts, setShowPriceAlerts] = useState(false)
  const { last: lastStock, remember: rememberStock } = useLastStock('stock-analysis')

  // 进入页面立即加载历史报告(供右侧常驻列表)。store 内部有 historyLoaded 去重, 重复调用安全。
  useEffect(() => { loadHistory() }, [])

  // 自动恢复上次选中的股票(切走再回来不丢)。useLastStock 的 last 来自 localStorage, 同步可用。
  useEffect(() => {
    if (!symbol && lastStock) {
      setSymbol(lastStock.symbol)
      setName(lastStock.name)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const onSelect = (sym: string, nm: string) => {
    setSymbol(sym)
    setName(nm)
    setShowPriceAlerts(false)
    rememberStock(sym, nm)
  }

  const handleAnalyze = () => {
    if (!symbol) return
    const params = new URLSearchParams({ symbol })
    if (name) params.set('name', name)
    navigate(`/quant-lab?${params.toString()}`)
  }

  return (
    <>
      <PageHeader
        title="个股分析"
        subtitle="日 K · 关键价位 · AI 四维分析(技术 / 基本面 / 财务 / 消息面)"
        right={
          <div className="flex items-center gap-2">
            <LastStockChip stock={lastStock} onSelect={onSelect} />
          </div>
        }
      />

      <div className="w-full px-8 py-6 space-y-6">
        {/* 搜索栏 */}
        <div className="flex items-center gap-3">
          <div className="w-72">
            <StockFinancialSearch onSelect={onSelect} assetTypes="stock,index" />
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
                className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-btn bg-gradient-to-r from-sky-500/25 to-blue-500/15 border border-sky-400/30 text-sky-300 text-xs font-medium hover:from-sky-500/35 hover:to-blue-500/25 transition-all"
              >
                <Sparkles className="h-3.5 w-3.5" />
                Quant Lab 研究
              </button>
              <button
                onClick={() => setShowPriceAlerts(true)}
                className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-btn border border-sky-400/25 bg-sky-400/[0.08] text-sky-300 text-xs font-medium hover:border-sky-400/40 hover:bg-sky-400/[0.12] transition-all"
                title="设置价格点位提醒"
              >
                <Bell className="h-3.5 w-3.5" />
                点位提醒
              </button>
            </>
          )}
        </div>

        {/* 主体:左侧当前个股看板 + 右侧常驻历史报告 */}
        <div className="grid grid-cols-[1fr_288px] gap-6 items-start">
          <div className="min-w-0">
            {!symbol ? (
              <EmptyState
                icon={LineChart}
                title="选择一只股票开始分析"
                hint="搜索代码或名称,查看日 K 与关键价位,并可让 AI 进行技术面 / 基本面 / 财务面 / 消息面四维综合分析。"
              />
            ) : (
              <StockAnalysisBoard symbol={symbol} />
            )}
          </div>
          <HistorySidebar />
        </div>
      </div>

      {/* 个股日 K 详情对话框(点击名称/代码打开) */}
      <StockPreviewDialog
        symbol={previewSymbol}
        name={previewSymbol === symbol ? name : undefined}
        triggerInfo={null}
        onClose={() => setPreviewSymbol(null)}
      />

      {showPriceAlerts && symbol && (
        <PriceAlertDialog
          key={symbol}
          symbol={symbol}
          name={name}
          onClose={() => setShowPriceAlerts(false)}
        />
      )}
    </>
  )
}

// ===== 分析看板:日 K + 关键价位 =====
function StockAnalysisBoard({ symbol }: { symbol: string }) {
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

  if (kline.isLoading) {
    return <div className="flex items-center justify-center py-20"><Loader2 className="h-5 w-5 animate-spin text-muted" /></div>
  }

  if (kline.isError) {
    return (
      <EmptyState
        icon={AlertTriangle}
        title="日 K 数据加载失败"
        hint="请检查网络或数据源配置后重试。"
      />
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

  return (
    <div className="rounded-card border border-border/60 bg-surface/40 overflow-hidden">
      <div className="px-4 py-3 border-b border-border/40">
        <div className="flex items-center justify-between gap-2">
          <div className="flex items-center gap-2 min-w-0">
            <LineChart className="h-4 w-4 text-sky-400 shrink-0" />
            <span className="text-sm font-medium text-foreground">关键价位分析</span>
          </div>
          <div className="flex items-baseline gap-2 shrink-0">
            <span className="text-[10px] text-muted">{rows.length} 个交易日</span>
            <span className="text-[10px] text-muted/60">·</span>
            <span className="text-[10px] text-muted">当前价</span>
            <span className={`text-base font-mono font-bold ${isUp ? 'text-bull' : 'text-bear'}`}>
              {curClose?.toFixed(2) ?? '—'}
            </span>
          </div>
        </div>
      </div>
      <div className="p-3">
        <AnalysisKChart
          rows={rows}
          levels={levels}
          series={levelsQ.data?.series}
          seriesDates={levelsQ.data?.dates}
          defaultLevelTypes={['sr', 'pivot', 'keltner_s']}
          height={480}
        />
      </div>
    </div>
  )
}

// ===== 左侧常驻:历史报告侧栏(所有股票,按时间倒序平铺) =====
function HistorySidebar() {
  const { reports, loaded } = useHistoryReports()

  return (
    <aside className="self-start sticky top-0">
      <div className="rounded-card border border-border/60 bg-surface/40 overflow-hidden">
        <div className="px-3 py-2.5 border-b border-border/40 flex items-center gap-2">
          <HistoryIcon className="h-3.5 w-3.5 text-sky-400 shrink-0" />
          <span className="text-xs font-medium text-foreground">历史报告</span>
          {loaded && reports.length > 0 && (
            <span className="ml-auto text-[10px] text-muted">{reports.length}</span>
          )}
        </div>

        {!loaded ? (
          <div className="flex items-center justify-center py-16">
            <Loader2 className="h-4 w-4 animate-spin text-muted" />
          </div>
        ) : reports.length === 0 ? (
          <div className="px-3 py-10 text-center">
            <p className="text-xs text-muted">还没有任何个股分析报告</p>
            <p className="text-[10px] text-muted/60 mt-1">选一只股票,点「AI 个股分析」生成</p>
          </div>
        ) : (
          <div className="max-h-[calc(100vh-220px)] overflow-y-auto p-2 space-y-1.5">
            {reports.map(r => (
              <div
                key={r.id}
                className="group rounded-lg border border-border/40 bg-elevated/20 p-2.5 hover:border-border hover:bg-elevated/40 transition-colors"
              >
                <div className="flex items-center justify-between gap-2">
                  <button
                    onClick={() => openHistoryReport(r.id)}
                    className="flex-1 text-left min-w-0"
                  >
                    <div className="flex items-center gap-1.5 min-w-0">
                      <span className="text-xs font-medium text-foreground truncate">{r.name || r.symbol}</span>
                      <span className="text-[10px] font-mono text-muted shrink-0">{r.symbol}</span>
                    </div>
                    <div className="mt-0.5 flex items-center gap-2 text-[10px] text-muted">
                      <span>{fmtRelative(r.created_at)}</span>
                      {r.close != null && <span className="font-mono">价 {r.close.toFixed(2)}</span>}
                      {r.focus && <span className="text-sky-300/70 truncate">关注: {r.focus}</span>}
                    </div>
                    {r.summary && (
                      <div className="mt-1 text-[11px] text-muted truncate">{r.summary}</div>
                    )}
                  </button>
                  <button
                    onClick={() => { deleteReport(r.id); toast('已删除', 'success') }}
                    className="shrink-0 text-[10px] text-muted/60 hover:text-danger transition-colors px-1 py-0.5 opacity-0 group-hover:opacity-100"
                    title="删除"
                  >
                    删除
                  </button>
                </div>
              </div>
            ))}
          </div>
        )}
      </div>
    </aside>
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

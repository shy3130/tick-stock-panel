import { useEffect } from 'react'
import { History, Trash2, FileText, Clock, Sparkles, Loader2 } from 'lucide-react'
import { useHistoryReports, openHistoryReport, deleteReport, loadHistory } from '@/lib/aiReportStore'
import { useActiveTasks } from '@/lib/aiReportStore'

/**
 * AI 财务分析历史报告面板 —— 显示在财务页底部。
 *
 * - 列出最近 20 条报告(后端裁剪)
 * - 点击查看 → 打开到对话框(历史模式)
 * - 显示正在生成中的对应标的(若该标的有活跃任务,标注)
 * - 支持删除单条
 */
export function ReportHistoryPanel() {
  const { reports, loaded } = useHistoryReports()
  const activeTasks = useActiveTasks()

  // 首次挂载拉取一次
  useEffect(() => { loadHistory() }, [])

  // 活跃任务的 symbol 集合(用于在历史列表里标注"生成中")
  const activeSymbols = new Set(activeTasks.map(t => t.symbol))

  if (!loaded) {
    return (
      <div className="rounded-card border border-border/40 bg-surface px-4 py-6 text-center">
        <Loader2 />
      </div>
    )
  }

  if (reports.length === 0) {
    return (
      <div className="rounded-card border border-dashed border-border/50 bg-surface/50 px-6 py-8 text-center">
        <History className="mx-auto h-6 w-6 text-muted/40" />
        <div className="mt-2 text-xs text-muted">暂无历史分析报告</div>
        <div className="mt-0.5 text-[10px] text-muted/60">选择个股后点击「AI 财务分析」生成,报告会自动保存在此</div>
      </div>
    )
  }

  return (
    <div className="rounded-card border border-border bg-surface overflow-hidden">
      {/* 标题栏 */}
      <div className="flex items-center justify-between px-4 py-2.5 border-b border-border/50 bg-elevated/20">
        <div className="flex items-center gap-2">
          <History className="h-3.5 w-3.5 text-secondary" />
          <span className="text-xs font-medium text-foreground">历史分析报告</span>
          <span className="text-[10px] text-muted">{reports.length}/20</span>
        </div>
        <span className="text-[10px] text-muted/60">点击查看 · 报告最多保留 20 条</span>
      </div>

      {/* 列表 */}
      <div className="divide-y divide-border/30 max-h-80 overflow-y-auto">
        {reports.map(r => {
          const isGenerating = activeSymbols.has(r.symbol)
          return (
            <div
              key={r.id}
              className="group flex items-center gap-3 px-4 py-2.5 hover:bg-elevated/30 transition-colors cursor-pointer"
              onClick={() => openHistoryReport(r.id)}
            >
              {/* 图标 */}
              <div className={`flex h-8 w-8 items-center justify-center rounded-lg shrink-0 ${
                isGenerating
                  ? 'bg-purple-400/10 text-purple-300'
                  : 'bg-elevated text-secondary group-hover:text-accent'
              }`}>
                {isGenerating
                  ? <Sparkles className="h-3.5 w-3.5 animate-pulse" />
                  : <FileText className="h-3.5 w-3.5" />}
              </div>

              {/* 主信息 */}
              <div className="flex-1 min-w-0">
                <div className="flex items-center gap-2">
                  <span className="text-xs font-medium text-foreground truncate">{r.name || r.symbol}</span>
                  <span className="text-[10px] font-mono text-muted shrink-0">{r.symbol}</span>
                  {r.focus && (
                    <span className="hidden sm:inline-block px-1.5 py-px rounded bg-purple-400/10 text-purple-300 text-[9px] shrink-0">
                      {r.focus}
                    </span>
                  )}
                </div>
                {/* 摘要 */}
                <div className="flex items-center gap-2 mt-0.5">
                  <span className="text-[10px] text-muted/70 flex items-center gap-1">
                    <Clock className="h-2.5 w-2.5" />
                    {fmtRelative(r.created_at)}
                  </span>
                  {r.summary && (
                    <span className="text-[10px] text-muted/50 truncate">{r.summary}</span>
                  )}
                </div>
              </div>

              {/* 删除按钮 */}
              <button
                onClick={e => { e.stopPropagation(); deleteReport(r.id) }}
                className="opacity-0 group-hover:opacity-100 p-1.5 rounded-lg hover:bg-danger/10 text-muted hover:text-danger transition-all shrink-0"
                title="删除"
              >
                <Trash2 className="h-3.5 w-3.5" />
              </button>
            </div>
          )
        })}
      </div>
    </div>
  )
}

// ===== 小工具 =====
function fmtRelative(iso: string): string {
  try {
    const t = new Date(iso).getTime()
    const diff = Date.now() - t
    if (diff < 60_000) return '刚刚'
    if (diff < 3600_000) return `${Math.floor(diff / 60_000)} 分钟前`
    if (diff < 86400_000) return `${Math.floor(diff / 3600_000)} 小时前`
    if (diff < 7 * 86400_000) return `${Math.floor(diff / 86400_000)} 天前`
    return new Date(iso).toLocaleDateString('zh-CN', { month: '2-digit', day: '2-digit' })
  } catch { return '' }
}

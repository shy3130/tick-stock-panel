import { useEffect, useRef, useState, useCallback } from 'react'
import { useNavigate } from 'react-router-dom'
import { motion, AnimatePresence } from 'framer-motion'
import {
  X, Sparkles, Loader2, AlertTriangle, Copy, Check, RefreshCw,
  Settings2, Send, Wand2, Minimize2, History, LineChart, Star,
} from 'lucide-react'
import { cn } from '@/lib/cn'
import { MarkdownRenderer } from '@/components/financials/MarkdownRenderer'
import { AiProviderSelector } from '@/components/AiProviderSelector'
import { AiExecutionMetaBadge } from '@/components/AiExecutionMetaBadge'
import { api, type AiExecutionMeta } from '@/lib/api'
import { toast } from '@/components/Toast'
import { stageScreenerBacktestHandoff } from '@/lib/screenerBacktestHandoff'
import {
  type ActiveTask, type HistoryReport,
  minimizeDialog, closeDialog, startAnalysis, cancelAnalysis,
} from '@/lib/stockAnalysisStore'
import { aiStreamStatus } from '@/lib/aiStreamStatus'
import type { StockDataMeta } from '@/lib/stockAnalysisStore'

/**
 * AI 个股分析对话框 —— 蓝色主题,与财务分析对话框区分。
 * 复用 MarkdownRenderer(通用 markdown 渲染);标题/配色/文案独立。
 */

interface Props {
  task: ActiveTask | HistoryReport | null
  mode: 'active' | 'history' | null
  minimized: boolean
}

type Phase = 'loading' | 'streaming' | 'done' | 'error' | 'cancelled'

function getPhase(task: ActiveTask | HistoryReport | null): Phase {
  if (!task) return 'loading'
  if ('phase' in task) return task.phase
  return 'done'
}
function getContent(task: ActiveTask | HistoryReport | null): string {
  return task?.content ?? ''
}
function getMeta(task: ActiveTask | HistoryReport | null) {
  if (!task) return null
  if ('meta' in task) return task.meta
  return { summary: task.summary, close: task.close, levels: task.levels }
}

/** F14: 头部数据口径条「数据截止 {date} · {source} · {adjustment}」 */
function getDataMeta(task: ActiveTask | HistoryReport | null): StockDataMeta | null {
  if (task && 'dataMeta' in task) return task.dataMeta
  return null
}

export function StockAnalysisDialog({ task, mode, minimized }: Props) {
  const navigate = useNavigate()
  const scrollRef = useRef<HTMLDivElement>(null)
  const [focus, setFocus] = useState('')
  const [copied, setCopied] = useState(false)
  const [profileId, setProfileId] = useState<string>()

  const phase = getPhase(task)
  const content = getContent(task)
  const meta = getMeta(task)
  const dataMeta = getDataMeta(task)
  const aiMeta: AiExecutionMeta | null = task && 'meta' in task ? (task.meta?.ai_meta ?? null) : null
  const isHistory = mode === 'history'
  // F9: 连接状态条(仅活跃任务有连接态;历史报告无)
  const status = task && 'connection' in task ? aiStreamStatus({ phase, connection: task.connection }) : null
  const isWorking = phase === 'loading' || phase === 'streaming'
  const open = !!task && !minimized

  useEffect(() => {
    if (open && phase === 'streaming' && scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight
    }
  }, [content, phase, open])

  useEffect(() => {
    setFocus(task && 'focus' in task ? task.focus : '')
  }, [task])

  const handleStartNew = useCallback(async () => {
    if (!task) return
    const name = 'name' in task ? task.name : ''
    const publicResearch = 'publicResearch' in task ? task.publicResearch : undefined
    await startAnalysis(task.symbol, name, focus.trim(), profileId, publicResearch)
  }, [task, focus, profileId])

  const handleCopy = async () => {
    if (!content) return
    try {
      await navigator.clipboard.writeText(content)
      setCopied(true)
      setTimeout(() => setCopied(false), 2000)
    } catch { /* ignore */ }
  }

  const canTakeaway = !!task?.symbol && (phase === 'done' || isHistory)
  const handleWatchlist = async () => {
    if (!task?.symbol) return
    try {
      await api.watchlistAdd(task.symbol)
      toast('已加入自选', 'success')
    } catch (e: any) {
      toast(String(e?.message ?? '加入自选失败'), 'error')
    }
  }
  const handleToBacktest = () => {
    if (!task?.symbol) return
    const n = stageScreenerBacktestHandoff({ target: 'strategy', symbols: [task.symbol], asOf: null })
    if (!n) {
      toast('无法送入回测', 'error')
      return
    }
    navigate('/backtest')
  }


  if (!open) return null

  const error = task && 'error' in task ? task.error : ''

  return (
    <AnimatePresence>
      <motion.div
        initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }}
        className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 backdrop-blur-sm p-4"
        onClick={e => { if (e.target === e.currentTarget && !isWorking) closeDialog() }}
      >
        <motion.div
          initial={{ opacity: 0, scale: 0.96, y: 12 }} animate={{ opacity: 1, scale: 1, y: 0 }} exit={{ opacity: 0, scale: 0.96, y: 12 }}
          transition={{ type: 'spring', damping: 26, stiffness: 320 }}
          className="w-full max-w-3xl max-h-[88vh] bg-surface/95 backdrop-blur-xl border border-border/50 rounded-2xl shadow-2xl flex flex-col overflow-hidden"
        >
          {/* 头部 —— 蓝色主题 */}
          <div className="relative px-5 py-3.5 border-b border-border/50 bg-gradient-to-r from-sky-500/[0.06] via-blue-500/[0.04] to-transparent">
            <div className="flex items-center gap-3">
              <div className="flex h-9 w-9 items-center justify-center rounded-xl bg-gradient-to-br from-sky-500/20 to-blue-500/15 border border-sky-400/30 shrink-0">
                {isHistory
                  ? <History className="h-4.5 w-4.5 text-sky-300" />
                  : <LineChart className="h-4.5 w-4.5 text-sky-300" />}
              </div>
              <div className="min-w-0 flex-1">
                <div className="flex items-center gap-2">
                  <span className="text-sm font-semibold text-foreground truncate">
                    {isHistory ? '历史分析报告' : 'AI 个股分析'}
                  </span>
                  {task && <span className="text-xs text-secondary truncate">{task.name}</span>}
                  {task && <span className="text-[10px] font-mono text-muted shrink-0">{task.symbol}</span>}
                </div>
                <div className="flex items-center gap-2 mt-0.5 text-[10px] text-muted">
                  {dataMeta?.data_as_of && (
                    <span className="shrink-0 font-mono" title="报告生成时行情数据的截止日期">
                      数据截止 {dataMeta.data_as_of}
                    </span>
                  )}
                  {dataMeta?.source && <span className="shrink-0">· {dataMeta.source}</span>}
                  {dataMeta?.adjustment && <span className="shrink-0">· {dataMeta.adjustment}</span>}
                  {dataMeta?.public_research?.status !== undefined
                    && dataMeta.public_research.status !== 'disabled' && (
                    <span
                      className={cn(
                        'shrink-0',
                        dataMeta.public_research.status === 'available'
                          || dataMeta.public_research.status === 'partial'
                          ? 'text-sky-300'
                          : 'text-warning',
                      )}
                      title={(dataMeta.public_research.warnings ?? []).join('；') || '公开消息均为 C 级未核验证据'}
                    >
                      · 公开消息 {dataMeta.public_research.status === 'available'
                        ? `${dataMeta.public_research.evidence.length} 条 [UNVERIFIED]`
                        : dataMeta.public_research.status === 'partial'
                          ? `部分可用 · ${dataMeta.public_research.evidence.length} 条`
                          : '不可用'}
                    </span>
                  )}
                  {status && (
                    <span className={cn(
                      'flex items-center gap-1 shrink-0',
                      status.tone === 'error' ? 'text-danger' : status.tone === 'active' ? 'text-sky-300' : 'text-muted',
                    )}>
                      <span className={cn('h-1.5 w-1.5 rounded-full',
                        status.tone === 'active' && 'bg-sky-400 animate-pulse',
                        status.tone === 'error' && 'bg-danger',
                        status.tone === 'muted' && 'bg-muted',
                      )} />
                      {status.label}
                    </span>
                  )}
                  {meta?.summary ? (
                    <span className="flex items-center gap-1 truncate">
                      <Sparkles className="h-2.5 w-2.5 shrink-0" />
                      <span className="truncate">{meta.summary}</span>
                    </span>
                  ) : isWorking ? <span>正在读取行情与价位数据…</span> : null}
                  {phase === 'streaming' && !status && (
                    <span className="flex items-center gap-1 text-sky-300 shrink-0">
                      <span className="h-1.5 w-1.5 rounded-full bg-sky-400 animate-pulse" />生成中
                    </span>
                  )}
                  {isHistory && task && 'created_at' in task && (
                    <span className="shrink-0">{fmtRelative(task.created_at)}</span>
                  )}
                </div>
                {(dataMeta?.degraded
                  || (dataMeta?.warnings?.length ?? 0) > 0
                  || (dataMeta?.public_research?.warnings?.length ?? 0) > 0) && (
                  <div className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-0.5 text-[10px] text-muted/70">
                    {dataMeta?.degraded && <span className="shrink-0">数据维度降级</span>}
                    {(dataMeta?.warnings ?? []).map((w, i) => (
                      <span key={`data-${i}`} className="truncate" title={w}>{w}</span>
                    ))}
                    {(dataMeta?.public_research?.warnings ?? []).map((w, i) => (
                      <span key={`research-${i}`} className="truncate" title={w}>公开消息：{w}</span>
                    ))}
                  </div>
                )}
              </div>
              <div className="flex items-center gap-1 shrink-0">
                {content && !isWorking && (
                  <button onClick={handleCopy} title="复制全文"
                    className="p-1.5 rounded-lg hover:bg-elevated text-muted hover:text-foreground transition-colors">
                    {copied ? <Check className="h-4 w-4 text-emerald-400" /> : <Copy className="h-4 w-4" />}
                  </button>
                )}
                {!isHistory && isWorking && (
                  <>
                    <button
                      onClick={() => { if (task && 'id' in task) void cancelAnalysis(task.id) }}
                      title="取消本次分析"
                      className="p-1.5 rounded-lg hover:bg-elevated text-muted hover:text-danger transition-colors"
                    >
                      <X className="h-4 w-4" />
                    </button>
                    <button onClick={minimizeDialog} title="最小化为气泡,后台继续生成"
                      className="p-1.5 rounded-lg hover:bg-elevated text-muted hover:text-foreground transition-colors">
                      <Minimize2 className="h-4 w-4" />
                    </button>
                  </>
                )}
                {(!isWorking || isHistory) && (
                  <button onClick={closeDialog} title="关闭"
                    className="p-1.5 rounded-lg hover:bg-elevated text-muted hover:text-foreground transition-colors">
                    <X className="h-4 w-4" />
                  </button>
                )}
              </div>
            </div>
          </div>

          {/* 内容区 */}
          <div ref={scrollRef} className="flex-1 overflow-y-auto px-5 py-4 min-h-[280px]">
            {phase === 'loading' && !content && (
              <div className="flex flex-col items-center justify-center py-16 gap-3">
                <div className="relative">
                  <div className="h-10 w-10 rounded-full bg-gradient-to-br from-sky-500/20 to-blue-500/15 border border-sky-400/30 flex items-center justify-center">
                    <LineChart className="h-4.5 w-4.5 text-sky-300 animate-pulse" />
                  </div>
                  <Loader2 className="absolute -inset-1 h-12 w-12 text-sky-400/40 animate-spin" style={{ animationDuration: '3s' }} />
                </div>
                <div className="text-xs text-secondary">AI 正在分析行情与关键价位…</div>
                <div className="text-[10px] text-muted">读取日 K / 技术指标 / 压力支撑 / 财务,生成四维分析</div>
              </div>
            )}

            {(phase === 'error' || phase === 'cancelled') && (
              <div className="flex flex-col items-center justify-center py-14 gap-3">
                <div className="h-11 w-11 rounded-full bg-danger/10 flex items-center justify-center">
                  <AlertTriangle className="h-5 w-5 text-danger" />
                </div>
                <div className="text-sm font-medium text-foreground">{phase === 'cancelled' ? '已取消' : '分析失败'}</div>
                <div className="text-xs text-secondary text-center max-w-md px-4">{error || (phase === 'cancelled' ? '本次分析已停止,不会写入历史报告' : '')}</div>
                {error.includes('AI') && (
                  <button onClick={() => { window.location.href = '/settings?tab=ai' }}
                    className="mt-1 inline-flex items-center gap-1.5 h-8 px-3 rounded-lg bg-elevated border border-border text-xs text-secondary hover:text-foreground transition-colors">
                    <Settings2 className="h-3.5 w-3.5" /> 去配置 AI
                  </button>
                )}
              </div>
            )}
            {meta?.struct_summary && (
              <div className="mb-3 rounded-lg border border-sky-400/20 bg-sky-500/[0.04] px-3 py-2">
                <div className="text-[10px] font-medium tracking-wide text-sky-300/80">结构摘要（观察，不含方向）</div>
                <div className="mt-1 text-xs text-secondary">{meta.struct_summary.trend}</div>
                {meta.struct_summary.key_levels.length > 0 && (
                  <div className="mt-1.5 flex flex-wrap gap-1">
                    {meta.struct_summary.key_levels.map((level) => (
                      <span key={level} className="rounded bg-elevated px-1.5 py-0.5 font-mono text-[10px] text-muted">{level}</span>
                    ))}
                  </div>
                )}
                {meta.struct_summary.data_gaps.length > 0 && (
                  <div className="mt-1 text-[10px] text-muted/70">缺口：{meta.struct_summary.data_gaps.join('；')}</div>
                )}
              </div>
            )}
            {(content || phase === 'streaming') && (
              <div className="relative">
                <MarkdownRenderer content={content} />
                {phase === 'streaming' && (
                  <span className="inline-block w-1.5 h-3.5 bg-sky-400 ml-0.5 align-middle animate-pulse rounded-sm" />
                )}
              </div>
            )}
          </div>

          {/* 底部:关注点输入 */}
          <div className="border-t border-border/50 bg-surface/60 px-5 py-3">
            <div className="flex items-center gap-2">
              <AiProviderSelector entry="stock_analysis" value={profileId} onChange={setProfileId} compact />
              <div className="flex items-center gap-1.5 text-[10px] text-muted shrink-0">
                <Wand2 className="h-3 w-3" />
                <span className="hidden sm:inline">关注重点</span>
              </div>
              <input
                type="text"
                value={focus}
                onChange={e => setFocus(e.target.value)}
                onKeyDown={e => { if (e.key === 'Enter' && (phase === 'done' || phase === 'error' || phase === 'cancelled' || isHistory)) handleStartNew() }}
                disabled={isWorking}
                placeholder={isHistory ? '修改关注重点,回车重新生成' : (phase === 'done' ? '如:重点看能否突破压力位…回车重新分析' : '可留空,留空则全面分析')}
                className={cn(
                  'flex-1 h-8 px-3 rounded-lg bg-base ring-1 ring-border/30 text-xs text-foreground placeholder:text-muted/40',
                  'focus:outline-none focus:ring-2 focus:ring-sky-400/30 transition-shadow disabled:opacity-50',
                )}
              />
              {isHistory ? (
                <button
                  onClick={handleStartNew}
                  className="inline-flex items-center gap-1.5 h-8 px-3 rounded-lg bg-gradient-to-r from-sky-500/20 to-blue-500/15 border border-sky-400/30 text-xs font-medium text-sky-300 hover:from-sky-500/30 hover:to-blue-500/20 transition-all shrink-0"
                  title="以此关注点重新生成新报告"
                >
                  <RefreshCw className="h-3.5 w-3.5" />重新生成
                </button>
              ) : (
                <button
                  onClick={handleStartNew}
                  disabled={isWorking}
                  className="inline-flex items-center gap-1.5 h-8 px-3 rounded-lg bg-gradient-to-r from-sky-500/20 to-blue-500/15 border border-sky-400/30 text-xs font-medium text-sky-300 hover:from-sky-500/30 hover:to-blue-500/20 disabled:opacity-40 disabled:cursor-not-allowed transition-all shrink-0"
                  title={focus.trim() ? '按关注重点重新分析' : '重新分析'}
                >
                  {isWorking ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : phase === 'done' ? <RefreshCw className="h-3.5 w-3.5" /> : <Send className="h-3.5 w-3.5" />}
                  {phase === 'done' ? '重新分析' : '分析'}
                </button>
              )}
            </div>
            {aiMeta && (
              <div className="mt-1.5">
                <AiExecutionMetaBadge meta={aiMeta} />
              </div>
            )}
            {canTakeaway && (
              <div className="mt-1.5 flex items-center gap-2">
                <button type="button" onClick={() => { void handleWatchlist() }}
                  className="inline-flex items-center gap-1 h-7 px-2 rounded-lg bg-elevated border border-border text-[10px] text-secondary hover:text-foreground">
                  <Star className="h-3 w-3" />加自选
                </button>
                <button type="button" onClick={handleToBacktest}
                  className="inline-flex items-center gap-1 h-7 px-2 rounded-lg bg-elevated border border-border text-[10px] text-secondary hover:text-foreground">
                  <LineChart className="h-3 w-3" />送回测
                </button>
              </div>
            )}
            <p className="mt-1.5 text-[10px] text-muted/50 leading-relaxed">
              {isHistory
                ? '历史报告为静态记录;修改关注重点后将作为新任务重新生成。报告仅供参考,不构成投资建议。'
                : '报告由项目已配置的 AI 模型基于本地行情与财务数据生成;消息面维度暂依据价量异动推断。报告仅供参考,不构成投资建议。'}
            </p>
          </div>
        </motion.div>
      </motion.div>
    </AnimatePresence>
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
  } catch { return '' }
}

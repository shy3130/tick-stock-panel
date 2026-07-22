import { useEffect, useRef, useState, useCallback } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import {
  X, Sparkles, Loader2, AlertTriangle, Copy, Check, RefreshCw,
  Database, Settings2, Send, Wand2, Minimize2, History,
} from 'lucide-react'
import { cn } from '@/lib/cn'
import { copyText } from '@/lib/clipboard'
import { toast } from '@/components/Toast'
import { MarkdownRenderer } from './MarkdownRenderer'
import {
  type ActiveTask, type HistoryReport,
  minimizeDialog, closeDialog, startAnalysis,
} from '@/lib/aiReportStore'

interface Props {
  /** 当前展示的任务;活跃任务或历史报告 */
  task: ActiveTask | HistoryReport | null
  mode: 'active' | 'history' | null
  minimized: boolean
}

type Phase = 'loading' | 'streaming' | 'done' | 'error'

// 统一字段读取:活跃任务有 phase/createdAt,历史报告没有(按 done 处理)
function getPhase(task: ActiveTask | HistoryReport | null): Phase {
  if (!task) return 'loading'
  if ('phase' in task) return task.phase
  return 'done'  // 历史报告视为已完成
}
function getContent(task: ActiveTask | HistoryReport | null): string {
  return task?.content ?? ''
}
function getMeta(task: ActiveTask | HistoryReport | null) {
  if (!task) return null
  if ('meta' in task) return task.meta
  // 历史报告
  return { summary: task.summary, periods: task.periods }
}

export function AiAnalysisDialog({ task, mode, minimized }: Props) {
  const scrollRef = useRef<HTMLDivElement>(null)
  const focusInputRef = useRef<HTMLInputElement>(null)
  const [focus, setFocus] = useState('')
  const [copied, setCopied] = useState(false)

  const phase = getPhase(task)
  const content = getContent(task)
  const meta = getMeta(task)
  const isHistory = mode === 'history'
  const isWorking = phase === 'loading' || phase === 'streaming'
  const open = !!task && !minimized

  // 流式时自动滚动到底部
  useEffect(() => {
    if (open && phase === 'streaming' && scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight
    }
  }, [content, phase, open])

  // 切换任务时回填 focus
  useEffect(() => {
    setFocus(task && 'focus' in task ? task.focus : '')
  }, [task])

  const handleStartNew = useCallback(async () => {
    if (!task) return
    const name = 'name' in task ? task.name : ''
    await startAnalysis(task.symbol, name, focus.trim())
  }, [task, focus])

  const handleCopy = async () => {
    if (!content) return
    const success = await copyText(content)
    if (!success) {
      toast('复制失败,请手动选择文本', 'error')
      return
    }
    setCopied(true)
    toast('已复制到剪贴板', 'success')
    setTimeout(() => setCopied(false), 2000)
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
          {/* ===== 头部 ===== */}
          <div className="relative px-5 py-3.5 border-b border-border/50 bg-gradient-to-r from-purple-500/[0.06] via-fuchsia-500/[0.04] to-transparent">
            <div className="flex items-center gap-3">
              <div className="flex h-9 w-9 items-center justify-center rounded-xl bg-gradient-to-br from-purple-500/20 to-fuchsia-500/15 border border-purple-400/30 shrink-0">
                {isHistory
                  ? <History className="h-4.5 w-4.5 text-purple-300" />
                  : <Sparkles className="h-4.5 w-4.5 text-purple-300" />}
              </div>
              <div className="min-w-0 flex-1">
                <div className="flex items-center gap-2">
                  <span className="text-sm font-semibold text-foreground truncate">
                    {isHistory ? '历史分析报告' : 'AI 财务分析'}
                  </span>
                  {task && <span className="text-xs text-secondary truncate">{task.name}</span>}
                  {task && <span className="text-[10px] font-mono text-muted shrink-0">{task.symbol}</span>}
                </div>
                <div className="flex items-center gap-2 mt-0.5 text-[10px] text-muted">
                  {meta?.summary ? (
                    <span className="flex items-center gap-1 truncate">
                      <Database className="h-2.5 w-2.5 shrink-0" />
                      <span className="truncate">{meta.summary}</span>
                    </span>
                  ) : isWorking ? <span>正在准备数据…</span> : null}
                  {phase === 'streaming' && (
                    <span className="flex items-center gap-1 text-purple-300 shrink-0">
                      <span className="h-1.5 w-1.5 rounded-full bg-purple-400 animate-pulse" />生成中
                    </span>
                  )}
                  {isHistory && task && 'created_at' in task && (
                    <span className="shrink-0">{fmtRelative(task.created_at)}</span>
                  )}
                </div>
              </div>
              {/* 右侧操作按钮 */}
              <div className="flex items-center gap-1 shrink-0">
                {/* 复制:仅在内容就绪且非生成中显示 */}
                {content && !isWorking && (
                  <button onClick={handleCopy} title="复制全文"
                    className="p-1.5 rounded-lg hover:bg-elevated text-muted hover:text-foreground transition-colors">
                    {copied ? <Check className="h-4 w-4 text-emerald-400" /> : <Copy className="h-4 w-4" />}
                  </button>
                )}
                {/* 生成中:仅最小化(后台继续生成),无关闭按钮 */}
                {!isHistory && isWorking && (
                  <button onClick={minimizeDialog} title="最小化为气泡,后台继续生成"
                    className="p-1.5 rounded-lg hover:bg-elevated text-muted hover:text-foreground transition-colors">
                    <Minimize2 className="h-4 w-4" />
                  </button>
                )}
                {/* 完成态/历史报告:显示关闭按钮 */}
                {(!isWorking || isHistory) && (
                  <button onClick={closeDialog} title="关闭"
                    className="p-1.5 rounded-lg hover:bg-elevated text-muted hover:text-foreground transition-colors">
                    <X className="h-4 w-4" />
                  </button>
                )}
              </div>
            </div>
          </div>

          {/* ===== 内容区 ===== */}
          <div ref={scrollRef} className="flex-1 overflow-y-auto px-5 py-4 min-h-[280px]">
            {/* 加载态 */}
            {phase === 'loading' && !content && (
              <div className="flex flex-col items-center justify-center py-16 gap-3">
                <div className="relative">
                  <div className="h-10 w-10 rounded-full bg-gradient-to-br from-purple-500/20 to-fuchsia-500/15 border border-purple-400/30 flex items-center justify-center">
                    <Sparkles className="h-4.5 w-4.5 text-purple-300 animate-pulse" />
                  </div>
                  <Loader2 className="absolute -inset-1 h-12 w-12 text-purple-400/40 animate-spin" style={{ animationDuration: '3s' }} />
                </div>
                <div className="text-xs text-secondary">AI 正在分析财务数据…</div>
                <div className="text-[10px] text-muted">读取利润表 / 资负表 / 现金流 / 核心指标,生成专业报告</div>
              </div>
            )}

            {/* 错误态 */}
            {phase === 'error' && (
              <div className="flex flex-col items-center justify-center py-14 gap-3">
                <div className="h-11 w-11 rounded-full bg-danger/10 flex items-center justify-center">
                  <AlertTriangle className="h-5 w-5 text-danger" />
                </div>
                <div className="text-sm font-medium text-foreground">分析失败</div>
                <div className="text-xs text-secondary text-center max-w-md px-4">{error}</div>
                {error.includes('AI') && (
                  <button onClick={() => { window.location.href = '/settings?tab=ai' }}
                    className="mt-1 inline-flex items-center gap-1.5 h-8 px-3 rounded-lg bg-elevated border border-border text-xs text-secondary hover:text-foreground transition-colors">
                    <Settings2 className="h-3.5 w-3.5" /> 去配置 AI
                  </button>
                )}
                <button onClick={handleStartNew}
                  className="mt-1 inline-flex items-center gap-1.5 h-8 px-3 rounded-lg bg-purple-500/15 border border-purple-400/30 text-xs text-purple-300 hover:bg-purple-500/20 transition-colors">
                  <RefreshCw className="h-3.5 w-3.5" /> 重试
                </button>
              </div>
            )}

            {/* 报告内容 */}
            {(content || phase === 'streaming') && (
              <div className="relative">
                <MarkdownRenderer content={content} />
                {phase === 'streaming' && (
                  <span className="inline-block w-1.5 h-3.5 bg-purple-400 ml-0.5 align-middle animate-pulse rounded-sm" />
                )}
              </div>
            )}
          </div>

          {/* ===== 底部:自定义关注点输入 ===== */}
          <div className="border-t border-border/50 bg-surface/60 px-5 py-3">
            <div className="flex items-center gap-2">
              <div className="flex items-center gap-1.5 text-[10px] text-muted shrink-0">
                <Wand2 className="h-3 w-3" />
                <span className="hidden sm:inline">关注重点</span>
              </div>
              <input
                ref={focusInputRef}
                type="text"
                value={focus}
                onChange={e => setFocus(e.target.value)}
                onKeyDown={e => { if (e.key === 'Enter' && (phase === 'done' || phase === 'error' || isHistory)) handleStartNew() }}
                disabled={isWorking}
                placeholder={isHistory ? '修改关注重点,回车重新生成' : (phase === 'done' ? '如:重点看债务风险…回车重新分析' : '可留空,留空则全面分析')}
                className={cn(
                  'flex-1 h-8 px-3 rounded-lg bg-base ring-1 ring-border/30 text-xs text-foreground placeholder:text-muted/40',
                  'focus:outline-none focus:ring-2 focus:ring-purple-400/30 transition-shadow disabled:opacity-50',
                )}
              />
              {isHistory ? (
                <button
                  onClick={handleStartNew}
                  className="inline-flex items-center gap-1.5 h-8 px-3 rounded-lg bg-gradient-to-r from-purple-500/20 to-fuchsia-500/15 border border-purple-400/30 text-xs font-medium text-purple-300 hover:from-purple-500/30 hover:to-fuchsia-500/20 transition-all shrink-0"
                  title="以此关注点重新生成新报告"
                >
                  <RefreshCw className="h-3.5 w-3.5" />重新生成
                </button>
              ) : (
                <button
                  onClick={handleStartNew}
                  disabled={isWorking}
                  className="inline-flex items-center gap-1.5 h-8 px-3 rounded-lg bg-gradient-to-r from-purple-500/20 to-fuchsia-500/15 border border-purple-400/30 text-xs font-medium text-purple-300 hover:from-purple-500/30 hover:to-fuchsia-500/20 disabled:opacity-40 disabled:cursor-not-allowed transition-all shrink-0"
                  title={focus.trim() ? '按关注重点重新分析' : '重新分析'}
                >
                  {isWorking ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : phase === 'done' ? <RefreshCw className="h-3.5 w-3.5" /> : <Send className="h-3.5 w-3.5" />}
                  {phase === 'done' ? '重新分析' : '分析'}
                </button>
              )}
            </div>
            <p className="mt-1.5 text-[10px] text-muted/50 leading-relaxed">
              {isHistory
                ? '历史报告为静态记录;修改关注重点后将作为新任务重新生成。报告仅供参考,不构成投资建议。'
                : '报告由项目已配置的 AI 模型基于本地财务数据生成;可在输入框追加关注点后重新生成。报告仅供参考,不构成投资建议。'}
            </p>
          </div>
        </motion.div>
      </motion.div>
    </AnimatePresence>
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
    return new Date(iso).toLocaleDateString('zh-CN')
  } catch { return '' }
}

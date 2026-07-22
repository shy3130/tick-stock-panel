import { useMemo, useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { AnimatePresence, motion, useReducedMotion } from 'framer-motion'
import { AlertCircle, BookMarked, Check, ExternalLink, Loader2, RotateCcw, X } from 'lucide-react'
import { useLocation, useNavigate } from 'react-router-dom'

import { cn } from '@/lib/cn'
import {
  RESEARCH_LEDGER_QUERY_KEY,
  researchLedgerApi,
  type ResearchCaptureAction,
  type ResearchCaptureInput,
} from './api'

interface TriggerInfo {
  price?: number | null
  changePct?: number | null
  ts?: number
  signals?: string[]
  message?: string
}

interface Props {
  symbol: string
  name?: string
  triggerInfo?: TriggerInfo | null
}

interface CaptureNotice {
  entryId: string
  captureId: string
  action: ResearchCaptureAction
}

const SOURCE_MAP: Array<{ prefix: string; source: string; label: string }> = [
  { prefix: '/watchlist', source: 'watchlist', label: '自选' },
  { prefix: '/screener', source: 'screener', label: '策略选股' },
  { prefix: '/monitor', source: 'monitor', label: '监控中心' },
  { prefix: '/concept-analysis', source: 'concept', label: '概念分析' },
  { prefix: '/industry-analysis', source: 'industry', label: '行业分析' },
  { prefix: '/limit-ladder', source: 'limit-ladder', label: '连板梯队' },
  { prefix: '/stock-analysis', source: 'stock-analysis', label: '个股分析' },
  { prefix: '/', source: 'dashboard', label: '市场看板' },
]

function contextForPath(pathname: string) {
  return SOURCE_MAP.find(item => (
    item.prefix === '/' ? pathname === '/' : pathname.startsWith(item.prefix)
  )) ?? {
    source: 'stock-preview',
    label: '个股预览',
  }
}

function noticeCopy(action: ResearchCaptureAction) {
  if (action === 'created') return { title: '已建立待整理研究', detail: '标的与当前来源已自动保存。' }
  if (action === 'appended') return { title: '已追加到现有研究', detail: '本次来源已加入系统记录时间线。' }
  return { title: '已在研究账本中', detail: '相同来源已记录，本次没有重复写入。' }
}

export function ResearchCaptureButton({ symbol, name, triggerInfo }: Props) {
  const location = useLocation()
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const reduceMotion = useReducedMotion()
  const [notice, setNotice] = useState<CaptureNotice | null>(null)

  const payload = useMemo<ResearchCaptureInput>(() => {
    const context = triggerInfo ? { source: 'monitor', label: '监控触发' } : contextForPath(location.pathname)
    const triggeredAt = triggerInfo?.ts ? new Date(triggerInfo.ts).toISOString() : null
    const signalText = triggerInfo?.signals?.filter(Boolean).join('、') ?? ''
    const summary = triggerInfo?.message?.trim()
      || (signalText ? `触发信号：${signalText}` : `从${context.label}加入研究`)
    const sourceKey = triggerInfo?.ts
      ? `${context.source}:${triggerInfo.ts}:${symbol.toUpperCase()}`
      : `${context.source}:${symbol.toUpperCase()}`
    return {
      symbol,
      name: name ?? '',
      source: context.source,
      source_label: context.label,
      source_key: sourceKey,
      summary,
      snapshot: {
        path: location.pathname,
        price: triggerInfo?.price ?? null,
        change_pct: triggerInfo?.changePct ?? null,
        signals: signalText || null,
        triggered_at: triggeredAt,
      },
    }
  }, [location.pathname, name, symbol, triggerInfo])

  const capture = useMutation({
    mutationFn: () => researchLedgerApi.capture(payload),
    onSuccess: async result => {
      setNotice({
        entryId: result.entry.id,
        captureId: result.capture_id,
        action: result.action,
      })
      await queryClient.invalidateQueries({ queryKey: RESEARCH_LEDGER_QUERY_KEY })
    },
  })

  const undo = useMutation({
    mutationFn: ({ entryId, captureId }: { entryId: string; captureId: string }) => (
      researchLedgerApi.undoCapture(entryId, captureId)
    ),
    onSuccess: async () => {
      setNotice(null)
      await queryClient.invalidateQueries({ queryKey: RESEARCH_LEDGER_QUERY_KEY })
    },
  })

  const buttonLabel = capture.isPending
    ? '加入中'
    : capture.isError
      ? '重试'
      : notice?.action === 'duplicate'
        ? '已在账本'
        : notice
          ? '已加入'
          : '加入研究'

  return (
    <>
      <button
        type="button"
        onClick={() => capture.mutate()}
        disabled={capture.isPending || notice !== null}
        className={cn(
          'inline-flex h-11 shrink-0 items-center gap-1.5 whitespace-nowrap rounded border px-2.5 text-xs font-medium transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-g-research disabled:cursor-default md:h-8',
          notice
            ? 'border-emerald-500/30 bg-emerald-500/10 text-emerald-500'
            : 'border-g-research/30 bg-g-research/10 text-g-research hover:border-g-research/50 hover:bg-g-research/15',
        )}
        title="将当前标的和来源保存到研究账本"
      >
        {capture.isPending
          ? <Loader2 className="h-3.5 w-3.5 animate-spin" />
          : notice
            ? <Check className="h-3.5 w-3.5" />
            : <BookMarked className="h-3.5 w-3.5" />}
        {buttonLabel}
      </button>

      <AnimatePresence>
        {capture.isError ? (
          <motion.aside
            initial={reduceMotion ? false : { opacity: 0, y: 12, scale: 0.98 }}
            animate={{ opacity: 1, y: 0, scale: 1 }}
            exit={reduceMotion ? undefined : { opacity: 0, y: 8, scale: 0.98 }}
            transition={{ duration: reduceMotion ? 0 : 0.2, ease: [0.16, 1, 0.3, 1] }}
            role="alert"
            className="fixed bottom-3 left-3 right-3 z-[80] flex items-start gap-3 rounded-card border border-danger/30 bg-surface p-4 shadow-2xl sm:left-auto sm:right-4 sm:w-[360px]"
          >
            <AlertCircle className="mt-0.5 h-4 w-4 shrink-0 text-danger" />
            <div className="min-w-0 flex-1">
              <div className="text-sm font-semibold text-foreground">加入研究失败</div>
              <div className="mt-1 text-xs leading-5 text-muted">请检查服务状态后重试。</div>
            </div>
            <button type="button" onClick={() => capture.reset()} className="flex h-8 w-8 shrink-0 items-center justify-center rounded-btn text-muted transition-colors hover:bg-elevated hover:text-foreground" aria-label="关闭错误提示">
              <X className="h-3.5 w-3.5" />
            </button>
          </motion.aside>
        ) : notice ? (
          <motion.aside
            initial={reduceMotion ? false : { opacity: 0, y: 12, scale: 0.98 }}
            animate={{ opacity: 1, y: 0, scale: 1 }}
            exit={reduceMotion ? undefined : { opacity: 0, y: 8, scale: 0.98 }}
            transition={{ duration: reduceMotion ? 0 : 0.2, ease: [0.16, 1, 0.3, 1] }}
            aria-label="研究账本捕获结果"
            className="fixed bottom-3 left-3 right-3 z-[80] overflow-hidden rounded-card border border-g-research/30 bg-surface shadow-2xl sm:left-auto sm:right-4 sm:w-[360px]"
          >
            <div className="flex items-start gap-3 p-4">
              <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-md bg-g-research/12 text-g-research">
                <BookMarked className="h-4 w-4" />
              </span>
              <div className="min-w-0 flex-1" aria-live="polite">
                <div className="text-sm font-semibold text-foreground">{noticeCopy(notice.action).title}</div>
                <div className="mt-1 text-xs leading-5 text-muted">{noticeCopy(notice.action).detail}</div>
              </div>
              <button type="button" onClick={() => setNotice(null)} className="flex h-8 w-8 shrink-0 items-center justify-center rounded-btn text-muted transition-colors hover:bg-elevated hover:text-foreground" aria-label="关闭提示">
                <X className="h-3.5 w-3.5" />
              </button>
            </div>
            <div className="flex items-center justify-end gap-2 border-t border-border bg-base/60 px-3 py-2">
              {notice.action !== 'duplicate' && (
                <button
                  type="button"
                  disabled={undo.isPending}
                  onClick={() => undo.mutate({ entryId: notice.entryId, captureId: notice.captureId })}
                  className="inline-flex min-h-9 items-center gap-1.5 rounded-btn px-2.5 text-xs text-secondary transition-colors hover:bg-elevated hover:text-foreground disabled:opacity-50"
                >
                  {undo.isPending ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <RotateCcw className="h-3.5 w-3.5" />}
                  撤销
                </button>
              )}
              <button
                type="button"
                onClick={() => navigate(`/research-ledger?entry=${encodeURIComponent(notice.entryId)}`)}
                className="inline-flex min-h-9 items-center gap-1.5 rounded-btn bg-g-research/12 px-3 text-xs font-medium text-g-research transition-colors hover:bg-g-research/20"
              >
                查看记录<ExternalLink className="h-3.5 w-3.5" />
              </button>
            </div>
          </motion.aside>
        ) : null}
      </AnimatePresence>
    </>
  )
}

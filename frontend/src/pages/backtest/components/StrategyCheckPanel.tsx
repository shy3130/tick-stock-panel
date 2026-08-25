import { createContext, useCallback, useContext, useMemo, useRef, useState, type ReactNode } from 'react'
import { ClipboardCheck } from 'lucide-react'
import { BADGE_TONE } from '@/components/ui/Primitives'
import {
  applyStrategyCheckStatusForRun,
  emptyStrategyCheckItems,
  emptyStrategyCheckRunState,
  strategyCheckItemsForRun,
  STRATEGY_CHECK_ITEMS,
  STRATEGY_CHECK_STATUS_LABEL,
  summarizeStrategyCheck,
  type StrategyCheckItemId,
  type StrategyCheckItems,
  type StrategyCheckReportedStatus,
  type StrategyCheckStatusHandler,
  type StrategyCheckWorkflowStatus,
} from './strategyCheck'

const STATUS_BADGE: Record<StrategyCheckWorkflowStatus, string> = {
  idle: BADGE_TONE.neutral,
  running: BADGE_TONE.accent,
  completed: BADGE_TONE.success,
  failed: BADGE_TONE.danger,
}

interface StrategyCheckContextValue {
  items: StrategyCheckItems
  report: (id: StrategyCheckItemId, status: StrategyCheckReportedStatus, error?: string) => void
}

const StrategyCheckContext = createContext<StrategyCheckContextValue | null>(null)

export function StrategyCheckProvider({ runId, children }: { runId: string; children: ReactNode }) {
  const [state, setState] = useState(emptyStrategyCheckRunState)
  const activeRunIdRef = useRef(runId)
  activeRunIdRef.current = runId
  const items = strategyCheckItemsForRun(state, runId)
  const report = useCallback((
    id: StrategyCheckItemId,
    status: StrategyCheckReportedStatus,
    error?: string,
  ) => {
    const callbackRunId = runId
    if (activeRunIdRef.current !== callbackRunId) return
    setState(prev => applyStrategyCheckStatusForRun(prev, callbackRunId, id, status, error))
  }, [runId])
  const value = useMemo<StrategyCheckContextValue>(() => ({ items, report }), [items, report])
  return <StrategyCheckContext.Provider value={value}>{children}</StrategyCheckContext.Provider>
}

/** 诊断面板向最近的体检 Provider 上报工作流状态；在其他页面复用时为 no-op。 */
export function useStrategyCheckStatus(id: StrategyCheckItemId): StrategyCheckStatusHandler {
  const context = useContext(StrategyCheckContext)
  return useCallback(
    (status, error) => { context?.report(id, status, error) },
    [context, id],
  )
}

interface Props {
  persisted: boolean
  onSelectItem: (sectionKey: StrategyCheckItemId) => void
}

/** 策略体检汇总 — 只展示工作流完成度与主 Run 固化状态, 点击项滚动到既有诊断区块 */
export function StrategyCheckPanel({ persisted, onSelectItem }: Props) {
  const items = useContext(StrategyCheckContext)?.items ?? emptyStrategyCheckItems()
  const summary = summarizeStrategyCheck(items, persisted)
  return (
    <section className="rounded-btn border border-border bg-surface/40">
      <div className="flex flex-wrap items-center justify-between gap-3 border-b border-border px-3 py-2.5">
        <div>
          <div className="flex items-center gap-1.5 text-xs font-semibold text-secondary">
            <ClipboardCheck className="h-3.5 w-3.5 text-accent" />
            策略体检
          </div>
          <div className="mt-0.5 text-[10px] leading-4 text-muted">
            这里只汇总证据完成度，不自动判定策略有效性。逐项启动下方既有诊断，本面板不重跑、不重算指标。
          </div>
        </div>
        <div className="flex flex-wrap items-center gap-1.5">
          <span className={`inline-flex items-center rounded-full border px-2 py-0.5 text-[10px] font-medium ${persisted ? BADGE_TONE.success : BADGE_TONE.warning}`}>
            {persisted ? '主 Run 已固化' : '主 Run 未固化'}
          </span>
          <span className="inline-flex items-center rounded-full border border-border bg-elevated px-2 py-0.5 font-mono text-[10px] text-secondary">
            {summary.completedCount}/{summary.total} 已完成
          </span>
        </div>
      </div>

      <div className="grid gap-2 p-3 sm:grid-cols-2">
        {STRATEGY_CHECK_ITEMS.map(def => {
          const item = items[def.id]
          return (
            <button
              key={def.id}
              type="button"
              onClick={() => onSelectItem(def.sectionKey)}
              className="flex min-h-11 items-center justify-between gap-2 rounded-input border border-border bg-elevated/40 px-3 py-2 text-left transition-colors hover:border-accent/40 hover:bg-elevated"
            >
              <span className="text-[11px] font-medium text-foreground">{def.title}</span>
              <span className={`shrink-0 rounded-full border px-2 py-0.5 text-[10px] font-medium ${STATUS_BADGE[item.status]}`}>
                {STRATEGY_CHECK_STATUS_LABEL[item.status]}
              </span>
            </button>
          )
        })}
      </div>

      {summary.failedItems.length > 0 && (
        <div className="mx-3 mb-3 space-y-1 rounded-input border border-danger/30 bg-danger/5 px-3 py-2 text-[11px] leading-5 text-danger" role="status">
          {summary.failedItems.map(item => (
            <div key={item.id}>
              {item.title}失败{item.error ? `：${item.error}` : ''}
            </div>
          ))}
        </div>
      )}
    </section>
  )
}

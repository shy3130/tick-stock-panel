// 策略体检汇总 — 纯逻辑 (无 React/指标重算), 只描述诊断工作流完成度。
//
// - 四项既有诊断: 稳健性 / 市场状态 / 成本敏感性 / 风格归因;
// - 状态只区分待运行/运行中/已完成/失败, 外加主 BacktestRun 是否已固化;
// - 不输出 pass/reject, 不重算收益或风险指标。

export const STRATEGY_CHECK_ITEM_IDS = ['robustness', 'regime', 'cost_sensitivity', 'style'] as const

export type StrategyCheckItemId = (typeof STRATEGY_CHECK_ITEM_IDS)[number]

/** 汇总面板展示用工作流状态 */
export type StrategyCheckWorkflowStatus = 'idle' | 'running' | 'completed' | 'failed'

/** 诊断面板上报给汇总的状态: 不含 idle, 初始待运行由汇总侧持有 */
export type StrategyCheckReportedStatus = Exclude<StrategyCheckWorkflowStatus, 'idle'>

export type StrategyCheckStatusHandler = (status: StrategyCheckReportedStatus, error?: string) => void

export interface StrategyCheckItemState {
  status: StrategyCheckWorkflowStatus
  error?: string
}

export type StrategyCheckItems = Record<StrategyCheckItemId, StrategyCheckItemState>

export interface StrategyCheckItemDef {
  id: StrategyCheckItemId
  title: string
  sectionKey: StrategyCheckItemId
}

export const STRATEGY_CHECK_ITEMS: readonly StrategyCheckItemDef[] = [
  { id: 'robustness', title: '稳健性', sectionKey: 'robustness' },
  { id: 'regime', title: '市场状态', sectionKey: 'regime' },
  { id: 'cost_sensitivity', title: '成本敏感性', sectionKey: 'cost_sensitivity' },
  { id: 'style', title: '风格归因', sectionKey: 'style' },
]

export const STRATEGY_CHECK_STATUS_LABEL: Record<StrategyCheckWorkflowStatus, string> = {
  idle: '待运行',
  running: '运行中',
  completed: '已完成',
  failed: '失败',
}

export function emptyStrategyCheckItems(): StrategyCheckItems {
  return {
    robustness: { status: 'idle' },
    regime: { status: 'idle' },
    cost_sensitivity: { status: 'idle' },
    style: { status: 'idle' },
  }
}

export interface StrategyCheckRunState {
  runId: string | null
  items: StrategyCheckItems
}

export function emptyStrategyCheckRunState(): StrategyCheckRunState {
  return { runId: null, items: emptyStrategyCheckItems() }
}

/** 新 Run 首帧即展示空状态，不等待 effect 清理上一 Run 的完成度 */
export function strategyCheckItemsForRun(
  state: StrategyCheckRunState,
  runId: string | null,
): StrategyCheckItems {
  return state.runId === runId ? state.items : emptyStrategyCheckItems()
}

/** 当前 Run 接收状态时绑定 runId；首次回调会从空状态开始，绝不继承上一 Run */
export function applyStrategyCheckStatusForRun(
  state: StrategyCheckRunState,
  runId: string,
  id: StrategyCheckItemId,
  status: StrategyCheckReportedStatus,
  error?: string,
): StrategyCheckRunState {
  return {
    runId,
    items: applyStrategyCheckStatus(
      state.runId === runId ? state.items : emptyStrategyCheckItems(),
      id,
      status,
      error,
    ),
  }
}

/** 把单项上报写回汇总; 非失败态清掉旧错误, 不改其余项 */
export function applyStrategyCheckStatus(
  items: StrategyCheckItems,
  id: StrategyCheckItemId,
  status: StrategyCheckReportedStatus,
  error?: string,
): StrategyCheckItems {
  return {
    ...items,
    [id]: status === 'failed'
      ? { status, error: error || undefined }
      : { status },
  }
}

export interface StrategyCheckFailedItem {
  id: StrategyCheckItemId
  title: string
  error?: string
}

export interface StrategyCheckSummary {
  total: number
  completedCount: number
  failedCount: number
  runningCount: number
  idleCount: number
  persisted: boolean
  failedItems: StrategyCheckFailedItem[]
}

/** 从四项状态 + 主 Run 固化标记汇总证据完成度, 不做投资判定 */
export function summarizeStrategyCheck(
  items: StrategyCheckItems,
  persisted: boolean,
): StrategyCheckSummary {
  let completedCount = 0
  let failedCount = 0
  let runningCount = 0
  let idleCount = 0
  const failedItems: StrategyCheckFailedItem[] = []

  for (const def of STRATEGY_CHECK_ITEMS) {
    const item = items[def.id]
    if (item.status === 'completed') completedCount += 1
    else if (item.status === 'failed') {
      failedCount += 1
      failedItems.push({ id: def.id, title: def.title, error: item.error })
    }
    else if (item.status === 'running') runningCount += 1
    else idleCount += 1
  }

  return {
    total: STRATEGY_CHECK_ITEMS.length,
    completedCount,
    failedCount,
    runningCount,
    idleCount,
    persisted,
    failedItems,
  }
}

import { useSyncExternalStore } from 'react'
import { api } from './api'
import { nextConnection, type AiConnection } from './aiStreamStatus'

/**
 * AI 财务分析 —— 全局任务/报告 store(与 UI 解耦)。
 *
 * 设计要点:
 * 1. 流式接收逻辑在这里,与弹窗组件解耦 → 用户关闭/最小化弹窗,后台流照常累积。
 * 2. useSyncExternalStore 订阅 → 任意组件(弹窗、气泡、历史面板)实时同步。
 * 3. "活跃任务"上限 MAX_ACTIVE=3:同时进行的任务最多 3 个,超出拒绝新建。
 *    (历史报告名额 MAX_REPORTS=20 在后端裁剪,与活跃任务名额分离)
 * 4. 同 symbol 已有活跃任务 → 直接聚焦那个,不新建第 2 个。
 * 5. 任务完成(收到 done 或 content 非空且流结束)→ 自动存后端 + 移入历史 + 弹窗可恢复为"历史模式"。
 */

export type Phase = 'loading' | 'streaming' | 'done' | 'error' | 'cancelled'

export interface ActiveTask {
  id: string                  // 任务 id(前端生成,与最终 report id 解耦)
  symbol: string
  name: string
  focus: string
  profileId?: string
  phase: Phase
  content: string             // 累积的 Markdown
  error: string
  meta: { summary?: string; periods?: number } | null
  /** F9: 流连接态; start→connecting, 首个 delta→open, 终态→closed */
  connection: AiConnection
  createdAt: number           // ms 时间戳
  savedReportId?: string      // 完成后存到后端的报告 id
  doneAt?: number             // 进入 done/error 态的时间戳(用于气泡过期清理)
  dismissed?: boolean         // 用户已从气泡点击查看过 → 不再在气泡显示
  attemptId?: string
}

export interface HistoryReport {
  id: string
  symbol: string
  name: string
  focus: string
  content: string
  periods?: number
  summary?: string
  created_at: string
}

const MAX_ACTIVE = 3
const abortByTask = new Map<string, AbortController>()

// ===== 全局状态 =====
let activeTasks: ActiveTask[] = []
let history: HistoryReport[] = []
let historyLoaded = false
const listeners = new Set<() => void>()

// 当前"前台"展示的任务:
//   - 活跃任务 id(正在生成/刚完成,对话框打开)
//   - 或 'history:<id>'(查看历史报告)
//   - 或 null(对话框关闭/最小化)
let activeDialogTaskId: string | null = null
let dialogMinimized = false     // 对话框是否最小化为气泡

function emit() { listeners.forEach(fn => fn()) }

function subscribe(fn: () => void) {
  listeners.add(fn)
  return () => { listeners.delete(fn) }
}

function normalizeAiError(msg: string) {
  return msg.includes('API Key') || msg.includes('api_key')
    ? 'AI 未配置或无效,请在「设置 → AI」中检查当前 AI 提供方'
    : msg
}

// 快照必须返回稳定引用:只有内容真正变化时才返回新数组/对象。
// useSyncExternalStore 用 Object.is 比较,getSnapshot 必须缓存。
let _activeSnap: ActiveTask[] = []
let _historySnap: HistoryReport[] = []
interface DialogSnap { taskId: string | null; minimized: boolean }
let _dialogSnap: DialogSnap = { taskId: activeDialogTaskId, minimized: dialogMinimized }

function rebuildSnap() {
  _activeSnap = activeTasks
  _historySnap = history
  _dialogSnap = { taskId: activeDialogTaskId, minimized: dialogMinimized }
}

function getActiveSnapshot() { return _activeSnap }
function getHistorySnapshot() { return _historySnap }

/** 只读取当前活跃任务(测试/调试用;与 reviewStore.getReviewState 对称) */
export function getActiveTasks(): ActiveTask[] {
  return activeTasks
}
function getDialogSnapshot() { return _dialogSnap }

function patchTask(id: string, patch: Partial<ActiveTask>) {
  activeTasks = activeTasks.map(t => {
    if (t.id !== id) return t
    if (t.phase === 'cancelled') {
      if (patch.dismissed === undefined) return t
      return { ...t, dismissed: patch.dismissed }
    }
    const next = { ...t, ...patch }
    // F9: 连接态随 phase 派生;cancelled 终态已在上方拦截
    if (patch.phase) next.connection = nextConnection(patch.phase, next.connection)
    if ((patch.phase === 'done' || patch.phase === 'error' || patch.phase === 'cancelled') && t.phase !== patch.phase && !next.doneAt) {
      next.doneAt = Date.now()
    }
    return next
  })
  rebuildSnap()
  emit()
}

// ===== 公开:查询 hooks =====

export function useBubbleTasks(): ActiveTask[] {
  const all = useSyncExternalStore(subscribe, getActiveSnapshot, () => [])
  // 同时订阅对话框状态:最小化/打开/关闭会改变气泡可见性,需独立触发重渲染。
  // (否则最小化时 activeTasks 引用未变,useSyncExternalStore 不会重渲染,胶囊不出现)
  useSyncExternalStore(subscribe, getDialogSnapshot, () => ({ taskId: null, minimized: false }))
  const ds = _dialogSnap
  return all.filter(t => {
    // 进行中:始终显示(dismissed 仅作用于完成态,不影响生成中的任务再次最小化)
    if (t.phase === 'loading' || t.phase === 'streaming') {
      // 除非对话框正打开看着它(非最小化)
      return !(ds.taskId === t.id && !ds.minimized)
    }
    // 完成/失败态:常驻显示,直到用户主动点击查看(dismissed)。
    // 不设自动过期 —— 胶囊是持续可见的状态指示器,历史报告列表是查看入口。
    if (t.dismissed) return false                       // 用户已点击查看过 → 移除
    if (!ds.minimized && ds.taskId === t.id) return false  // 对话框正展示 → 不重复
    return true
  })
}

/** 兼容旧调用名(Layout 等处可能引用) */
export const useActiveTasks = useBubbleTasks

export function useHistoryReports(): { reports: HistoryReport[]; loaded: boolean } {
  const reports = useSyncExternalStore(subscribe, getHistorySnapshot, () => [])
  return { reports, loaded: historyLoaded }
}

export function useDialogState() {
  return useSyncExternalStore(subscribe, getDialogSnapshot, () => ({ taskId: null, minimized: false }))
}

/** 当前对话框要展示的任务(活跃或历史),null=未打开。 */
export function useDialogTask(): { task: ActiveTask | HistoryReport | null; mode: 'active' | 'history' | null } {
  const ds = useDialogState()
  const active = useSyncExternalStore(subscribe, getActiveSnapshot, () => [])
  const hist = useSyncExternalStore(subscribe, getHistorySnapshot, () => [])
  if (!ds.taskId) return { task: null, mode: null }
  if (ds.taskId.startsWith('history:')) {
    const rid = ds.taskId.slice('history:'.length)
    return { task: hist.find(r => r.id === rid) ?? null, mode: 'history' }
  }
  return { task: active.find(t => t.id === ds.taskId) ?? null, mode: 'active' }
}

// ===== 公开:动作 =====

/** 拉取历史报告(惰性,首次需要时调用)。 */
export async function loadHistory(): Promise<void> {
  try {
    const res = await api.financialReportsList()
    history = res.reports ?? []
    historyLoaded = true
    rebuildSnap()
    emit()
  } catch {
    // 静默失败,列表会显示空
  }
}

/**
 * 查询某只股票最近一次的历史分析报告(用于二次确认提示)。
 * 若历史未加载,先触发拉取。
 * @returns 最近一条报告,或 null
 */
export async function findLatestHistoryReport(symbol: string): Promise<HistoryReport | null> {
  if (!historyLoaded) await loadHistory()
  // history 已按 created_at 降序,取第一条匹配
  return history.find(r => r.symbol === symbol) ?? null
}

/**
 * 启动一个新的 AI 分析任务。
 * @returns 任务 id;若超出上限或已有活跃任务,返回 { error }。
 */
export async function startAnalysis(symbol: string, name: string, focus = '', profileId?: string): Promise<{ id?: string; error?: string }> {
  // 同 symbol 已有活跃任务 → 直接聚焦它
  const existing = activeTasks.find(t => t.symbol === symbol && (t.phase === 'loading' || t.phase === 'streaming'))
  if (existing) {
    activeDialogTaskId = existing.id
    dialogMinimized = false
    rebuildSnap()
    emit()
    return { id: existing.id }
  }
  // 上限检查
  const ongoing = activeTasks.filter(t => t.phase === 'loading' || t.phase === 'streaming')
  if (ongoing.length >= MAX_ACTIVE) {
    return { error: `同时进行的分析任务不能超过 ${MAX_ACTIVE} 个,请等待现有任务完成` }
  }

  const id = `task_${Date.now()}_${Math.random().toString(36).slice(2, 6)}`
  const task: ActiveTask = {
    id, symbol, name, focus, profileId,
    phase: 'loading', content: '', error: '',
    meta: null, connection: 'connecting', createdAt: Date.now(),
  }
  activeTasks = [...activeTasks, task]
  activeDialogTaskId = id
  dialogMinimized = false
  rebuildSnap()
  emit()

  runStream(id, symbol, focus, profileId)
  return { id }
}

async function runStream(id: string, symbol: string, focus: string, profileId?: string) {
  const ac = new AbortController()
  abortByTask.set(id, ac)
  try {
    let firstDelta = true
    for await (const chunk of api.financialAnalyzeStream(symbol, focus, profileId, ac.signal)) {
      const cur = activeTasks.find(t => t.id === id)
      if (!cur || cur.phase === 'cancelled' || ac.signal.aborted) return
      switch (chunk.type) {
        case 'attempt':
          if (chunk.attempt_id) patchTask(id, { attemptId: chunk.attempt_id })
          break
        case 'meta':
          patchTask(id, { meta: { summary: chunk.summary, periods: chunk.periods } })
          break
        case 'delta':
          if (firstDelta) { patchTask(id, { phase: 'streaming' }); firstDelta = false }
          patchTask(id, { content: cur.content + (chunk.content ?? '') })
          break
        case 'error':
          patchTask(id, { phase: 'error', error: chunk.message ?? '分析失败' })
          return
        case 'done':
          patchTask(id, { phase: 'done' })
          break
      }
    }
    const final = activeTasks.find(t => t.id === id)
    if (final && final.phase !== 'error' && final.phase !== 'cancelled' && !ac.signal.aborted && final.content) {
      try {
        const res = await api.financialReportSave({
          symbol: final.symbol,
          name: final.name,
          focus: final.focus,
          content: final.content,
          periods: final.meta?.periods,
          summary: final.meta?.summary ?? '',
        })
        if (res.report) {
          patchTask(id, { savedReportId: res.report.id })
          history = [res.report, ...history.filter(r => r.id !== res.report.id)]
          historyLoaded = true
          rebuildSnap()
          emit()
        }
      } catch {
        // 持久化失败不影响前端已展示的内容
      }
    }
  } catch (e: any) {
    const cur = activeTasks.find(t => t.id === id)
    if (cur?.phase === 'cancelled' || ac.signal.aborted) {
      if (cur && cur.phase !== 'cancelled') patchTask(id, { phase: 'cancelled', error: '已取消' })
      return
    }
    const msg = String(e?.message ?? '分析失败')
    patchTask(id, {
      phase: 'error',
      error: normalizeAiError(msg),
    })
  } finally {
    abortByTask.delete(id)
  }
}

export async function cancelAnalysis(id: string): Promise<void> {
  const t = activeTasks.find(x => x.id === id)
  if (!t || (t.phase !== 'loading' && t.phase !== 'streaming')) return
  abortByTask.get(id)?.abort()
  if (t.attemptId) {
    try { await api.cancelAgentAttempt(t.attemptId) } catch { /* 取消失败仍以本地中止为准 */ }
  }
  patchTask(id, { phase: 'cancelled', error: '已取消' })
}


/** 打开对话框(活跃任务或历史报告)。 */
export function openDialog(taskId: string) {
  activeDialogTaskId = taskId
  dialogMinimized = false
  rebuildSnap()
  emit()
}

/** 最小化对话框 → 变成气泡。 */
export function minimizeDialog() {
  dialogMinimized = true
  rebuildSnap()
  emit()
}

/** 关闭对话框。 */
export function closeDialog() {
  activeDialogTaskId = null
  dialogMinimized = false
  rebuildSnap()
  emit()
}

export function restoreDialog(taskId: string) {
  const t = activeTasks.find(x => x.id === taskId)
  if (t && (t.phase === 'done' || t.phase === 'error' || t.phase === 'cancelled')) {
    patchTask(taskId, { dismissed: true })
  }
  activeDialogTaskId = taskId
  dialogMinimized = false
  rebuildSnap()
  emit()
}

/** 重试一个失败/已完成的任务(以新任务方式重新分析)。 */
export async function retryAnalysis(task: { symbol: string; name: string; focus: string; profileId?: string }): Promise<{ error?: string }> {
  return startAnalysis(task.symbol, task.name, task.focus, task.profileId)
}


/** 删除历史报告。 */
export async function deleteReport(reportId: string): Promise<void> {
  try {
    await api.financialReportDelete(reportId)
    history = history.filter(r => r.id !== reportId)
    rebuildSnap()
    emit()
  } catch {
    // 静默
  }
}

/** 打开历史报告到对话框。 */
export function openHistoryReport(reportId: string) {
  activeDialogTaskId = `history:${reportId}`
  dialogMinimized = false
  rebuildSnap()
  emit()
}

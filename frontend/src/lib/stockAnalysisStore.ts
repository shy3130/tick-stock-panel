import { useSyncExternalStore } from 'react'
import { api, type PriceLevel, type LevelType } from './api'

/**
 * AI 个股分析 —— 全局任务/报告 store(与 aiReportStore 解耦、并行存在)。
 *
 * 与财务分析 store 的区别:
 *  - 独立的 activeTasks / history 状态池(不共享 3 并发上限)
 *  - meta 额外带 levels(关键价位),供图表回放
 *  - 状态文案在胶囊组件里用「蓝」色系区分(财务用紫)
 *
 * 设计同 aiReportStore:流式接收逻辑在此,与弹窗解耦 → 关闭弹窗后台照常累积。
 */

export type Phase = 'loading' | 'streaming' | 'done' | 'error'

export interface ActiveTask {
  id: string
  symbol: string
  name: string
  focus: string
  phase: Phase
  content: string
  error: string
  meta: {
    summary?: string
    levels?: Record<LevelType, PriceLevel[]>
    close?: number | null
  } | null
  createdAt: number
  savedReportId?: string
  doneAt?: number
  dismissed?: boolean
}

export interface HistoryReport {
  id: string
  symbol: string
  name: string
  focus: string
  content: string
  summary?: string
  close?: number | null
  levels?: Record<LevelType, PriceLevel[]>
  created_at: string
}

const MAX_ACTIVE = 3

let activeTasks: ActiveTask[] = []
let history: HistoryReport[] = []
let historyLoaded = false
const listeners = new Set<() => void>()

let activeDialogTaskId: string | null = null
let dialogMinimized = false

function emit() { listeners.forEach(fn => fn()) }
function subscribe(fn: () => void) { listeners.add(fn); return () => { listeners.delete(fn) } }

function normalizeAiError(msg: string) {
  return msg.includes('API Key') || msg.includes('api_key')
    ? 'AI 未配置或无效,请在「设置 → AI」中检查当前 AI 提供方'
    : msg
}

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
function getDialogSnapshot() { return _dialogSnap }

function patchTask(id: string, patch: Partial<ActiveTask>) {
  activeTasks = activeTasks.map(t => {
    if (t.id !== id) return t
    const next = { ...t, ...patch }
    if ((patch.phase === 'done' || patch.phase === 'error') && t.phase !== patch.phase && !next.doneAt) {
      next.doneAt = Date.now()
    }
    return next
  })
  rebuildSnap()
  emit()
}

// ===== 查询 hooks =====

export function useBubbleTasks(): ActiveTask[] {
  const all = useSyncExternalStore(subscribe, getActiveSnapshot, () => [])
  useSyncExternalStore(subscribe, getDialogSnapshot, () => ({ taskId: null, minimized: false }))
  const ds = _dialogSnap
  return all.filter(t => {
    if (t.phase === 'loading' || t.phase === 'streaming') {
      return !(ds.taskId === t.id && !ds.minimized)
    }
    if (t.dismissed) return false
    if (!ds.minimized && ds.taskId === t.id) return false
    return true
  })
}

export function useHistoryReports(): { reports: HistoryReport[]; loaded: boolean } {
  const reports = useSyncExternalStore(subscribe, getHistorySnapshot, () => [])
  return { reports, loaded: historyLoaded }
}

export function useDialogState() {
  return useSyncExternalStore(subscribe, getDialogSnapshot, () => ({ taskId: null, minimized: false }))
}

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

// ===== 动作 =====

export async function loadHistory(): Promise<void> {
  try {
    const res = await api.stockAnalysisReportsList()
    history = res.reports ?? []
    historyLoaded = true
    rebuildSnap()
    emit()
  } catch { /* 静默 */ }
}

export async function findLatestHistoryReport(symbol: string): Promise<HistoryReport | null> {
  if (!historyLoaded) await loadHistory()
  return history.find(r => r.symbol === symbol) ?? null
}

/**
 * 查询某只股票【当日】是否已生成过分析报告(用于二次确认)。
 * 判断依据:created_at 的日期部分 == 本地今天。
 * @returns 当天最近一条报告,或 null
 */
export async function findTodayReport(symbol: string): Promise<HistoryReport | null> {
  if (!historyLoaded) await loadHistory()
  const today = new Date().toISOString().slice(0, 10)  // YYYY-MM-DD
  return history.find(r => r.symbol === symbol && (r.created_at ?? '').slice(0, 10) === today) ?? null
}

export async function startAnalysis(symbol: string, name: string, focus = '', profileId?: string): Promise<{ id?: string; error?: string }> {
  const existing = activeTasks.find(t => t.symbol === symbol && (t.phase === 'loading' || t.phase === 'streaming'))
  if (existing) {
    activeDialogTaskId = existing.id
    dialogMinimized = false
    rebuildSnap()
    emit()
    return { id: existing.id }
  }
  const ongoing = activeTasks.filter(t => t.phase === 'loading' || t.phase === 'streaming')
  if (ongoing.length >= MAX_ACTIVE) {
    return { error: `同时进行的个股分析任务不能超过 ${MAX_ACTIVE} 个,请等待现有任务完成` }
  }

  const id = `stask_${Date.now()}_${Math.random().toString(36).slice(2, 6)}`
  const task: ActiveTask = {
    id, symbol, name, focus,
    phase: 'loading', content: '', error: '',
    meta: null, createdAt: Date.now(),
  }
  activeTasks = [...activeTasks, task]
  activeDialogTaskId = id
  dialogMinimized = false
  rebuildSnap()
  emit()

  runStream(id, symbol, name, focus, profileId)
  return { id }
}

async function runStream(id: string, symbol: string, _name: string, focus: string, profileId?: string) {
  try {
    let firstDelta = true
    for await (const chunk of api.stockAnalyzeStream(symbol, focus, profileId)) {
      const cur = activeTasks.find(t => t.id === id)
      if (!cur) return
      switch (chunk.type) {
        case 'meta':
          patchTask(id, { meta: { summary: chunk.summary, levels: chunk.levels, close: chunk.close } })
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
    if (final && final.phase !== 'error') {
      // 兜底:流正常结束但从未收到 delta(后端在生成内容前异常断流)→ 标记失败,避免卡死
      if (!final.content) {
        patchTask(id, { phase: 'error', error: '分析未返回内容(后端可能异常中断),请重试' })
        return
      }
      try {
        const res = await api.stockAnalysisReportSave({
          symbol: final.symbol, name: final.name, focus: final.focus,
          content: final.content, summary: final.meta?.summary ?? '',
          close: final.meta?.close ?? null, levels: final.meta?.levels,
        })
        if (res.report) {
          patchTask(id, { savedReportId: res.report.id })
          history = [res.report, ...history.filter(r => r.id !== res.report.id)]
          historyLoaded = true
          rebuildSnap()
          emit()
        }
      } catch { /* 持久化失败不影响展示 */ }
    }
  } catch (e: any) {
    const msg = String(e?.message ?? '分析失败')
    patchTask(id, {
      phase: 'error',
      error: normalizeAiError(msg),
    })
  }
}

export function openDialog(taskId: string) {
  activeDialogTaskId = taskId; dialogMinimized = false; rebuildSnap(); emit()
}
export function minimizeDialog() {
  dialogMinimized = true; rebuildSnap(); emit()
}
export function closeDialog() {
  activeDialogTaskId = null; dialogMinimized = false; rebuildSnap(); emit()
}
export function restoreDialog(taskId: string) {
  const t = activeTasks.find(x => x.id === taskId)
  if (t && (t.phase === 'done' || t.phase === 'error')) {
    patchTask(taskId, { dismissed: true })
  }
  activeDialogTaskId = taskId; dialogMinimized = false; rebuildSnap(); emit()
}
export async function retryAnalysis(task: { symbol: string; name: string; focus: string }): Promise<{ error?: string }> {
  return startAnalysis(task.symbol, task.name, task.focus)
}
export async function deleteReport(reportId: string): Promise<void> {
  try {
    await api.stockAnalysisReportDelete(reportId)
    history = history.filter(r => r.id !== reportId)
    rebuildSnap()
    emit()
  } catch { /* 静默 */ }
}
export function openHistoryReport(reportId: string) {
  activeDialogTaskId = `history:${reportId}`; dialogMinimized = false; rebuildSnap(); emit()
}

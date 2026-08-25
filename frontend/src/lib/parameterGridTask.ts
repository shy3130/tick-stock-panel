import { useSyncExternalStore } from 'react'
import { api, type ParameterGridLaunchResponse, type ParameterGridRequest } from './api'
import { storage } from './storage'


export interface ParameterGridTask {
  experimentId: string | null
  isLaunching: boolean
  revision: number
}

export interface ParameterGridLaunchOutcome {
  launched: ParameterGridLaunchResponse
  adopted: boolean
}

function restoreExperimentId(): string | null {
  const saved = storage.parameterGridLastExperimentId.get(null)
  return typeof saved === 'string' && saved ? saved : null
}

let revision = 0
let launchSeq = 0
let current: ParameterGridTask = {
  experimentId: restoreExperimentId(),
  isLaunching: false,
  revision,
}
const listeners = new Set<() => void>()
const serverSnapshot: ParameterGridTask = {
  experimentId: null,
  isLaunching: false,
  revision: 0,
}

function emit(): void {
  listeners.forEach(listener => listener())
}

function update(
  next: Omit<ParameterGridTask, 'revision'>,
  persistExperimentId = false,
): void {
  current = { ...next, revision: ++revision }
  if (persistExperimentId) storage.parameterGridLastExperimentId.set(next.experimentId)
  emit()
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener)
  return () => listeners.delete(listener)
}

function getSnapshot(): ParameterGridTask {
  return current
}

function getServerSnapshot(): ParameterGridTask {
  return serverSnapshot
}

/**
 * 启动服务端参数网格实验。实验 ID 属于模块级状态，因此发起请求后路由切换仍可
 * 恢复最新实验。仅当前最新 launch 响应可以更新恢复 ID，避免旧组件异步响应覆盖。
 */
export async function startParameterGridExperiment(
  payload: ParameterGridRequest,
): Promise<ParameterGridLaunchOutcome> {
  const sequence = ++launchSeq
  const previousExperimentId = current.experimentId
  update({ experimentId: null, isLaunching: true })

  try {
    const launched = await api.parameterGridLaunch(payload)
    if (sequence !== launchSeq) return { launched, adopted: false }
    update({ experimentId: launched.experiment_id, isLaunching: false }, true)
    return { launched, adopted: true }
  } catch (error) {
    if (sequence === launchSeq) {
      update({ experimentId: previousExperimentId, isLaunching: false })
    }
    throw error
  }
}

/** Clear a missing or explicitly discarded experiment and invalidate in-flight launches. */
export function clearParameterGridExperiment(): void {
  launchSeq += 1
  update({ experimentId: null, isLaunching: false }, true)
}

export function clearParameterGridExperimentIfCurrent(
  experimentId: string,
  expectedRevision: number,
): boolean {
  if (current.experimentId !== experimentId || current.revision !== expectedRevision) return false
  clearParameterGridExperiment()
  return true
}

/** F7: 从实验列表恢复指定实验 — 写恢复键并更新模块状态, 面板据此重新拉取详情。 */
export function openParameterGridExperiment(experimentId: string): void {
  launchSeq += 1
  update({ experimentId, isLaunching: false }, true)
}

export function getParameterGridTask(): ParameterGridTask {
  return current
}

export function useParameterGridTask(): ParameterGridTask {
  return useSyncExternalStore(subscribe, getSnapshot, getServerSnapshot)
}

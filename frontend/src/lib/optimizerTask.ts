import { useSyncExternalStore } from 'react'
import { api, type OptimizerLaunchResponse, type OptimizerRequest } from './api'
import { storage } from './storage'

export interface OptimizerTask {
  experimentId: string | null
  isLaunching: boolean
  revision: number
}

function restoreExperimentId(): string | null {
  const saved = storage.optimizerLastExperimentId.get(null)
  return typeof saved === 'string' && saved ? saved : null
}

let revision = 0
let launchSeq = 0
let current: OptimizerTask = {
  experimentId: restoreExperimentId(),
  isLaunching: false,
  revision,
}
const listeners = new Set<() => void>()
const serverSnapshot: OptimizerTask = { experimentId: null, isLaunching: false, revision: 0 }

function emit(): void {
  listeners.forEach(listener => listener())
}

function update(next: Omit<OptimizerTask, 'revision'>, persist = false): void {
  current = { ...next, revision: ++revision }
  if (persist) storage.optimizerLastExperimentId.set(next.experimentId)
  emit()
}

export async function startOptimizerExperiment(payload: OptimizerRequest): Promise<OptimizerLaunchResponse> {
  const sequence = ++launchSeq
  const previous = current.experimentId
  update({ experimentId: null, isLaunching: true })
  try {
    const launched = await api.optimizerLaunch(payload)
    if (sequence !== launchSeq) return launched
    update({ experimentId: launched.experiment_id, isLaunching: false }, true)
    return launched
  } catch (error) {
    if (sequence === launchSeq) update({ experimentId: previous, isLaunching: false })
    throw error
  }
}

export function clearOptimizerExperiment(): void {
  launchSeq += 1
  update({ experimentId: null, isLaunching: false }, true)
}

export function clearOptimizerExperimentIfCurrent(experimentId: string, expectedRevision: number): boolean {
  if (current.experimentId !== experimentId || current.revision !== expectedRevision) return false
  clearOptimizerExperiment()
  return true
}

export function getOptimizerTask(): OptimizerTask {
  return current
}

export function useOptimizerTask(): OptimizerTask {
  return useSyncExternalStore(
    listener => {
      listeners.add(listener)
      return () => listeners.delete(listener)
    },
    () => current,
    () => serverSnapshot,
  )
}

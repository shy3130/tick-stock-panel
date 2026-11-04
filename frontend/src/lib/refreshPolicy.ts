import { useSyncExternalStore } from 'react'
import type { ScreenerCachedSummary } from './api'

function subscribeVisibility(listener: () => void) {
  document.addEventListener('visibilitychange', listener)
  return () => document.removeEventListener('visibilitychange', listener)
}

export function useDocumentVisible(): boolean {
  return useSyncExternalStore(subscribeVisibility, () => document.visibilityState !== 'hidden', () => true)
}

// Sample once per window: changing the interval on every render resets Query's
// timer, so frequent quote updates can indefinitely postpone the next poll.
const pollingWindowJitter = Math.random()

/** SSE drives updates; periodic reads remain a fallback for missed notifications. */
export function pollingInterval(baseMs: number, live: boolean, visible: boolean, random = () => pollingWindowJitter): number | false {
  if (!visible) return false
  return Math.round((live ? Math.max(60_000, baseMs) : baseMs) * (1 + random() * 0.1))
}

export function retryDelay(attempt: number, retryAfterMs?: number, random = Math.random): number {
  const base = retryAfterMs ?? Math.min(1000 * 2 ** attempt, 30_000)
  return Math.round(base * (1 + random() * 0.1))
}

export function createRevisionTracker() {
  const revisions = new Map<string, string>()
  return {
    accept(channel: string, data: string): boolean {
      try {
        const revision = JSON.parse(data).revision
        if (typeof revision !== 'string' || !revision) return true
        if (revisions.get(channel) === revision) return false
        revisions.set(channel, revision)
      } catch { /* Older servers and malformed notifications still trigger a safe read. */ }
      return true
    },
  }
}

export function pendingStrategyIds(
  pending: { ids: string[]; startedAt: number; taskId?: string },
  summary: Pick<ScreenerCachedSummary, 'results' | 'task'>,
  asOf: string,
): string[] | null {
  if (pending.taskId) {
    // Placeholder data may belong to the previous query while a new task's
    // status is loading. Recent result timestamps cannot complete that task.
    if (summary.task === null) return null
    if (!summary.task || summary.task.id !== pending.taskId) return pending.ids
    return summary.task.complete ? [] : summary.task.pending
  }
  return pending.ids.filter(id => {
    const result = summary.results[id]
    if (!result || result.as_of !== asOf) return true
    const availableAt = result.available_at ?? result.computed_at
    return availableAt != null && availableAt < pending.startedAt - 2000
  })
}

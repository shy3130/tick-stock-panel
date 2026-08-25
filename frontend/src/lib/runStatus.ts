export interface ExperimentRuntime {
  stage?: string
  label?: string
  current?: string
  completed?: number
  total?: number
  failed?: number
  ok?: number
  started_at?: string
  updated_at?: string
  elapsed_ms?: number
  eta_ms?: number | null
  last_elapsed_ms?: number
}

/** SSE 任务连接状态: connecting=首次连接, open=已连上, reconnecting=断线自动重连中, closed=已断开 */
export type RunConnectionState = 'connecting' | 'open' | 'reconnecting' | 'closed'

export function formatClock(ms: number): string {
  if (!Number.isFinite(ms) || ms < 0) return '—'
  if (ms < 1000) return `${Math.round(ms)}ms`
  const total = Math.round(ms / 1000)
  if (total < 60) return `${total} 秒`
  const minutes = Math.floor(total / 60)
  const seconds = total % 60
  if (minutes < 60) return seconds ? `${minutes} 分 ${seconds} 秒` : `${minutes} 分`
  const hours = Math.floor(minutes / 60)
  const rest = minutes % 60
  return rest ? `${hours} 小时 ${rest} 分` : `${hours} 小时`
}

export function estimateEtaMs(elapsedMs: number, completed: number, total: number): number | null {
  if (!(elapsedMs > 0) || !(completed > 0) || !(total > completed)) return null
  return (elapsedMs / completed) * (total - completed)
}

export function formatRate(completed: number, elapsedMs: number): string | null {
  if (!(completed > 0) || !(elapsedMs > 0)) return null
  const perMin = completed / (elapsedMs / 60_000)
  if (perMin >= 10) return `${perMin.toFixed(0)} /分`
  if (perMin >= 1) return `${perMin.toFixed(1)} /分`
  return `${(perMin * 60).toFixed(1)} /时`
}

export function elapsedSince(startedAt: string | null | undefined, now = Date.now()): number | null {
  if (!startedAt) return null
  const started = Date.parse(startedAt)
  if (!Number.isFinite(started)) return null
  return Math.max(0, now - started)
}

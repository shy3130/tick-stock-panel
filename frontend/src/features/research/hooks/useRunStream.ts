import { useEffect, useRef, useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { openRunStream, readStreamMessage } from '../api/runs'
import { isActiveJob, type JobStatus } from '../model/status'
import type { RunStreamEvent } from '../model/run'
import { researchKeys } from '../queryKeys'

export interface RunStreamState {
  jobStatus: JobStatus | null
  stage: string | null
  ratio: number | null
  message: string | null
  connection: 'idle' | 'open' | 'reconnecting' | 'closed'
  interrupted: boolean
  lastEvent: RunStreamEvent | null
}

const TERMINAL: JobStatus[] = ['completed', 'failed', 'cancelled', 'interrupted']

export function useRunStream(runId: string | undefined, initialStatus: JobStatus | null) {
  const qc = useQueryClient()
  const [state, setState] = useState<RunStreamState>({
    jobStatus: initialStatus,
    stage: null,
    ratio: null,
    message: null,
    connection: 'idle',
    interrupted: initialStatus === 'interrupted',
    lastEvent: null,
  })
  const lastEventId = useRef<string | null>(null)
  const shouldStream = Boolean(runId) && isActiveJob(initialStatus)

  useEffect(() => {
    if (!runId || !shouldStream) {
      setState((prev) => ({ ...prev, jobStatus: initialStatus, connection: 'closed' }))
      return
    }

    let closed = false
    let source: EventSource | null = null
    let retryTimer: number | null = null
    let attempt = 0

    const invalidate = () => {
      void qc.invalidateQueries({ queryKey: researchKeys.run(runId) })
      void qc.invalidateQueries({ queryKey: researchKeys.all })
    }

    const connect = () => {
      if (closed) return
      setState((prev) => ({ ...prev, connection: attempt === 0 ? 'open' : 'reconnecting' }))
      source = openRunStream(runId, lastEventId.current ?? undefined)
      source.onopen = () => {
        if (closed) return
        attempt = 0
        setState((prev) => ({ ...prev, connection: 'open' }))
      }
      const types = ['snapshot', 'progress', 'warning', 'interrupted', 'completed', 'failed', 'cancelled', 'heartbeat']
      for (const type of types) {
        source.addEventListener(type, ((event: MessageEvent) => {
          const parsed = readStreamMessage(type, event)
          if (parsed.id) lastEventId.current = parsed.id
          const nextStatus = parsed.job_status
            ?? (type === 'completed' ? 'completed' : type === 'failed' ? 'failed' : type === 'cancelled' ? 'cancelled' : type === 'interrupted' ? 'interrupted' : null)
          const terminal = type === 'completed' || type === 'failed' || type === 'cancelled' || type === 'interrupted'
            || (nextStatus != null && TERMINAL.includes(nextStatus))
          setState((prev) => ({
            jobStatus: nextStatus ?? prev.jobStatus,
            stage: parsed.stage ?? prev.stage,
            ratio: parsed.ratio ?? prev.ratio,
            message: parsed.message ?? prev.message,
            connection: terminal ? 'closed' : 'open',
            interrupted: type === 'interrupted' || nextStatus === 'interrupted' || prev.interrupted,
            lastEvent: parsed,
          }))
          if (terminal) {
            invalidate()
            source?.close()
          }
        }) as EventListener)
      }
      source.onerror = () => {
        if (closed) return
        setState((prev) => ({ ...prev, connection: 'reconnecting' }))
        if (!source || source.readyState !== EventSource.CLOSED) return
        source.close()
        attempt += 1
        retryTimer = window.setTimeout(connect, Math.min(8000, 500 * 2 ** Math.min(attempt, 4)))
      }
    }

    connect()
    return () => {
      closed = true
      if (retryTimer != null) window.clearTimeout(retryTimer)
      source?.close()
    }
    // stream identity is runId + whether the job is still active
  }, [runId, shouldStream, qc, initialStatus])

  return state
}

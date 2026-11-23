import { useSyncExternalStore } from 'react'

// One local clock per window; no backend polling or timer per table row.
const listeners = new Set<() => void>()
let timer: ReturnType<typeof setInterval> | undefined
let tick = Date.now()
function subscribe(listener: () => void) {
  listeners.add(listener)
  if (!timer) {
    tick = Date.now()
    timer = setInterval(() => { tick = Date.now(); listeners.forEach(fn => fn()) }, 15_000)
  }
  return () => {
    listeners.delete(listener)
    if (!listeners.size) { clearInterval(timer); timer = undefined }
  }
}

export function useQuoteClock(): number {
  return useSyncExternalStore(subscribe, () => tick, () => tick)
}

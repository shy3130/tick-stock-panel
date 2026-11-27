import type { QueryClient } from '@tanstack/react-query'

/** Yield to mounted foreground queries, including charts that mount after daily K arrives. */
export function scheduleNeighborPrefetch(
  client: QueryClient,
  symbol: string,
  tasks: Array<() => Promise<unknown>>,
  delayMs = 0,
): () => void {
  let stopped = false
  let running = false
  let next = 0
  let timer: ReturnType<typeof setTimeout> | undefined

  const schedule = (waitMs = 0) => {
    if (stopped || running || timer !== undefined || next >= tasks.length) return
    timer = setTimeout(() => {
      timer = undefined
      if (stopped || client.isFetching({
        predicate: query => query.isActive() && query.queryKey.includes(symbol),
      })) return
      running = true
      void Promise.resolve().then(tasks[next++]).catch(() => {}).finally(() => {
        running = false
        schedule()
      })
    }, waitMs)
  }
  // 前台查询结束后保留 delayMs 让路窗口，避免插进指标防抖空档；连续预取任务间仍即时衔接。
  const unsubscribe = client.getQueryCache().subscribe(() => schedule(delayMs))
  schedule(delayMs)
  return () => {
    stopped = true
    clearTimeout(timer)
    unsubscribe()
  }
}

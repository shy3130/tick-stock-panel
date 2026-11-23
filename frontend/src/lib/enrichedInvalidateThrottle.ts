/**
 * watchlist-enriched 前缀 invalidate 的节流器。
 *
 * SSE quotes_updated 按行情轮询节奏到达 (expert 1s / pro 2-3s), 每次都整表
 * invalidate → 自选页每秒重拉 N×64 字段并全量重建派生。SSE 消息体不含逐
 * symbol 行情, 无法客户端 patch → 用节流换频次: leading 立即发 (首拍不延迟),
 * 窗口内的后续触发由 trailing 定时器兜底 (最终一定刷新, 不丢尾)。
 */

export interface EnrichedInvalidateThrottler {
  /** 一次触发; invalidate 由节流器决定立即或延迟调用 */
  fire: (invalidate: () => void) => void
  /** 取消挂起的 trailing 定时器 (卸载/断流时调用) */
  cancel: () => void
}

export function createEnrichedThrottler(
  intervalMs = 5000,
  now: () => number = Date.now,
): EnrichedInvalidateThrottler {
  let last = 0
  let timer: ReturnType<typeof setTimeout> | null = null
  return {
    fire(invalidate) {
      const t = now()
      if (t - last >= intervalMs) {
        last = t
        invalidate()
        return
      }
      if (timer != null) return
      timer = setTimeout(() => {
        timer = null
        last = now()
        invalidate()
      }, Math.max(0, intervalMs - (t - last)))
    },
    cancel() {
      if (timer != null) {
        clearTimeout(timer)
        timer = null
      }
    },
  }
}

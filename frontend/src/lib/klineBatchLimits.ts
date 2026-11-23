/**
 * 批量 K 线单请求标的上限 —— 前后端唯一权威值。
 *
 * 后端 daily-batch / minute-batch 各自校验的阈值, 同时就是前端分块大小。
 * 两处共用一个数才不会出现「后端调小、前端仍按旧值分块 → 全站 400」。
 *
 * 值来自用户偏好 (系统设置 → 批量请求)。Layout 在渲染期把偏好灌进来:
 * 渲染先于子组件 effect, 首次请求就已用上正确值; 设置页保存成功后也立即回写,
 * 无需等偏好查询重新拉取。
 *
 * 模块级可变状态 (而非 Context) 的原因: 分块助手是纯函数, 在 react-query
 * queryFn 内调用, 不走组件树。
 */

export const DAILY_BATCH_SYMBOLS_MIN = 50
export const DAILY_BATCH_SYMBOLS_MAX = 5000
export const MINUTE_BATCH_SYMBOLS_MIN = 50
export const MINUTE_BATCH_SYMBOLS_MAX = 2000

/** 后端默认值 (preferences.get_*_batch_symbols 的 fallback) */
const DEFAULTS = { daily: 400, minute: 400 }

let dailyLimit = DEFAULTS.daily
let minuteLimit = DEFAULTS.minute

function clamp(v: number, lo: number, hi: number): number {
  if (!Number.isFinite(v)) return lo
  return Math.max(lo, Math.min(hi, Math.trunc(v)))
}

/** 同步用户偏好; 缺省字段保持当前值 (偏好查询尚未返回时不覆盖)。 */
export function setKlineBatchLimits(next: { daily?: number; minute?: number }): void {
  if (typeof next.daily === 'number') {
    dailyLimit = clamp(next.daily, DAILY_BATCH_SYMBOLS_MIN, DAILY_BATCH_SYMBOLS_MAX)
  }
  if (typeof next.minute === 'number') {
    minuteLimit = clamp(next.minute, MINUTE_BATCH_SYMBOLS_MIN, MINUTE_BATCH_SYMBOLS_MAX)
  }
}

/** daily-batch 单请求标的数 (= 分块大小)。 */
export function getDailyBatchChunkSize(): number {
  return dailyLimit
}

/** minute-batch 单请求标的数 (= 分块大小)。 */
export function getMinuteBatchChunkSize(): number {
  return minuteLimit
}

/** 测试用: 恢复默认值, 避免用例间相互污染。 */
export function resetKlineBatchLimits(): void {
  dailyLimit = DEFAULTS.daily
  minuteLimit = DEFAULTS.minute
}

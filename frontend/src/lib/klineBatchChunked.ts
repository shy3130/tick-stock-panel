import { api, type KlineRow } from '@/lib/api'
import { getDailyBatchChunkSize } from '@/lib/klineBatchLimits'

// 日K批量分块助手: 后端 /api/kline/daily-batch 单次 symbols 上限由用户偏好
// 配置 (系统设置 → 批量请求)。分块大小直接读该上限 —— 与后端校验同源,
// 不会出现「后端调小、前端仍按旧值分块」导致的必然 400。
// 调用方 (自选/选股/中签页) 统一走这里分块串行拉取并合并, 响应形状与单次调用一致。

export async function fetchDailyBatchChunked(
  symbols: string[],
  days = 12,
): Promise<{ data: Record<string, KlineRow[]> }> {
  const data: Record<string, KlineRow[]> = {}
  const chunkSize = getDailyBatchChunkSize()
  for (let i = 0; i < symbols.length; i += chunkSize) {
    const part = symbols.slice(i, i + chunkSize)
    const resp = await api.klineDailyBatch(part, days)
    Object.assign(data, resp.data ?? {})
  }
  return { data }
}

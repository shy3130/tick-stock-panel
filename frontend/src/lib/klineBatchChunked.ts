import { api, type KlineRow } from '@/lib/api'

// 日K批量分块助手: 后端 /api/kline/daily-batch 单次 symbols 上限 400 (挡千只
// 自选整表直发的服务端 dict 峰值)。调用方 (自选/选股/中签页) 统一走这里分块
// 串行拉取并合并, 响应形状与单次调用一致。

const DAILY_BATCH_CHUNK = 300

export async function fetchDailyBatchChunked(
  symbols: string[],
  days = 12,
): Promise<{ data: Record<string, KlineRow[]> }> {
  const data: Record<string, KlineRow[]> = {}
  for (let i = 0; i < symbols.length; i += DAILY_BATCH_CHUNK) {
    const part = symbols.slice(i, i + DAILY_BATCH_CHUNK)
    const resp = await api.klineDailyBatch(part, days)
    Object.assign(data, resp.data ?? {})
  }
  return { data }
}

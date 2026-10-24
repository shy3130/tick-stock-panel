import type { QueryClient } from '@tanstack/react-query'
import { api, type MinuteKlineRow } from '@/lib/api'
import { QK } from '@/lib/queryKeys'

// 分钟批量分时的增量轮询助手:
// 后端 /api/kline/minute-batch 已支持 since 增量 (只回 >= since 的K, 含形成中
// 动态最后一根)。这里在 queryFn 内读取 react-query 缓存里的上一轮全量序列,
// 以"各 symbol 最后一根的最旧时间"为 since 请求增量, 并按 (symbol, datetime)
// upsert 合并后返回 — 组件层拿到的仍是完整序列, 代码零改动。
// 缓存不存在 (首次/换标的池/换日) 时不带 since, 全量拉取。
//
// 大列表内存约束 (千只自选场景):
// - 缓存 key 固定为 QK.minuteBatch() 单键, 数据按 symbol upsert — 不再把全量
//   symbol 拼进 key (增删 1 只即换 key 整批重拉、新旧两份共存);
// - 请求按 MINUTE_BATCH_CHUNK 分块 (后端单次上限 400), 串行发出避免并发多批
//   同时放大服务端峰值;
// - 缓存 symbol 数超过 MINUTE_CACHE_MAX_SYMBOLS 时按 LRU 淘汰视口外序列
//   (滚动历史不再永久常驻)。

type MinuteBatchData = Record<string, MinuteKlineRow[]>

/** 单请求分块大小 (后端 /api/kline/minute-batch 上限 400, 留安全余量) */
export const MINUTE_BATCH_CHUNK = 300
/** 缓存内 symbol 数上限: 视口滚动历史只保留最近请求过的 */
export const MINUTE_CACHE_MAX_SYMBOLS = 500
/** 视口集合未就绪时大列表的播种行数 (effect 填充视口后防抖补拉) */
export const MINUTE_SEED_MAX = 60

function chunk<T>(items: T[], size: number): T[][] {
  const out: T[][] = []
  for (let i = 0; i < items.length; i += size) out.push(items.slice(i, i + size))
  return out
}

export function lastBarTs(data: MinuteBatchData, symbols?: string[]): string | null {
  // 直接取"最旧最后一根"的原始 datetime 字符串 (与服务端行同格式, 同为北京墙钟):
  // 不做任何 Date/ISO 转换 — toISOString 会变成带 Z 的 UTC, 服务端 naive 比较
  // 会 TypeError, 且换算差 8 小时。固定格式字符串的字典序即时间序。
  const scope = symbols ? new Set(symbols) : null
  let min: string | null = null
  for (const [sym, rows] of Object.entries(data)) {
    if (scope && !scope.has(sym)) continue
    const last = rows[rows.length - 1]
    if (!last) continue
    if (min === null || last.datetime < min) min = last.datetime
  }
  return min
}

export function mergeInto(base: MinuteBatchData, patch: MinuteBatchData): MinuteBatchData {
  const merged: MinuteBatchData = { ...base }
  for (const [sym, rows] of Object.entries(patch)) {
    const cache = merged[sym] ?? []
    const byTs = new Map(cache.map(r => [r.datetime, r]))
    for (const r of rows) byTs.set(r.datetime, r)   // 新值覆盖 (动态K定版)
    merged[sym] = Array.from(byTs.values())
      .sort((a, b) => (a.datetime < b.datetime ? -1 : a.datetime > b.datetime ? 1 : 0))
  }
  return merged
}

/**
 * LRU 淘汰 (纯函数): 缓存 symbol 数超 max 时, 删除「本次未请求且最久未 touch」
 * 的序列。lastTouch 为 symbol → 最近请求序号 (越新越大), 由调用方在每次请求时
 * 维护; requested 集合内的 symbol 恒保留。
 */
export function pruneMinuteCache(
  data: MinuteBatchData,
  requested: Iterable<string>,
  lastTouch: ReadonlyMap<string, number>,
  max: number,
): MinuteBatchData {
  const keys = Object.keys(data)
  if (keys.length <= max) return data
  const keep = new Set(requested)
  const candidates = keys
    .filter(sym => !keep.has(sym))
    .sort((a, b) => (lastTouch.get(a) ?? -1) - (lastTouch.get(b) ?? -1))  // 最旧在前
  let drop = keys.length - max
  const dropped = new Set<string>()
  for (const sym of candidates) {
    if (drop <= 0) break
    dropped.add(sym)
    drop -= 1
  }
  if (!dropped.size) return data
  const out: MinuteBatchData = {}
  for (const sym of keys) {
    if (!dropped.has(sym)) out[sym] = data[sym]
  }
  return out
}

// 模块级 LRU touch 状态 (页面生命周期内单调递增)
const _lastTouch = new Map<string, number>()
let _touchSeq = 0

/**
 * queryFn 用: 读缓存 → 按 300/块增量请求 → 逐块合并 → LRU 淘汰 → 返回
 * { data: 完整序列 } (保持端点响应形状, 下游零改动)。
 */
export async function fetchMinuteBatchIncremental(
  qc: QueryClient,
  symbols: string[],
  preferLocal?: boolean,
): Promise<{ data: MinuteBatchData }> {
  const cacheKey = QK.minuteBatch()
  const prev = qc.getQueryData<{ data: MinuteBatchData }>(cacheKey)?.data
  let data: MinuteBatchData = prev ?? {}
  for (const part of chunk(symbols, MINUTE_BATCH_CHUNK)) {
    const since = prev ? lastBarTs(prev, part) ?? undefined : undefined
    const resp = await api.klineMinuteBatch(part, undefined, preferLocal, since)
    data = mergeInto(data, resp.data ?? {})
  }
  for (const sym of symbols) _lastTouch.set(sym, (_touchSeq += 1))
  return { data: pruneMinuteCache(data, symbols, _lastTouch, MINUTE_CACHE_MAX_SYMBOLS) }
}

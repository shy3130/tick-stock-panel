import type { QueryClient } from '@tanstack/react-query'
import { api, type MinuteKlineRow } from '@/lib/api'
import { getMinuteBatchChunkSize } from '@/lib/klineBatchLimits'
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
// - 请求按端点单请求上限分块 (值与后端校验同源, 见 lib/klineBatchLimits),
//   串行发出避免并发多批同时放大服务端峰值;
// - 缓存 symbol 数超过 MINUTE_CACHE_MAX_SYMBOLS 时按 LRU 淘汰视口外序列
//   (滚动历史不再永久常驻)。

type MinuteBatchData = Record<string, MinuteKlineRow[]>

/** 缓存内 symbol 数上限: 视口滚动历史只保留最近请求过的 */
export const MINUTE_CACHE_MAX_SYMBOLS = 500
/** 视口集合未就绪时大列表的播种行数 (effect 填充视口后防抖补拉) */
export const MINUTE_SEED_MAX = 60

function chunk<T>(items: T[], size: number): T[][] {
  const out: T[][] = []
  for (let i = 0; i < items.length; i += size) out.push(items.slice(i, i + size))
  return out
}

/**
 * 逐 symbol 的 since (symbol → 最后已缓存 bar 时间)。
 *
 * 整块共用「最旧末根」是错的: 块里混有未缓存的新 symbol 时 (滚动进新区段、
 * 手动刷新、新增自选), 新 symbol 会被 since 裁到只剩 1 根, 永远凑不满 2 根
 * → 分时图空白且点刷新也没用。逐 symbol 精确化后, 已缓存者仍只拉新增,
 * 未缓存者不带 since 拉全天。
 *
 * 另加自愈: 缓存序列不足 MIN_CACHED_BARS 根时同样视为未缓存 (不带 since)。
 * 否则历史遗留的「1 根残序列」会因 since 恒为最后一根而永久卡死 — 每轮只
 * 补 1 根、永远画不出分时, 手动刷新也无法恢复。
 */
export const MIN_CACHED_BARS = 2

function sinceBySymbol(data: MinuteBatchData, symbols: string[]): (sym: string) => string | undefined {
  const lastOf = new Map<string, string>()
  for (const sym of symbols) {
    const rows = data[sym]
    if (!rows || rows.length < MIN_CACHED_BARS) continue   // 残序列 → 全量重取
    lastOf.set(sym, rows[rows.length - 1].datetime)
  }
  return (sym: string) => lastOf.get(sym)
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
  const sinceOf = sinceBySymbol(data, symbols)
  for (const part of chunk(symbols, getMinuteBatchChunkSize())) {
    // 按 since 分组: 端点单请求只收一个 since, 而块的 symbols 可能混合
    // 「已缓存(有 since)」与「新增(无 since)」。混发会把新 symbol 裁成 1 根。
    const groups = new Map<string | undefined, string[]>()
    for (const sym of part) {
      const s = sinceOf(sym)
      const list = groups.get(s)
      if (list) list.push(sym)
      else groups.set(s, [sym])
    }
    for (const [since, group] of groups) {
      const resp = await api.klineMinuteBatch(group, undefined, preferLocal, since)
      data = mergeInto(data, resp.data ?? {})
    }
  }
  for (const sym of symbols) _lastTouch.set(sym, (_touchSeq += 1))
  return { data: pruneMinuteCache(data, symbols, _lastTouch, MINUTE_CACHE_MAX_SYMBOLS) }
}

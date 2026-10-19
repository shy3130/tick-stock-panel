/** RunningHub 赞助商折扣文案 — 数据驱动，不在代码里写死折数。
 *
 * /api/settings/ai/sponsor-models 返回每个模型的 discountRate
 * (1 = 原价, 0.3 = 3 折, 0.6 = 6 折)。这里按模型族聚合出真实折扣区间：
 * DeepSeek 各档模型折扣不同时显示 "3-6折"，相同时显示 "6折"。
 * 数据取不到时返回 null，调用方回退到不含具体数字的文案。
 *
 * 60s TTL + 单飞：设置页与助手抽屉共用同一份，不重复打上游。
 */
import { useEffect, useState } from 'react'
import { api, type SponsorModelItem } from '@/lib/api'

const TTL_MS = 60_000

/** 上游定价接口不可用时的兜底文案 (唯一来源, 其他地方不得再写死折数) */
export const DEEPSEEK_FALLBACK_RANGE = '3-6折'
export const MAX_DISCOUNT_FALLBACK_PCT = 80

let cache: { at: number; items: SponsorModelItem[] } | null = null
let inflight: Promise<SponsorModelItem[]> | null = null

/** 拉取模型定价 (带 TTL 缓存与并发合并)。失败直接抛出，由调用方决定回退。 */
export function loadSponsorModelItems(): Promise<SponsorModelItem[]> {
  if (cache && Date.now() - cache.at < TTL_MS) return Promise.resolve(cache.items)
  if (inflight) return inflight
  inflight = api.sponsorModels()
    .then(data => {
      const items = data.items ?? data.models.map(id => ({ id }))
      cache = { at: Date.now(), items }
      return items
    })
    .finally(() => { inflight = null })
  return inflight
}

/** 测试用: 清空模块级缓存 */
export function resetSponsorModelCache(): void {
  cache = null
  inflight = null
}

/** React hook: 模型定价列表。缓存命中时同步返回, 否则加载后更新; 失败返回 null。 */
export function useSponsorModelItems(): SponsorModelItem[] | null {
  const [items, setItems] = useState<SponsorModelItem[] | null>(cache?.items ?? null)
  useEffect(() => {
    let alive = true
    loadSponsorModelItems()
      .then(v => { if (alive) setItems(v) })
      .catch(() => { /* 取不到定价时由调用方回退为不含数字的文案 */ })
    return () => { alive = false }
  }, [])
  return items
}

/** 折扣率 → 中文折数: 0.6 → "6折", 0.35 → "3.5折", 0.3 → "3折" */
export function fmtFold(rate: number): string {
  const fold = Math.round(rate * 100) / 10
  return `${Number.isInteger(fold) ? fold : fold.toFixed(1)}折`
}

/** 模型族折扣区间: 0.3/0.6 → "3-6折"; 单一 0.6 → "6折"; 无折扣数据 → null
 *
 * 有忙闲时定价的模型取两档折扣 (忙时最高、闲时最低) 一并计入区间,
 * 例如忙时 6 折 / 闲时 3 折 → "3-6折"。
 */
export function discountRangeLabel(items: SponsorModelItem[], family = 'deepseek'): string | null {
  const needle = family.toLowerCase()
  const rates: number[] = []
  for (const m of items) {
    if (!m.id.toLowerCase().includes(needle)) continue
    for (const rate of [m.busy_discount_rate, m.offpeak_discount_rate, m.discount_rate]) {
      if (rate != null && Number.isFinite(rate) && rate > 0 && rate < 1) rates.push(rate)
    }
  }
  if (!rates.length) return null
  const lo = Math.min(...rates)
  const hi = Math.max(...rates)
  if (lo === hi) return fmtFold(lo)
  return `${Math.round(lo * 100) / 10}-${fmtFold(hi)}`
}

/** 某模型的忙闲时折扣文案: "忙时6折 · 闲时3折"; 无忙闲时定价 → null */
export function bandDiscountLabel(m: SponsorModelItem): string | null {
  const busy = m.busy_discount_rate
  const off = m.offpeak_discount_rate
  if (busy == null || off == null) return null
  return `忙时${fmtFold(busy)} · 闲时${fmtFold(off)}`
}

/** 全站最大优惠百分比: 最低折扣 0.3 → 70; 无折扣数据 → null */
export function maxDiscountPct(items: SponsorModelItem[]): number | null {
  const rates: number[] = []
  for (const m of items) {
    for (const rate of [m.busy_discount_rate, m.offpeak_discount_rate, m.discount_rate]) {
      if (rate != null && Number.isFinite(rate) && rate > 0 && rate < 1) rates.push(rate)
    }
  }
  if (!rates.length) return null
  return Math.round((1 - Math.min(...rates)) * 100)
}

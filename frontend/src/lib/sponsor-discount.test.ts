/** RunningHub 折扣文案: 从模型定价动态聚合成区间, 不再写死折数。 */
import { expect, it } from 'vitest'
import { DEEPSEEK_FALLBACK_RANGE, bandDiscountLabel, discountRangeLabel, fmtFold, maxDiscountPct } from './sponsor-discount'
import type { SponsorModelItem } from './api'

const item = (id: string, discount_rate?: number): SponsorModelItem => ({ id, discount_rate })

it('折扣率转中文折数', () => {
  expect(fmtFold(0.6)).toBe('6折')
  expect(fmtFold(0.35)).toBe('3.5折')
  expect(fmtFold(1)).toBe('10折')
})

it('DeepSeek 各档折扣不同 → 显示区间 3-6折', () => {
  const items = [item('deepseek/deepseek-v4-flash', 0.3), item('deepseek/deepseek-v4-pro', 0.6)]
  expect(discountRangeLabel(items)).toBe('3-6折')
})

it('DeepSeek 折扣一致 → 显示单一折数', () => {
  const items = [item('deepseek/deepseek-v4-flash', 0.6), item('deepseek/deepseek-v4-pro', 0.6)]
  expect(discountRangeLabel(items)).toBe('6折')
})

it('无折扣(1.0)/缺定价/非法值不参与聚合', () => {
  const items = [
    item('deepseek/deepseek-v4-flash', 0.6),
    item('deepseek/deepseek-v4-vision', 1),
    item('deepseek/deepseek-old'),
    item('deepseek/deepseek-bad', Number.NaN),
  ]
  expect(discountRangeLabel(items)).toBe('6折')
})

it('只看 DeepSeek 家族, 其他模型折扣不影响结果', () => {
  const items = [item('deepseek/deepseek-v4-flash', 0.6), item('openai/gpt-6-astra', 0.2)]
  expect(discountRangeLabel(items)).toBe('6折')
})

it('无任何 DeepSeek 折扣数据 → null (调用方回退兜底文案)', () => {
  expect(discountRangeLabel([], 'deepseek')).toBeNull()
  expect(discountRangeLabel([item('deepseek/deepseek-v4', 1)], 'deepseek')).toBeNull()
})

it('全站最大优惠取最低折扣', () => {
  const items = [item('a', 0.6), item('b', 0.2), item('c', 1)]
  expect(maxDiscountPct(items)).toBe(80)
  expect(maxDiscountPct([])).toBeNull()
})

it('兜底区间集中在模块常量, 供取数失败时使用', () => {
  expect(DEEPSEEK_FALLBACK_RANGE).toBe('3-6折')
})

// ── 忙闲时定价 ──────────────────────────────────────────────

const banded = (id: string, busy: number, off: number, current?: string): SponsorModelItem => ({
  id, discount_rate: busy, busy_discount_rate: busy, offpeak_discount_rate: off, current_band: current,
})

it('忙时6折/闲时3折 → 展示两档', () => {
  expect(bandDiscountLabel(banded('deepseek/deepseek-v4.1-flash', 0.6, 0.3))).toBe('忙时6折 · 闲时3折')
})

it('忙闲时区间参与家族聚合: 忙6闲3 → 3-6折', () => {
  const items = [banded('deepseek/deepseek-v4.1-flash', 0.6, 0.3)]
  expect(discountRangeLabel(items)).toBe('3-6折')
})

it('无忙闲时字段的模型回退单一折扣, bandDiscountLabel 为 null', () => {
  expect(bandDiscountLabel(item('openai/gpt-6', 0.8))).toBeNull()
  expect(bandDiscountLabel(item('openai/gpt-6', 0.8))).toBeNull()
})

it('全站最高优惠把闲时折扣计入', () => {
  expect(maxDiscountPct([banded('deepseek/a', 0.6, 0.3), item('openai/b', 0.8)])).toBe(70)
})

export interface IntradayScale {
  range: { min: number; max: number }
  reference?: number
  centered: boolean
  ticks: { price: number; percent?: number }[]
}

/** 单日价格和百分比只从这一份量程转换；价格为原价，百分比为百分点。 */
export function intradayScale(values: (number | null | undefined)[], reference?: number | null, precision = 2,
  limitRange?: { min: number; max: number }): IntradayScale | undefined {
  const prices = values.filter((value): value is number => value != null && Number.isFinite(value) && value > 0)
  if (!prices.length) return undefined
  const base = reference != null && Number.isFinite(reference) && reference > 0 ? reference : undefined
  const minMove = 10 ** -precision
  const low = Math.min(...prices), high = Math.max(...prices)
  let range: IntradayScale['range'], centered = false
  if (base != null) {
    const limitSpan = limitRange && Number.isFinite(limitRange.min) && Number.isFinite(limitRange.max)
      && limitRange.min > 0 && limitRange.min < base && limitRange.max > base
      ? Math.max(base - limitRange.min, limitRange.max - base) : undefined
    const minimum = Math.min(Math.max(base * 0.003, minMove * 2), limitSpan ?? Infinity)
    const half = Math.max(Math.abs(high - base), Math.abs(low - base), minimum)
    // 向外取易读的百分比量程；涨幅超过 100% 时改用正价格范围，避免出现负价格。
    const percent = half / base * 100
    const step = 10 ** Math.floor(Math.log10(percent)) / 5
    const rounded = Math.ceil(percent / step - 1e-9) * step / 100 * base
    // 刻度取整不能把实际 10.01% 的限价量程扩成 12%；真实超带数据仍完整显示。
    const bounded = limitSpan != null ? Math.max(half, Math.min(rounded, limitSpan)) : rounded
    const span = bounded >= base && half < base ? half : bounded
    if (span < base) { range = { min: base - span, max: base + span }; centered = true }
    else range = { min: Math.min(low, base), max: Math.max(high, base) }
  } else {
    const padding = Math.max((high + low) * 0.0015, minMove * 2)
    range = { min: Math.max(minMove, low - padding), max: high + padding }
  }
  if (range.max <= range.min) range = { min: low / 2, max: high + minMove * 2 }
  const divisions = range.max - range.min < minMove * (4 - 1e-7) ? 2 : 4
  return { range, reference: base, centered, ticks: Array.from({ length: divisions + 1 }, (_, i) => {
    const middle = centered && i === divisions / 2
    const price = middle ? base! : range.min + (range.max - range.min) * i / divisions
    return { price, ...(base != null ? { percent: middle ? 0 : (price / base - 1) * 100 } : {}) }
  }) }
}

export function formatAxisPercent(value: number) {
  const fixed = value.toFixed(2)
  return `${Number(fixed) === 0 ? '0.00' : `${value > 0 ? '+' : ''}${fixed}`}%`
}

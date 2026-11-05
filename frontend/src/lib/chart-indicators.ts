import type { OHLC } from './chart-types'

export const SUB_CHART_META = [
  { key: 'vol', label: '成交量', height: 84 },
  { key: 'macd', label: 'MACD', height: 72 },
  { key: 'rsi', label: 'RSI', height: 72 },
  { key: 'kdj', label: 'KDJ', height: 72 },
]
export const OVERLAY_INDICATORS = [{ key: 'boll', label: 'BOLL' }]

export function volMaN(data: OHLC[], n: number): (number | null)[] {
  const result: (number | null)[] = []
  for (let i = 0; i < data.length; i++) {
    if (i < n - 1) { result.push(null); continue }
    let sum = 0
    for (let j = i - n + 1; j <= i; j++) sum += data[j].volume ?? 0
    result.push(sum / n)
  }
  return result
}

export function volumeRatioAt(data: OHLC[], index: number, days: number): number | null {
  const window = Math.max(1, Math.min(20, Math.round(days)))
  if (index < window) return null
  let sum = 0
  for (let offset = 1; offset <= window; offset++) {
    const volume = data[index - offset]?.volume
    if (volume == null || !Number.isFinite(volume)) return null
    sum += volume
  }
  const average = sum / window
  const current = data[index]?.volume
  if (current == null || !Number.isFinite(current) || average <= 0) return null
  return current / average
}

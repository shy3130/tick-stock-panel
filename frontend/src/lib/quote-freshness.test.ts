import { describe, expect, it } from 'vitest'
import type { QuoteFreshness } from './api'
import { advanceQuoteFreshness, quoteFreshnessLabel } from './quote-freshness'
import { cnToday, klineDateRange } from './kline'

const stamp = (clock: string) => Date.parse(`2026-11-06T${clock}+08:00`)
const live: QuoteFreshness = { state: 'live', message: '实时行情', data_date: '2026-11-06',
  quote_ts: stamp('10:00:00'), checked_at: stamp('10:00:10'), max_age_ms: 90_000 }

describe('quote freshness', () => {
  it('ages silently disconnected data without making requests or changing the original', () => {
    expect(advanceQuoteFreshness(live, 10_000).state).toBe('live')
    expect(advanceQuoteFreshness(live, 100_000).state).toBe('delayed')
    expect(live.state).toBe('live')
  })
  it('does not call normal lunch or close stale', () => {
    const lunch = { ...live, checked_at: stamp('11:30:00'), quote_ts: stamp('11:30:00') }
    expect(advanceQuoteFreshness(lunch, 40 * 60_000).state).toBe('historical')
    const close = { ...live, checked_at: stamp('15:00:00'), quote_ts: stamp('15:00:00') }
    expect(advanceQuoteFreshness(close, 3 * 3600_000).state).toBe('historical')
  })
  it('old cached live data becomes stale on the next trading morning', () => {
    const monday = Date.parse('2026-11-09T10:00:00+08:00')
    expect(advanceQuoteFreshness(live, monday - live.checked_at).state).toBe('stale')
  })
  it('includes each symbol date rather than a global category date', () => {
    expect(quoteFreshnessLabel({ ...live, state: 'stale', data_date: '2026-11-05' })).toBe('数据过期 · 11-05')
    expect(quoteFreshnessLabel(live)).toBe('更新 10:00:00')
  })
  it('default windows use the Beijing date before UTC midnight and clamp short months', () => {
    expect(cnToday(new Date('2026-11-05T17:00:00Z'))).toBe('2026-11-06')
    expect(klineDateRange(6, '2026-11-06')).toEqual({ start: '2026-05-06', end: '2026-11-06' })
    expect(klineDateRange(6, '2026-08-31').start).toBe('2026-02-28')
  })
})

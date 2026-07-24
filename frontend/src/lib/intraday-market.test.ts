import { describe, expect, it } from 'vitest'
import { intradayTimeLabel, intradayTimes, isMarketOpen } from './intraday-market'

describe('intraday market sessions', () => {
  it('keeps market-local minute timestamps unchanged', () => {
    expect(intradayTimeLabel('2026-07-17T09:30:00')).toBe('09:30')
    expect(intradayTimeLabel('2026-07-17 15:59:00')).toBe('15:59')
  })

  it('builds the US continuous regular session', () => {
    const times = intradayTimes('NBIS.US')

    expect(times[0]).toBe('09:30')
    expect(times.at(-1)).toBe('16:00')
    expect(times).toHaveLength(391)
    expect(times).toContain('12:30')
  })

  it('builds the Hong Kong session with its lunch break', () => {
    const times = intradayTimes('700.HK')

    expect(times[0]).toBe('09:30')
    expect(times.at(-1)).toBe('16:00')
    expect(times).toContain('12:00')
    expect(times).not.toContain('12:30')
    expect(times).toContain('13:00')
  })

  it('keeps the A-share split session', () => {
    const times = intradayTimes('000001.SZ')

    expect(times[0]).toBe('09:30')
    expect(times.at(-1)).toBe('15:00')
    expect(times).toContain('11:30')
    expect(times).not.toContain('12:30')
    expect(times).toContain('13:00')
  })

  it('detects CN and Hong Kong sessions in Asia/Shanghai', () => {
    expect(isMarketOpen('700.HK', new Date('2026-07-24T01:30:00Z'))).toBe(true)
    expect(isMarketOpen('700.HK', new Date('2026-07-24T04:30:00Z'))).toBe(false)
    expect(isMarketOpen('000001.SZ', new Date('2026-07-24T06:00:00Z'))).toBe(true)
    expect(isMarketOpen('000001.SZ', new Date('2026-07-25T02:00:00Z'))).toBe(false)
  })

  it('detects the US regular session in America/New_York', () => {
    expect(isMarketOpen('AAPL.US', new Date('2026-07-24T14:00:00Z'))).toBe(true)
    expect(isMarketOpen('AAPL.US', new Date('2026-07-24T21:00:00Z'))).toBe(false)
  })
})

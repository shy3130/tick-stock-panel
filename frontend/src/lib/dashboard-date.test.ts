import { describe, expect, it } from 'vitest'

import { resolveDashboardDateState, selectedDateForRequest } from './dashboard-date'

describe('dashboard live trading date', () => {
  it('opens the realtime date when it is newer than persisted daily data', () => {
    expect(resolveDashboardDateState('2026-07-20', '2026-07-20', '2026-07-21', undefined)).toEqual({
      currentDate: '2026-07-21',
      maxDate: '2026-07-21',
    })
  })

  it('uses the market overview date when no realtime date exists', () => {
    expect(resolveDashboardDateState('2026-07-20', '2026-07-18', null, undefined).currentDate)
      .toBe('2026-07-18')
  })

  it('keeps the realtime date in live request mode', () => {
    expect(selectedDateForRequest('2026-07-21', '2026-07-21')).toBeUndefined()
  })

  it('keeps an older date as an explicit historical request', () => {
    expect(selectedDateForRequest('2026-07-18', '2026-07-21')).toBe('2026-07-18')
  })
})

import { describe, expect, it } from 'vitest'

import { overviewRefetchInterval } from './overviewRefresh'

describe('overviewRefetchInterval', () => {
  it('polls the latest dashboard but leaves historical dates static', () => {
    expect(overviewRefetchInterval(undefined)).toBe(10_000)
    expect(overviewRefetchInterval('2026-07-20')).toBe(false)
  })
})

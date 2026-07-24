import { describe, expect, it } from 'vitest'

import { limitLadderRefetchInterval } from '@/lib/limit-ladder-realtime'

describe('limit ladder realtime refresh', () => {
  it('refreshes the live ladder periodically', () => {
    expect(limitLadderRefetchInterval('')).toBe(5_000)
  })

  it('does not refresh a historical ladder', () => {
    expect(limitLadderRefetchInterval('2026-07-20')).toBe(false)
  })
})

import { describe, expect, it } from 'vitest'

import { isNewBuild, mayAutoReload } from './appVersion'


describe('app version decisions', () => {
  it('detects only a different non-empty remote build', () => {
    expect(isNewBuild('old', { build_id: 'new', published_at: null })).toBe(true)
    expect(isNewBuild('same', { build_id: 'same', published_at: null })).toBe(false)
    expect(isNewBuild('old', { build_id: '', published_at: null })).toBe(false)
  })

  it('auto reloads only a hidden idle page without an open dialog', () => {
    expect(mayAutoReload({
      visibility: 'hidden',
      activeMutations: 0,
      dialogOpen: false,
    })).toBe(true)
    expect(mayAutoReload({
      visibility: 'visible',
      activeMutations: 0,
      dialogOpen: false,
    })).toBe(false)
    expect(mayAutoReload({
      visibility: 'hidden',
      activeMutations: 1,
      dialogOpen: false,
    })).toBe(false)
    expect(mayAutoReload({
      visibility: 'hidden',
      activeMutations: 0,
      dialogOpen: true,
    })).toBe(false)
  })
})

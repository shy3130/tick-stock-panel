import assert from 'node:assert/strict'
import test from 'node:test'

import { canAuthorStrategies } from './access.ts'

test('only administrators can author Python strategies', () => {
  assert.equal(canAuthorStrategies({ user: { role: 'admin' } }), true)
  assert.equal(canAuthorStrategies({ user: { role: 'user' } }), false)
  assert.equal(canAuthorStrategies({ user: null }), false)
  assert.equal(canAuthorStrategies(undefined), false)
})

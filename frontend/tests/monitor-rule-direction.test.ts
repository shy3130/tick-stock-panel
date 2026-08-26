import assert from 'node:assert/strict'
import test from 'node:test'

import { directionAfterTypeChange } from '../src/components/monitor/ruleTypeDirection.ts'

test('switching to abnormal initializes both directions', () => {
  assert.equal(directionAfterTypeChange('signal', 'abnormal', 'entry'), 'both')
})

test('leaving abnormal clears up/down before strategy save', () => {
  assert.equal(directionAfterTypeChange('abnormal', 'strategy', 'up'), 'both')
  assert.equal(directionAfterTypeChange('abnormal', 'strategy', 'down'), 'both')
})

test('switching between regular types preserves a valid direction', () => {
  assert.equal(directionAfterTypeChange('signal', 'strategy', 'entry'), 'entry')
})

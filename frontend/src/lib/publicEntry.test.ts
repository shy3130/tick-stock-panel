import test from 'node:test'
import assert from 'node:assert/strict'
import { resolvePublicEntry } from './publicEntry.ts'

test('shows loading while auth status is unknown', () => {
  assert.equal(resolvePublicEntry(null, '/'), 'loading')
})

test('shows the workspace when the user is authenticated', () => {
  assert.equal(resolvePublicEntry({ authenticated: true }, '/'), 'workspace')
  assert.equal(resolvePublicEntry({ authenticated: true }, '/screener'), 'workspace')
})

test('shows the public landing page for unauthenticated root visits', () => {
  assert.equal(resolvePublicEntry({ authenticated: false }, '/'), 'landing')
})

test('redirects unauthenticated internal routes to login', () => {
  assert.equal(resolvePublicEntry({ authenticated: false }, '/screener'), 'login')
  assert.equal(resolvePublicEntry({ authenticated: false }, '/settings?tab=ai'), 'login')
})

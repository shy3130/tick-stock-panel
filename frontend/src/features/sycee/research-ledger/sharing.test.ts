import assert from 'node:assert/strict'
import test from 'node:test'

import type { ResearchEntry, ResearchShare } from './api.ts'
import { isResearchShareStale, researchShareUrl } from './sharing.ts'

const entry = {
  id: 'research_1',
  title: '渠道验证',
  subject_type: 'stock',
  subject: '600519.SH',
  thesis: '',
  evidence: [],
  counter_evidence: [],
  invalidation: '',
  plan: '',
  status: 'tracking',
  tags: [],
  created_at: '2026-07-01T08:00:00+00:00',
  updated_at: '2026-07-02T08:00:00+00:00',
  origin: 'manual',
  captures: [],
} satisfies ResearchEntry

const share = {
  id: 'research_share_1',
  entry_id: entry.id,
  token: 'token',
  created_at: entry.created_at,
  refreshed_at: entry.updated_at,
  entry_updated_at: entry.updated_at,
} satisfies ResearchShare

test('builds an encoded public share URL without a duplicate slash', () => {
  assert.equal(
    researchShareUrl('https://sycee.example/', 'token/value'),
    'https://sycee.example/share/research/token%2Fvalue',
  )
})

test('marks the snapshot stale whenever the private entry version differs', () => {
  assert.equal(isResearchShareStale(share, entry), false)
  assert.equal(isResearchShareStale(share, { ...entry, updated_at: '2026-07-03T08:00:00+00:00' }), true)
})

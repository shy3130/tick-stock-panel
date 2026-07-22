import { request } from '@/lib/api'

export type ResearchSubjectType = 'stock' | 'strategy' | 'sector' | 'market'
export type ResearchStatus = 'draft' | 'tracking' | 'validated' | 'invalidated' | 'archived'
export type ResearchOrigin = 'manual' | 'capture'
export type ResearchCaptureAction = 'created' | 'appended' | 'duplicate'
export type ResearchCaptureValue = string | number | boolean | null

export interface ResearchCapture {
  id: string
  captured_at: string
  source: string
  source_label: string
  source_key: string
  summary: string
  snapshot: Record<string, ResearchCaptureValue>
}

export interface ResearchCaptureInput {
  symbol: string
  name: string
  source: string
  source_label: string
  source_key: string
  summary: string
  snapshot: Record<string, ResearchCaptureValue>
}

export interface ResearchEntryInput {
  title: string
  subject_type: ResearchSubjectType
  subject: string
  thesis: string
  evidence: string[]
  counter_evidence: string[]
  invalidation: string
  plan: string
  status: ResearchStatus
  tags: string[]
}

export interface ResearchEntry extends ResearchEntryInput {
  id: string
  created_at: string
  updated_at: string
  origin: ResearchOrigin
  captures: ResearchCapture[]
}

export const RESEARCH_LEDGER_QUERY_KEY = ['sycee', 'research-ledger'] as const

export const researchLedgerApi = {
  list: () => request<{ entries: ResearchEntry[]; total: number }>('/api/sycee/research'),
  create: (entry: ResearchEntryInput) =>
    request<{ entry: ResearchEntry }>('/api/sycee/research', {
      method: 'POST',
      body: JSON.stringify(entry),
    }),
  update: (entryId: string, changes: Partial<ResearchEntryInput>) =>
    request<{ entry: ResearchEntry }>(`/api/sycee/research/${encodeURIComponent(entryId)}`, {
      method: 'PATCH',
      body: JSON.stringify(changes),
    }),
  delete: (entryId: string) =>
    request<{ ok: boolean }>(`/api/sycee/research/${encodeURIComponent(entryId)}`, {
      method: 'DELETE',
    }),
  capture: (capture: ResearchCaptureInput) =>
    request<{ entry: ResearchEntry; action: ResearchCaptureAction; capture_id: string }>('/api/sycee/research/capture', {
      method: 'POST',
      body: JSON.stringify(capture),
    }),
  undoCapture: (entryId: string, captureId: string) =>
    request<{ ok: boolean; entry_deleted: boolean; entry: ResearchEntry | null }>(
      `/api/sycee/research/${encodeURIComponent(entryId)}/captures/${encodeURIComponent(captureId)}`,
      { method: 'DELETE' },
    ),
}

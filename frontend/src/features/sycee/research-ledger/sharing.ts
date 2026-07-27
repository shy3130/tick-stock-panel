import type { ResearchEntry, ResearchShare } from './api'

export function researchShareUrl(origin: string, token: string): string {
  return `${origin.replace(/\/$/, '')}/share/research/${encodeURIComponent(token)}`
}

export function isResearchShareStale(share: ResearchShare, entry: ResearchEntry): boolean {
  return share.entry_updated_at !== entry.updated_at
}

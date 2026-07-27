import { request } from '@/lib/api'

export interface SyceeBackupData {
  portfolio: { version: 1; trades: unknown[] } | null
  portfolio_sell_alert: { version: 1; config: Record<string, unknown> } | null
  trade_reviews: { version: 1; reviews: unknown[] } | null
  research_ledger: { version: 1; entries: unknown[] } | null
}

export interface SyceeBackupDocument {
  format: 'sycee-user-data'
  version: 1
  exported_at: string
  data: SyceeBackupData
}

export interface SyceeRestoreResponse {
  ok: true
  safety_backup_id: string
}

export const DATA_BACKUP_QUERY_KEY = ['sycee', 'data-backup'] as const

export const syceeDataBackupApi = {
  read: () => request<SyceeBackupDocument>('/api/sycee/data-backup'),
  restore: (backup: SyceeBackupDocument) => request<SyceeRestoreResponse>(
    '/api/sycee/data-backup/restore',
    {
      method: 'POST',
      body: JSON.stringify({ confirmation: 'RESTORE_SYCEE_DATA', backup }),
    },
  ),
}

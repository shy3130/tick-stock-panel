import type { SyceeBackupDocument } from './api.ts'

export const MAX_BACKUP_FILE_BYTES = 20 * 1024 * 1024

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function validSection(value: unknown, listKey: string): boolean {
  return value === null || (isRecord(value) && value.version === 1 && Array.isArray(value[listKey]))
}

export function parseBackupFile(text: string): SyceeBackupDocument {
  if (new TextEncoder().encode(text).byteLength > MAX_BACKUP_FILE_BYTES) {
    throw new Error('备份文件不能超过 20 MB')
  }
  let value: unknown
  try {
    value = JSON.parse(text)
  } catch {
    throw new Error('备份文件不是有效 JSON')
  }
  if (!isRecord(value) || value.format !== 'sycee-user-data' || value.version !== 1
    || typeof value.exported_at !== 'string' || !isRecord(value.data)) {
    throw new Error('不是受支持的 Sycee 数据备份')
  }
  const data = value.data
  if (!validSection(data.portfolio, 'trades')
    || !(data.portfolio_sell_alert === null || (isRecord(data.portfolio_sell_alert)
      && data.portfolio_sell_alert.version === 1 && isRecord(data.portfolio_sell_alert.config)))
    || !validSection(data.trade_reviews, 'reviews')
    || !validSection(data.research_ledger, 'entries')
    || !(data.strategy_tracking === undefined || validSection(data.strategy_tracking, 'tracks'))) {
    throw new Error('Sycee 数据备份内容不完整')
  }
  return value as unknown as SyceeBackupDocument
}

export function backupSummary(backup: SyceeBackupDocument) {
  return {
    trades: backup.data.portfolio?.trades.length ?? 0,
    reviews: backup.data.trade_reviews?.reviews.length ?? 0,
    researchEntries: backup.data.research_ledger?.entries.length ?? 0,
    strategyTracks: backup.data.strategy_tracking?.tracks.length ?? 0,
    sellAlertSaved: backup.data.portfolio_sell_alert !== null,
  }
}

export function backupFilename(exportedAt: string): string {
  const parsed = new Date(exportedAt)
  const stamp = Number.isFinite(parsed.getTime())
    ? parsed.toISOString().replace(/[-:]/g, '').replace(/\.\d{3}Z$/, 'Z')
    : 'unknown-time'
  return `sycee-data-${stamp}.json`
}

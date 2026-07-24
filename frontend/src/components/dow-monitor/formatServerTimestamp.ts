const BEIJING_OFFSET_MS = 8 * 60 * 60 * 1000

export function formatServerTimestamp(value: number | string | null | undefined) {
  if (value == null || value === '') return null
  const parsed = new Date(value)
  if (!Number.isFinite(parsed.getTime())) return String(value)
  const beijing = new Date(parsed.getTime() + BEIJING_OFFSET_MS)
  return beijing.toISOString().slice(0, 16).replace('T', ' ')
}

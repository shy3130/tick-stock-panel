function parseServerTimestamp(value: number | string): Date {
  if (typeof value === 'number') return new Date(value)
  const normalized = value.trim().replace(' ', 'T')
  const timezoneAware = /(?:Z|[+-]\d{2}:?\d{2})$/i.test(normalized)
  return new Date(timezoneAware ? normalized : `${normalized}Z`)
}

function dateParts(value: Date, timeZone: string) {
  const parts = new Intl.DateTimeFormat('en-US', {
    timeZone,
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    hourCycle: 'h23',
  }).formatToParts(value)
  return Object.fromEntries(parts.map(part => [part.type, part.value]))
}

export function formatServerTimestamp(value: number | string | null | undefined) {
  if (value == null || value === '') return null
  const parsed = parseServerTimestamp(value)
  if (!Number.isFinite(parsed.getTime())) return String(value)
  const parts = dateParts(parsed, 'Asia/Shanghai')
  return `${parts.year}-${parts.month}-${parts.day} ${parts.hour}:${parts.minute}`
}

export function formatExchangeTradeDate(
  value: number | string | null | undefined,
  symbol: string,
) {
  if (value == null || value === '') return ''
  const parsed = parseServerTimestamp(value)
  if (!Number.isFinite(parsed.getTime())) return ''
  const timeZone = symbol.toUpperCase().endsWith('.US')
    ? 'America/New_York'
    : 'Asia/Shanghai'
  const parts = dateParts(parsed, timeZone)
  return `${parts.year}-${parts.month}-${parts.day}`
}

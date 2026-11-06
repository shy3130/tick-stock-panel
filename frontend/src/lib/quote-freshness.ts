import type { QuoteFreshness } from './api'

/** Advance the server's live verdict without requests, including a silent SSE outage. */
export function advanceQuoteFreshness(info: QuoteFreshness, elapsedMs: number): QuoteFreshness {
  if (info.state !== 'live' && info.state !== 'delayed') return info
  const nowMs = info.checked_at + Math.max(0, elapsedMs)
  const wall = new Date(nowMs + 8 * 3600_000)
  const day = wall.toISOString().slice(0, 10)
  const minute = wall.getUTCHours() * 60 + wall.getUTCMinutes()
  if (wall.getUTCDay() === 0 || wall.getUTCDay() === 6 || minute < 570) {
    return { ...info, state: 'historical', message: '历史行情，按实际数据日期显示' }
  }
  if (info.data_date && info.data_date < day) {
    return { ...info, state: 'stale', message: '行情已过期，保留最近有效数据，等待更新' }
  }
  let reference = nowMs
  const resting = (minute >= 690 && minute < 780) || minute >= 900
  if (resting) {
    const boundary = minute < 780 ? 690 : 900
    reference = Date.parse(`${day}T00:00:00+08:00`) + boundary * 60_000
  }
  if (info.quote_ts != null && reference - info.quote_ts > info.max_age_ms) {
    return { ...info, state: 'delayed', message: '行情更新延迟，保留最近有效数据，请检查数据源与连接' }
  }
  return resting ? { ...info, state: 'historical', message: '休盘行情，按最后有效时间显示' } : info
}

export function quoteFreshnessLabel(info: QuoteFreshness): string {
  const day = info.data_date?.slice(5) ?? ''
  const time = info.quote_ts == null ? '' : new Intl.DateTimeFormat('en-GB', {
    timeZone: 'Asia/Shanghai', hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false,
  }).format(info.quote_ts)
  switch (info.state) {
    case 'live': return time ? `更新 ${time}` : '实时行情'
    case 'stale': return `数据过期${day ? ` · ${day}` : ''}`
    case 'delayed': return `更新延迟${time ? ` · ${time}` : ''}`
    case 'historical': return `历史行情${day ? ` · ${day}` : ''}`
    case 'paused': return '行情已暂停'
    case 'disabled': return '实时未开启'
    case 'missing': return '待行情'
    default: return '时间未确认'
  }
}

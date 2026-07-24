type Session = readonly [start: string, end: string]

const MARKET_SESSIONS: Record<'cn' | 'hk' | 'us', readonly Session[]> = {
  cn: [['09:30', '11:30'], ['13:00', '15:00']],
  hk: [['09:30', '12:00'], ['13:00', '16:00']],
  us: [['09:30', '16:00']],
}

function marketForSymbol(symbol?: string): 'cn' | 'hk' | 'us' {
  const upper = symbol?.toUpperCase() ?? ''
  if (upper.endsWith('.US')) return 'us'
  if (upper.endsWith('.HK')) return 'hk'
  return 'cn'
}

function minuteOfDay(value: string): number {
  const [hour, minute] = value.split(':').map(Number)
  return hour * 60 + minute
}

function minuteLabel(value: number): string {
  const hour = Math.floor(value / 60)
  const minute = value % 60
  return `${String(hour).padStart(2, '0')}:${String(minute).padStart(2, '0')}`
}

export function intradayTimeLabel(datetime: string): string {
  const match = datetime.match(/[T\s](\d{2}):(\d{2})/)
  return match ? `${match[1]}:${match[2]}` : datetime.slice(11, 16)
}

export function intradayTimes(symbol?: string): string[] {
  const sessions = MARKET_SESSIONS[marketForSymbol(symbol)]
  return sessions.flatMap(([start, end]) => {
    const result: string[] = []
    for (let minute = minuteOfDay(start); minute <= minuteOfDay(end); minute += 1) {
      result.push(minuteLabel(minute))
    }
    return result
  })
}

export function isMarketOpen(symbol: string, now = new Date()): boolean {
  const market = marketForSymbol(symbol)
  const timeZone = market === 'us' ? 'America/New_York' : 'Asia/Shanghai'
  const parts = new Intl.DateTimeFormat('en-US', {
    timeZone,
    weekday: 'short',
    hour: '2-digit',
    minute: '2-digit',
    hourCycle: 'h23',
  }).formatToParts(now)
  const values = Object.fromEntries(parts.map(part => [part.type, part.value]))
  if (values.weekday === 'Sat' || values.weekday === 'Sun') return false
  const current = minuteOfDay(`${values.hour}:${values.minute}`)
  return MARKET_SESSIONS[market].some(
    ([start, end]) => current >= minuteOfDay(start) && current <= minuteOfDay(end),
  )
}

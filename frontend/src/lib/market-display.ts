const MARKET_LABELS: Record<string, string> = {
  cn: 'A股',
  hk: '港股',
  us: '美股',
}

const CURRENCY_LABELS: Record<string, string> = {
  CNY: '人民币',
  HKD: '港元',
  USD: '美元',
}

export type MarketCode = 'cn' | 'hk' | 'us'

export const marketFromSymbol = (symbol: string): MarketCode | '' => {
  const value = String(symbol ?? '').trim().toUpperCase()
  if (value.endsWith('.SH') || value.endsWith('.SZ') || value.endsWith('.BJ')) return 'cn'
  if (value.endsWith('.HK')) return 'hk'
  if (value.endsWith('.US')) return 'us'
  return ''
}

export const currencyForMarket = (market: string | null | undefined) => {
  const value = String(market ?? '').trim().toLowerCase()
  if (value === 'cn') return 'CNY'
  if (value === 'hk') return 'HKD'
  if (value === 'us') return 'USD'
  return ''
}

export const marketLabel = (market: string | null | undefined) => {
  const value = String(market ?? '').trim().toLowerCase()
  return MARKET_LABELS[value] ?? value
}

export const currencyLabel = (currency: string | null | undefined) => {
  const value = String(currency ?? '').trim().toUpperCase()
  return CURRENCY_LABELS[value] ?? value
}

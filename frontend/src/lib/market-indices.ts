import type { MarketCode } from './market-display'

export type CoreIndex = {
  symbol: string
  name: string
}

const CORE_INDEXES: Record<MarketCode, readonly CoreIndex[]> = {
  cn: [
    { symbol: '000001.SH', name: '上证指数' },
    { symbol: '399001.SZ', name: '深证成指' },
    { symbol: '399006.SZ', name: '创业板指' },
    { symbol: '000680.SH', name: '科创综指' },
  ],
  hk: [
    { symbol: 'HSI.HK', name: '恒生指数' },
    { symbol: 'HSTECH.HK', name: '恒生科技指数' },
    { symbol: 'HSCEI.HK', name: '恒生中国企业指数' },
  ],
  us: [
    { symbol: '.SPX.US', name: '标普500指数' },
    { symbol: '.IXIC.US', name: '纳斯达克综合指数' },
    { symbol: '.DJI.US', name: '道琼斯工业平均指数' },
    { symbol: '.VIX.US', name: '标普500波动率指数' },
  ],
}

export function pinnedIndexesForMarket(market: MarketCode): readonly CoreIndex[] {
  return CORE_INDEXES[market]
}

export function indexMatchesMarket(symbol: string, market: MarketCode): boolean {
  const upper = symbol.trim().toUpperCase()
  if (market === 'hk') return upper.endsWith('.HK')
  if (market === 'us') return upper.endsWith('.US')
  return !upper.endsWith('.HK') && !upper.endsWith('.US')
}

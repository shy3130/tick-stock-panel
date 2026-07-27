import type { Portfolio, PortfolioPosition, PositionQuote } from './api.ts'

export interface PortfolioPositionView extends PortfolioPosition {
  current_price: number | null
  market_value: number | null
  unrealized_pnl: number | null
  return_pct: number | null
  quote_date: string | null
  is_live: boolean
}

export interface PortfolioViewSummary {
  cost_value: number
  priced_cost_value: number
  market_value: number
  unrealized_pnl: number
  realized_pnl: number
  unpriced_count: number
}

export interface PortfolioView {
  positions: PortfolioPositionView[]
  summary: PortfolioViewSummary
}

export function buildPortfolioView(
  portfolio: Portfolio,
  quotes: Record<string, PositionQuote>,
): PortfolioView {
  let pricedCostValue = 0
  let marketValue = 0
  let unrealizedPnl = 0
  let unpricedCount = 0

  const positions = portfolio.positions.map(position => {
    const quote = quotes[position.symbol]
    if (!quote) {
      unpricedCount += 1
      return {
        ...position,
        current_price: null,
        market_value: null,
        unrealized_pnl: null,
        return_pct: null,
        quote_date: null,
        is_live: false,
      }
    }

    const positionMarketValue = quote.price * position.quantity
    const positionPnl = positionMarketValue - position.cost_value
    pricedCostValue += position.cost_value
    marketValue += positionMarketValue
    unrealizedPnl += positionPnl
    return {
      ...position,
      name: position.name || quote.name,
      current_price: quote.price,
      market_value: positionMarketValue,
      unrealized_pnl: positionPnl,
      return_pct: position.cost_value > 0 ? positionPnl / position.cost_value : null,
      quote_date: quote.date,
      is_live: quote.is_live,
    }
  })

  return {
    positions,
    summary: {
      cost_value: portfolio.summary.cost_value,
      priced_cost_value: pricedCostValue,
      market_value: marketValue,
      unrealized_pnl: unrealizedPnl,
      realized_pnl: portfolio.summary.realized_pnl,
      unpriced_count: unpricedCount,
    },
  }
}

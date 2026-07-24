import { createContext, useCallback, useContext, useEffect, useMemo, type ReactNode } from 'react'
import { useLocation, useNavigate } from 'react-router-dom'
import type { MarketCode } from './market-display'

const STORAGE_KEY = 'tickflow.market'
const MARKET_CODES: ReadonlySet<string> = new Set(['cn', 'hk', 'us'])

export const normalizeMarketCode = (value: string | null | undefined): MarketCode => {
  const normalized = String(value ?? '').trim().toLowerCase()
  return MARKET_CODES.has(normalized) ? normalized as MarketCode : 'cn'
}

export const withMarketParam = (path: string, market: MarketCode): string => {
  const hashIndex = path.indexOf('#')
  const hash = hashIndex >= 0 ? path.slice(hashIndex) : ''
  const withoutHash = hashIndex >= 0 ? path.slice(0, hashIndex) : path
  const queryIndex = withoutHash.indexOf('?')
  const pathname = queryIndex >= 0 ? withoutHash.slice(0, queryIndex) : withoutHash
  const search = queryIndex >= 0 ? withoutHash.slice(queryIndex + 1) : ''
  const params = new URLSearchParams(search)
  params.set('market', market)
  return `${pathname}?${params.toString()}${hash}`
}

type MarketScopeValue = {
  market: MarketCode
  setMarket: (market: MarketCode) => void
}

const MarketScopeContext = createContext<MarketScopeValue | null>(null)

const storedMarket = (): MarketCode => {
  if (typeof window === 'undefined') return 'cn'
  return normalizeMarketCode(window.localStorage.getItem(STORAGE_KEY))
}

export function MarketScopeProvider({ children }: { children: ReactNode }) {
  const location = useLocation()
  const navigate = useNavigate()
  const rawMarket = new URLSearchParams(location.search).get('market')
  const hasValidMarket = rawMarket != null && MARKET_CODES.has(rawMarket.toLowerCase())
  const market = hasValidMarket ? normalizeMarketCode(rawMarket) : storedMarket()

  useEffect(() => {
    window.localStorage.setItem(STORAGE_KEY, market)
    if (!hasValidMarket) {
      navigate(withMarketParam(`${location.pathname}${location.search}${location.hash}`, market), { replace: true })
    }
  }, [hasValidMarket, location.hash, location.pathname, location.search, market, navigate])

  const setMarket = useCallback((nextMarket: MarketCode) => {
    const normalized = normalizeMarketCode(nextMarket)
    window.localStorage.setItem(STORAGE_KEY, normalized)
    navigate(withMarketParam(`${location.pathname}${location.search}${location.hash}`, normalized), { replace: true })
  }, [location.hash, location.pathname, location.search, navigate])

  const value = useMemo(() => ({ market, setMarket }), [market, setMarket])
  return <MarketScopeContext.Provider value={value}>{children}</MarketScopeContext.Provider>
}

export function useMarketScope(): MarketScopeValue {
  const value = useContext(MarketScopeContext)
  if (!value) throw new Error('useMarketScope must be used within MarketScopeProvider')
  return value
}

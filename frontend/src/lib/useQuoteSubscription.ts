import { useEffect } from 'react'
import { api } from './api'
import { setFocusSymbol } from './quote-focus'

/** A lease describes visible demand. Only the server's existing worker fetches quotes. */
export function useQuoteSubscription(symbol: string, enabled = true) {
  useEffect(() => {
    if (!symbol || !enabled) return
    const releaseFocus = setFocusSymbol(symbol)
    // A separate ID per activation prevents late cleanup from removing a new symbol.
    const id = globalThis.crypto?.randomUUID?.() ?? `detail_${Date.now().toString(36)}_${Math.random().toString(36).slice(2)}`
    let active = true
    let pending: Promise<unknown> | undefined
    let timer: ReturnType<typeof setTimeout> | undefined
    const renew = () => {
      clearTimeout(timer)
      if (!active || pending) return
      if (document.visibilityState === 'hidden') {
        timer = setTimeout(renew, 20_000)
        return
      }
      pending = api.subscribeQuotes(id, [symbol]).catch(() => {
        // Keep valid prices; freshness labels expose missing/delayed data.
      }).finally(() => {
        pending = undefined
        if (active) timer = setTimeout(renew, 20_000)
      })
    }
    const visibility = () => { if (document.visibilityState !== 'hidden') renew() }
    document.addEventListener('visibilitychange', visibility)
    renew()
    return () => {
      active = false
      releaseFocus()
      clearTimeout(timer)
      document.removeEventListener('visibilitychange', visibility)
      // Finish the last renewal before releasing; an abandoned window also expires.
      void (pending ?? Promise.resolve()).finally(() => api.unsubscribeQuotes(id)).catch(() => {})
    }
  }, [symbol, enabled])
}

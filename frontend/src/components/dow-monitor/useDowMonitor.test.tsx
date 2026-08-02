import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, renderHook, waitFor } from '@testing-library/react'
import type { ReactNode } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { DowMonitorMarket, DowMonitorOverviewResponse } from './types'
import {
  useAddDowMonitorSymbol,
  useDowMonitorDetail,
  useDowMonitorOverview,
  useDowMonitorSymbols,
  useDowMonitorStatus,
  useDowNotifications,
  useMarkDowNotificationRead,
  useRemoveDowMonitorSymbol,
  useSetDowMonitorEnabled,
} from './useDowMonitor'

const fetchMock = vi.fn()

function response(body: unknown) {
  return {
    ok: true,
    json: () => Promise.resolve(body),
  } as Response
}

function createWrapper() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  })
  return {
    queryClient,
    wrapper: ({ children }: { children: ReactNode }) => (
      <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
    ),
  }
}

beforeEach(() => {
  fetchMock.mockResolvedValue(response({ symbols: [], source: 'webstock', source_timestamp: null }))
  vi.stubGlobal('fetch', fetchMock)
})

afterEach(() => {
  vi.unstubAllGlobals()
  fetchMock.mockReset()
})

describe('Dow monitor queries', () => {
  it('keeps market filtering as a query parameter and never toggles hidden symbols', async () => {
    const { wrapper } = createWrapper()

    renderHook(() => useDowMonitorOverview('hk'), { wrapper })

    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledWith(
        expect.stringMatching(/^\/api\/dow-monitor\/list-overview\?market=hk&_=\d+$/),
        expect.objectContaining({ cache: 'no-store' }),
      )
    })
    expect(fetchMock).not.toHaveBeenCalledWith(
      expect.stringContaining('/symbols/INTC.US'),
      expect.anything(),
    )
  })

  it('keeps polling the overview query while realtime is active', async () => {
    const { queryClient, wrapper } = createWrapper()

    renderHook(() => useDowMonitorOverview('hk', true), { wrapper })

    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledWith(
        expect.stringMatching(/^\/api\/dow-monitor\/list-overview\?market=hk&_=\d+$/),
        expect.objectContaining({ cache: 'no-store' }),
      )
    })
    const query = queryClient.getQueryCache().find({
      queryKey: ['dow-monitor', 'overview', 'hk'],
      exact: true,
    })
    const options = query?.options as { refetchInterval?: number | false } | undefined
    expect(options?.refetchInterval).toBe(15_000)
  })

  it('uses market and timeframe only as query parameters', async () => {
    const { wrapper } = createWrapper()

    renderHook(() => {
      useDowMonitorSymbols()
      useDowNotifications('us')
      useDowMonitorDetail('INTC.US', '15m')
    }, { wrapper })

    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledWith(
        '/api/dow-monitor/symbols',
        expect.objectContaining({}),
      )
      expect(fetchMock).toHaveBeenCalledWith(
        expect.stringMatching(/^\/api\/dow-monitor\/notification-summaries\?market=us&_=\d+$/),
        expect.objectContaining({ cache: 'no-store' }),
      )
      expect(fetchMock).toHaveBeenCalledWith(
        expect.stringMatching(/^\/api\/dow-monitor\/INTC\.US\?timeframe=15m&_=\d+$/),
        expect.objectContaining({ cache: 'no-store' }),
      )
    })
  })

  it('polls backend monitor status every 15 seconds', async () => {
    const { queryClient, wrapper } = createWrapper()

    renderHook(() => useDowMonitorStatus(), { wrapper })

    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledWith(
        expect.stringMatching(/^\/api\/dow-monitor\/status\?_=\d+$/),
        expect.objectContaining({ cache: 'no-store' }),
      )
    })
    const query = queryClient.getQueryCache().find({
      queryKey: ['dow-monitor', 'status'],
      exact: true,
    })
    const options = query?.options as { refetchInterval?: number } | undefined
    expect(options?.refetchInterval).toBe(15_000)
  })

  it('preserves persisted partial sidecars and activation state without normalising their casing', () => {
    const response = {
      symbols: [{
        symbol: '01347.HK',
        name: '华丰科技',
        last_price: 13.47,
        change_pct: 0.0125,
        quote_timestamp: 1_774_752_700_000,
        market: 'hk',
        enabled: true,
        created_at: '2026-07-23T08:00:00Z',
        updated_at: '2026-07-23T08:00:00Z',
        states: {
          '5m': {
            symbol: '01347.HK',
            market: 'hk',
            timeframe: '5m',
            freshness_state: 'LIVE',
            source_timestamp: '2026-07-23T08:00:00Z',
            snapshot: {},
            chart: { longTerm: { trendDirection: 'UP', operation: '持有' } },
            updated_at: '2026-07-23T08:00:00Z',
          },
        },
        latest_notification: {
          notification_id: 'notification-1',
          event_key: 'event-1',
          symbol: '01347.HK',
          market: 'hk',
          timeframe: '5m',
          side: 'BUY',
          action_name: '买入',
          shape_name: '突破',
          triggered_at: '2026-07-23T08:00:00Z',
          trigger_price: 12.3,
          snapshot_payload: {
            engine: { snapshot: { action: '观察' } },
            activation: {
              active: true,
              family: 'LONG_TERM_BUY',
              structure_id: 'LONG-1',
              activation_sequence: 1,
            },
          },
          read_at: null,
        },
        last_success_at: '2026-07-23T08:00:00Z',
        last_error: null,
      }],
      source: 'webstock',
      source_timestamp: '2026-07-23T08:00:00Z',
    } satisfies DowMonitorOverviewResponse

    const symbol = response.symbols[0]
    expect(symbol.states['5m']?.chart.longTerm?.trendDirection).toBe('UP')
    expect(symbol.latest_notification?.snapshot_payload?.engine?.snapshot?.action).toBe('观察')
    expect(symbol.latest_notification?.snapshot_payload?.activation?.active).toBe(true)
  })

  it('polls each read model every 15 seconds and retains successful data during a refresh', async () => {
    const { queryClient, wrapper } = createWrapper()
    const hongKong = { symbols: [{ symbol: '01347.HK' }], source: 'webstock', source_timestamp: null }
    const unitedStates = { symbols: [{ symbol: 'INTC.US' }], source: 'webstock', source_timestamp: null }
    let resolveRefresh: (value: Response) => void
    fetchMock.mockResolvedValueOnce(response(hongKong))
    fetchMock.mockImplementationOnce(() => new Promise<Response>((resolve) => { resolveRefresh = resolve }))

    const { result, rerender } = renderHook(
      ({ market }) => useDowMonitorOverview(market),
      { initialProps: { market: 'hk' as DowMonitorMarket }, wrapper },
    )

    await waitFor(() => expect(result.current.data).toEqual(hongKong))
    rerender({ market: 'us' })
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2))
    expect(result.current.data).toEqual(hongKong)
    expect(result.current.isPlaceholderData).toBe(true)

    await act(async () => resolveRefresh!(response(unitedStates)))
    await waitFor(() => expect(result.current.data).toEqual(unitedStates))

    renderHook(() => {
      useDowNotifications('us')
      useDowMonitorDetail('INTC.US', '15m')
    }, { wrapper })
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(4))

    for (const queryKey of [
      ['dow-monitor', 'overview', 'us'],
      ['dow-monitor', 'notifications', 'us'],
      ['dow-monitor', 'detail', 'INTC.US', '15m'],
    ]) {
      const query = queryClient.getQueryCache().find({ queryKey, exact: true })
      const options = query?.options as
        | { refetchInterval?: number; placeholderData?: unknown }
        | undefined
      expect(options?.refetchInterval).toBe(15_000)
      expect(options?.placeholderData).toBeDefined()
    }
  })

  it('invalidates only affected Dow monitor query families after mutations', async () => {
    const { queryClient, wrapper } = createWrapper()
    const invalidateQueries = vi.spyOn(queryClient, 'invalidateQueries')
    const removeQueries = vi.spyOn(queryClient, 'removeQueries')
    const { result } = renderHook(
      () => ({
        add: useAddDowMonitorSymbol(),
        remove: useRemoveDowMonitorSymbol(),
        setEnabled: useSetDowMonitorEnabled(),
        markRead: useMarkDowNotificationRead(),
      }),
      { wrapper },
    )

    await act(async () => {
      await result.current.add.mutateAsync({ symbol: '01347.HK' })
      await result.current.remove.mutateAsync('01347.HK')
      await result.current.setEnabled.mutateAsync({ symbol: '01347.HK', enabled: false })
      await result.current.markRead.mutateAsync('notification-1')
    })

    expect(removeQueries).toHaveBeenCalledWith({ queryKey: ['dow-monitor', 'detail', '01347.HK'] })
    expect(invalidateQueries).toHaveBeenCalledWith({ queryKey: ['dow-monitor', 'overview'] })
    expect(invalidateQueries).toHaveBeenCalledWith({ queryKey: ['dow-monitor', 'symbols'] })
    expect(invalidateQueries).toHaveBeenCalledWith({ queryKey: ['dow-monitor', 'notifications'] })
    for (const [argument] of invalidateQueries.mock.calls) {
      const filter = argument as { queryKey?: readonly unknown[] } | undefined
      expect(filter?.queryKey?.[0]).toBe('dow-monitor')
    }
  })
})

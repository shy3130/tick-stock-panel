import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import {
  RealtimeMarketDataClient,
  reconnectDelayMs,
  type RealtimeDataMessage,
  type RealtimeWireMessage,
} from './realtimeMarketData'

class FakeSocket {
  readonly sent: unknown[] = []
  readyState = 1
  onopen: (() => void) | null = null
  onmessage: ((event: { data: string }) => void) | null = null
  onclose: (() => void) | null = null
  onerror: (() => void) | null = null

  send(value: string) {
    this.sent.push(JSON.parse(value))
  }

  close() {
    this.readyState = 3
  }

  message(value: RealtimeWireMessage) {
    this.onmessage?.({ data: JSON.stringify(value) })
  }
}

function update(
  overrides: Partial<RealtimeWireMessage> & {
    streamId?: string
    sequence?: number
    lastDone?: number
  } = {},
): RealtimeDataMessage {
  return {
    type: 'update',
    version: 'v1',
    streamId: overrides.streamId ?? 's1',
    sequence: overrides.sequence ?? 1,
    symbol: '700.HK',
    market: 'hk',
    eventAt: '2026-07-24T10:00:00+08:00',
    publishedAt: '2026-07-24T10:00:00.100+08:00',
    datasets: {
      quote: {
        lastDone: overrides.lastDone ?? 550,
        timestamp: '2026-07-24T10:00:00+08:00',
      },
      depth: {
        bids: [{ position: 1, price: 549.8, volume: 100, orderCount: 1 }],
        asks: [{ position: 1, price: 550, volume: 100, orderCount: 1 }],
        timestamp: '2026-07-24T10:00:00+08:00',
      },
      candlestick: {
        period: 'min_1',
        timestamp: '2026-07-24T10:00:00+08:00',
        open: 549,
        high: 551,
        low: 548,
        close: overrides.lastDone ?? 550,
        volume: 1000,
        turnover: 550000,
      },
    },
    ...overrides,
  } as RealtimeDataMessage
}

function snapshot(overrides: Parameters<typeof update>[0] = {}): RealtimeWireMessage {
  const message = update(overrides)
  return { ...message, type: 'snapshot' }
}

describe('RealtimeMarketDataClient', () => {
  let socket: FakeSocket
  let client: RealtimeMarketDataClient

  beforeEach(() => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-07-24T02:00:00Z'))
    socket = new FakeSocket()
    client = new RealtimeMarketDataClient({
      socketFactory: () => socket,
      random: () => 0.5,
      marketOpen: () => true,
    })
  })

  afterEach(() => {
    client.dispose()
    vi.useRealTimers()
  })

  it('subscribes once and rejects out-of-order state from the same stream', () => {
    client.subscribe(['700.HK'], ['quote', 'depth', 'candlestick'], 10)

    expect(socket.sent.at(-1)).toEqual(expect.objectContaining({
      action: 'subscribe',
      symbols: ['700.HK'],
      depthLevels: 10,
    }))

    socket.message(update({ streamId: 's1', sequence: 2, lastDone: 551 }))
    socket.message(update({ streamId: 's1', sequence: 1, lastDone: 550 }))
    socket.message(update({ streamId: 's1', sequence: 2, lastDone: 552 }))

    expect(client.getState('700.HK')?.quote?.lastDone).toBe(551)
  })

  it('maps leading-zero Hong Kong aliases to the collector symbol', () => {
    client.subscribe(['01347.HK', '0981.HK', '002714.SZ'], ['quote'], 1)

    expect(socket.sent.at(-1)).toEqual(expect.objectContaining({
      action: 'subscribe',
      symbols: ['002714.SZ', '1347.HK', '981.HK'],
    }))

    socket.message(snapshot({
      symbol: '1347.HK',
      lastDone: 149.8,
    }))

    expect(client.getState('01347.HK')?.quote?.lastDone).toBe(149.8)
  })

  it('starts fallback at three seconds and recovers only after a valid snapshot', () => {
    client.subscribe(['700.HK'], ['quote'], 1)
    socket.message(update({ streamId: 's1', sequence: 1 }))

    vi.advanceTimersByTime(3000)
    expect(client.getStatus()).toBe('fallback')

    socket.message(snapshot({ streamId: 's2', sequence: 1 }))
    expect(client.getStatus()).toBe('realtime')
  })

  it('falls back after 45 seconds without heartbeat or update', () => {
    client.subscribe(['700.HK'], ['quote'], 1)
    socket.message(snapshot())

    vi.advanceTimersByTime(44_999)
    expect(client.getStatus()).toBe('realtime')
    vi.advanceTimersByTime(1)
    expect(client.getStatus()).toBe('fallback')
  })

  it('accepts a changed stream and deduplicates an unchanged stream', () => {
    client.subscribe(['700.HK'], ['quote'], 1)
    socket.message(snapshot({ streamId: 'old', sequence: 20, lastDone: 550 }))
    socket.message(update({ streamId: 'new', sequence: 1, lastDone: 551 }))
    socket.message(update({ streamId: 'new', sequence: 1, lastDone: 552 }))

    const state = client.getState('700.HK')
    expect(state?.streamId).toBe('new')
    expect(state?.sequence).toBe(1)
    expect(state?.quote?.lastDone).toBe(551)
  })

  it('coalesces a burst of realtime messages into one visible snapshot per second', () => {
    client.subscribe(['700.HK'], ['quote'], 1)
    socket.message(snapshot({ sequence: 1, lastDone: 550 }))
    const listener = vi.fn()
    const unsubscribe = client.subscribeStore(listener)

    for (let sequence = 2; sequence <= 21; sequence += 1) {
      socket.message(update({ sequence, lastDone: 550 + sequence }))
    }

    expect(client.getState('700.HK')?.quote?.lastDone).toBe(571)
    expect(client.getSnapshot().states.get('700.HK')?.quote?.lastDone).toBe(550)
    expect(listener).not.toHaveBeenCalled()

    vi.advanceTimersByTime(999)
    expect(listener).not.toHaveBeenCalled()
    vi.advanceTimersByTime(1)

    expect(listener).toHaveBeenCalledTimes(1)
    expect(client.getSnapshot().states.get('700.HK')?.quote?.lastDone).toBe(571)
    unsubscribe()
  })

  it('publishes the latest state for every changed symbol at the batch boundary', () => {
    client.subscribe(['700.HK', '981.HK'], ['quote'], 1)
    const listener = vi.fn()
    const unsubscribe = client.subscribeStore(listener)

    socket.message(update({ symbol: '700.HK', sequence: 1, lastDone: 551 }))
    socket.message(update({ symbol: '981.HK', sequence: 1, lastDone: 66 }))
    socket.message(update({ symbol: '700.HK', sequence: 2, lastDone: 552 }))

    expect(listener).not.toHaveBeenCalled()
    vi.advanceTimersByTime(1000)

    expect(listener).toHaveBeenCalledTimes(1)
    expect(client.getSnapshot().states.get('700.HK')?.quote?.lastDone).toBe(552)
    expect(client.getSnapshot().states.get('981.HK')?.quote?.lastDone).toBe(66)
    unsubscribe()
  })

  it('marks datasets delayed only while their market is open', () => {
    client.dispose()
    socket = new FakeSocket()
    client = new RealtimeMarketDataClient({
      socketFactory: () => socket,
      marketOpen: () => true,
      livenessMs: 300_000,
    })
    client.subscribe(['700.HK'], ['quote', 'depth', 'candlestick'], 1)
    socket.message(snapshot())

    vi.advanceTimersByTime(119_999)
    expect(client.getState('700.HK')).toEqual(expect.objectContaining({
      quoteDelayed: false,
      depthDelayed: false,
      candlestickDelayed: false,
    }))

    vi.advanceTimersByTime(1)
    expect(client.getState('700.HK')).toEqual(expect.objectContaining({
      quoteDelayed: true,
      depthDelayed: true,
      candlestickDelayed: false,
    }))
    vi.advanceTimersByTime(60_000)
    expect(client.getState('700.HK')?.candlestickDelayed).toBe(true)

    client.dispose()
    socket = new FakeSocket()
    client = new RealtimeMarketDataClient({
      socketFactory: () => socket,
      marketOpen: () => false,
    })
    client.subscribe(['700.HK'], ['quote', 'depth', 'candlestick'], 1)
    socket.message(snapshot())
    vi.advanceTimersByTime(90_000)
    expect(client.getState('700.HK')).toEqual(expect.objectContaining({
      quoteDelayed: false,
      depthDelayed: false,
      candlestickDelayed: false,
    }))
  })

  it('does not mark a quiet open-market stream delayed while socket liveness is healthy', () => {
    client.subscribe(['700.HK'], ['quote', 'depth', 'candlestick'], 1)
    socket.message(snapshot())

    vi.advanceTimersByTime(30_000)

    expect(client.getStatus()).toBe('realtime')
    expect(client.getState('700.HK')).toEqual(expect.objectContaining({
      quoteDelayed: false,
      depthDelayed: false,
      candlestickDelayed: false,
    }))
  })

  it('unsubscribes removed symbols when a consumer unmounts', () => {
    const unsubscribe = client.subscribe(['700.HK'], ['quote'], 1)

    unsubscribe()

    expect(socket.sent.at(-1)).toEqual({
      action: 'unsubscribe',
      symbols: ['700.HK'],
    })
    vi.advanceTimersByTime(0)
    expect(client.getStatus()).toBe('disconnected')
  })

  it('reuses the open socket when React replaces the last consumer', () => {
    client.dispose()
    const sockets: FakeSocket[] = []
    client = new RealtimeMarketDataClient({
      socketFactory: () => {
        const next = new FakeSocket()
        sockets.push(next)
        return next
      },
      random: () => 0.5,
      marketOpen: () => true,
    })

    const unsubscribe = client.subscribe(['NVDA.US'], ['quote'], 1)
    unsubscribe()
    client.subscribe(['NVDA.US', 'UAA.US'], ['quote'], 1)
    vi.advanceTimersByTime(0)

    expect(sockets).toHaveLength(1)
    expect(sockets[0].readyState).toBe(1)
    expect(sockets[0].sent.at(-1)).toEqual(expect.objectContaining({
      action: 'subscribe',
      symbols: ['NVDA.US', 'UAA.US'],
    }))
  })

  it('caps jittered exponential reconnect delay at 15 seconds', () => {
    expect(reconnectDelayMs(0, () => 0)).toBe(400)
    expect(reconnectDelayMs(0, () => 1)).toBe(600)
    expect(reconnectDelayMs(20, () => 1)).toBe(15_000)
  })
})

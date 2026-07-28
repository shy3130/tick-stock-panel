import { useEffect, useMemo, useSyncExternalStore } from 'react'

import { isMarketOpen } from './intraday-market'

export type RealtimeDataset = 'quote' | 'depth' | 'candlestick'
export type RealtimeStatus = 'connecting' | 'realtime' | 'fallback' | 'disconnected'

export interface RealtimeQuote {
  lastDone?: number
  prevClose?: number
  open?: number
  high?: number
  low?: number
  volume?: number
  turnover?: number
  tradeStatus?: string
  timestamp?: string
}

export interface RealtimeDepthLevel {
  position?: number
  price?: number
  volume?: number
  orderCount?: number
}

export interface RealtimeDepth {
  bids: RealtimeDepthLevel[]
  asks: RealtimeDepthLevel[]
  timestamp?: string
}

export interface RealtimeCandlestick {
  period: 'min_1'
  timestamp: string
  open?: number
  high?: number
  low?: number
  close?: number
  volume?: number
  turnover?: number
}

export interface RealtimeSymbolState {
  symbol: string
  streamId: string
  sequence: number
  eventAt: string
  publishedAt: string
  quote?: RealtimeQuote
  depth?: RealtimeDepth
  candlestick?: RealtimeCandlestick
  quoteDelayed: boolean
  depthDelayed: boolean
  candlestickDelayed: boolean
}

export interface RealtimeDataMessage {
  type: 'snapshot' | 'update'
  version: 'v1'
  streamId: string
  sequence: number
  symbol: string
  market: 'cn' | 'hk' | 'us'
  eventAt: string
  publishedAt: string
  datasets: {
    quote?: RealtimeQuote
    depth?: RealtimeDepth
    candlestick?: RealtimeCandlestick
  }
}

type RealtimeControlMessage =
  | { type: 'hello'; version: 'v1'; serverTime: string; heartbeatSeconds: number }
  | { type: 'heartbeat'; version: 'v1'; serverTime: string }
  | { type: 'fallback'; version: 'v1'; reason?: string }
  | { type: 'error'; version: 'v1'; detail?: string }
  | { type: 'subscribed' | 'unsubscribed'; version: 'v1'; symbols: string[] }

export type RealtimeWireMessage = RealtimeDataMessage | RealtimeControlMessage

interface SocketLike {
  readyState: number
  onopen: ((event: any) => any) | null
  onmessage: ((event: any) => any) | null
  onclose: ((event: any) => any) | null
  onerror: ((event: any) => any) | null
  send(value: string): void
  close(): void
}

interface ClientOptions {
  socketFactory?: (url: string) => SocketLike
  random?: () => number
  now?: () => number
  marketOpen?: (symbol: string, now: Date) => boolean
  initialFallbackMs?: number
  livenessMs?: number
}

interface Consumer {
  symbols: Set<string>
  datasets: Set<RealtimeDataset>
  depthLevels: number
}

interface ViewSnapshot {
  status: RealtimeStatus
  states: ReadonlyMap<string, RealtimeSymbolState>
}

const SOCKET_OPEN = 1
const QUOTE_DEPTH_DELAY_MS = 120_000
const CANDLESTICK_DELAY_MS = 180_000
const EMPTY_SNAPSHOT: ViewSnapshot = {
  status: 'disconnected',
  states: new Map(),
}

function socketUrl(): string {
  const scheme = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
  return `${scheme}//${window.location.host}/ws/realtime`
}

export function canonicalRealtimeSymbol(symbol: string): string {
  const normalized = symbol.trim().toUpperCase()
  const match = /^(\d{1,5})\.HK$/.exec(normalized)
  if (!match) return normalized
  return `${match[1].replace(/^0+(?=\d)/, '')}.HK`
}

export function reconnectDelayMs(
  attempt: number,
  random: () => number = Math.random,
): number {
  const jitter = 0.8 + random() * 0.4
  return Math.min(15_000, Math.round(500 * 2 ** attempt * jitter))
}

export class RealtimeMarketDataClient {
  private readonly socketFactory: (url: string) => SocketLike
  private readonly random: () => number
  private readonly now: () => number
  private readonly marketOpen: (symbol: string, now: Date) => boolean
  private readonly initialFallbackMs: number
  private readonly livenessMs: number
  private readonly consumers = new Map<number, Consumer>()
  private readonly listeners = new Set<() => void>()
  private readonly receivedAt = new Map<
    string,
    Partial<Record<RealtimeDataset, number>>
  >()
  private states = new Map<string, RealtimeSymbolState>()
  private view: ViewSnapshot = EMPTY_SNAPSHOT
  private status: RealtimeStatus = 'disconnected'
  private socket: SocketLike | null = null
  private nextConsumerId = 1
  private reconnectAttempt = 0
  private acceptedSnapshot = false
  private disposed = false
  private initialFallbackTimer: ReturnType<typeof setTimeout> | undefined
  private livenessTimer: ReturnType<typeof setTimeout> | undefined
  private reconnectTimer: ReturnType<typeof setTimeout> | undefined
  private stopTimer: ReturnType<typeof setTimeout> | undefined
  private freshnessTimer: ReturnType<typeof setInterval> | undefined

  constructor(options: ClientOptions = {}) {
    this.socketFactory = options.socketFactory ?? (url => new WebSocket(url))
    this.random = options.random ?? Math.random
    this.now = options.now ?? Date.now
    this.marketOpen = options.marketOpen ?? isMarketOpen
    this.initialFallbackMs = options.initialFallbackMs ?? 3000
    this.livenessMs = options.livenessMs ?? 45_000
  }

  getStatus = (): RealtimeStatus => this.status

  getState = (symbol: string): RealtimeSymbolState | undefined =>
    this.states.get(canonicalRealtimeSymbol(symbol))

  getSnapshot = (): ViewSnapshot => this.view

  subscribeStore = (listener: () => void): (() => void) => {
    this.listeners.add(listener)
    return () => this.listeners.delete(listener)
  }

  subscribe(
    symbols: string[],
    datasets: RealtimeDataset[],
    depthLevels: number,
  ): () => void {
    if (this.disposed) throw new Error('realtime client is disposed')
    this.cancelScheduledStop()
    const consumer: Consumer = {
      symbols: new Set(symbols.map(canonicalRealtimeSymbol).filter(Boolean)),
      datasets: new Set(datasets),
      depthLevels: Math.max(1, Math.min(10, Math.trunc(depthLevels))),
    }
    const id = this.nextConsumerId++
    const before = this.aggregate()
    this.consumers.set(id, consumer)
    const after = this.aggregate()
    if (after.symbols.size > 0) {
      if (before.symbols.size === 0) {
        if (this.socket?.readyState === SOCKET_OPEN) {
          this.sendSubscription(after)
        } else {
          this.acceptedSnapshot = false
          this.setStatus('connecting')
          this.startInitialFallback()
          this.startFreshnessTimer()
          this.connect()
        }
      } else {
        this.sendSubscription(after)
      }
    }

    return () => {
      const previous = this.aggregate()
      this.consumers.delete(id)
      const current = this.aggregate()
      const removed = [...previous.symbols].filter(symbol => !current.symbols.has(symbol))
      if (removed.length > 0) {
        this.send({ action: 'unsubscribe', symbols: removed.sort() })
      }
      if (current.symbols.size === 0) {
        this.scheduleStopConnection()
      } else {
        this.sendSubscription(current)
      }
    }
  }

  dispose(): void {
    this.disposed = true
    this.consumers.clear()
    this.stopConnection()
    this.listeners.clear()
  }

  private aggregate(): {
    symbols: Set<string>
    datasets: Set<RealtimeDataset>
    depthLevels: number
  } {
    const symbols = new Set<string>()
    const datasets = new Set<RealtimeDataset>()
    let depthLevels = 1
    for (const consumer of this.consumers.values()) {
      consumer.symbols.forEach(symbol => symbols.add(symbol))
      consumer.datasets.forEach(dataset => datasets.add(dataset))
      depthLevels = Math.max(depthLevels, consumer.depthLevels)
    }
    return { symbols, datasets, depthLevels }
  }

  private connect(): void {
    if (this.disposed || this.aggregate().symbols.size === 0) return
    if (this.socket && this.socket.readyState <= SOCKET_OPEN) return
    const socket = this.socketFactory(socketUrl())
    this.socket = socket
    const onOpen = () => {
      this.reconnectAttempt = 0
      this.sendSubscription(this.aggregate())
    }
    socket.onopen = onOpen
    socket.onmessage = event => this.handleRawMessage(event.data)
    socket.onerror = () => this.handleDisconnect(socket)
    socket.onclose = () => this.handleDisconnect(socket)
    if (socket.readyState === SOCKET_OPEN) onOpen()
  }

  private handleRawMessage(raw: string): void {
    let message: RealtimeWireMessage
    try {
      message = JSON.parse(raw) as RealtimeWireMessage
    } catch {
      return
    }
    if (!message || message.version !== 'v1' || typeof message.type !== 'string') return
    if (['hello', 'heartbeat', 'snapshot', 'update'].includes(message.type)) {
      this.resetLiveness()
    }
    if (message.type === 'fallback') {
      this.setStatus('fallback')
      return
    }
    if (message.type !== 'snapshot' && message.type !== 'update') return
    if (!this.applyDataMessage(message)) return
    if (message.type === 'snapshot') {
      this.acceptedSnapshot = true
      clearTimeout(this.initialFallbackTimer)
      this.initialFallbackTimer = undefined
      this.setStatus('realtime')
    }
  }

  private applyDataMessage(message: RealtimeDataMessage): boolean {
    if (
      typeof message.symbol !== 'string'
      || typeof message.streamId !== 'string'
      || !Number.isInteger(message.sequence)
      || message.sequence <= 0
      || !message.datasets
    ) return false
    const symbol = canonicalRealtimeSymbol(message.symbol)
    const previous = this.states.get(symbol)
    const sameStream = previous?.streamId === message.streamId
    if (sameStream && message.sequence <= (previous?.sequence ?? 0)) return false

    const base = sameStream ? previous : undefined
    const received = sameStream ? (this.receivedAt.get(symbol) ?? {}) : {}
    const receivedNow = this.now()
    for (const dataset of ['quote', 'depth', 'candlestick'] as const) {
      if (message.datasets[dataset]) received[dataset] = receivedNow
    }
    this.receivedAt.set(symbol, received)
    const next: RealtimeSymbolState = {
      symbol,
      streamId: message.streamId,
      sequence: message.sequence,
      eventAt: message.eventAt,
      publishedAt: message.publishedAt,
      quote: message.datasets.quote ?? base?.quote,
      depth: message.datasets.depth ?? base?.depth,
      candlestick: message.datasets.candlestick ?? base?.candlestick,
      quoteDelayed: false,
      depthDelayed: false,
      candlestickDelayed: false,
    }
    this.states = new Map(this.states).set(symbol, this.withFreshness(next, receivedNow))
    this.emit()
    return true
  }

  private withFreshness(
    state: RealtimeSymbolState,
    currentTime = this.now(),
  ): RealtimeSymbolState {
    if (!this.marketOpen(state.symbol, new Date(currentTime))) {
      return {
        ...state,
        quoteDelayed: false,
        depthDelayed: false,
        candlestickDelayed: false,
      }
    }
    const received = this.receivedAt.get(state.symbol) ?? {}
    return {
      ...state,
      quoteDelayed: Boolean(
        state.quote
        && currentTime - (received.quote ?? currentTime) >= QUOTE_DEPTH_DELAY_MS,
      ),
      depthDelayed: Boolean(
        state.depth
        && currentTime - (received.depth ?? currentTime) >= QUOTE_DEPTH_DELAY_MS,
      ),
      candlestickDelayed: Boolean(
        state.candlestick
        && currentTime - (received.candlestick ?? currentTime) >= CANDLESTICK_DELAY_MS,
      ),
    }
  }

  private refreshFreshness(): void {
    let changed = false
    const next = new Map(this.states)
    for (const [symbol, state] of next) {
      const freshened = this.withFreshness(state)
      if (
        freshened.quoteDelayed !== state.quoteDelayed
        || freshened.depthDelayed !== state.depthDelayed
        || freshened.candlestickDelayed !== state.candlestickDelayed
      ) {
        next.set(symbol, freshened)
        changed = true
      }
    }
    if (changed) {
      this.states = next
      this.emit()
    }
  }

  private startFreshnessTimer(): void {
    if (!this.freshnessTimer) {
      this.freshnessTimer = setInterval(() => this.refreshFreshness(), 1000)
    }
  }

  private startInitialFallback(): void {
    clearTimeout(this.initialFallbackTimer)
    this.initialFallbackTimer = setTimeout(() => {
      if (!this.acceptedSnapshot) this.setStatus('fallback')
    }, this.initialFallbackMs)
  }

  private resetLiveness(): void {
    clearTimeout(this.livenessTimer)
    this.livenessTimer = setTimeout(() => {
      this.setStatus('fallback')
      const socket = this.socket
      this.socket = null
      socket?.close()
      this.scheduleReconnect()
    }, this.livenessMs)
  }

  private handleDisconnect(socket: SocketLike): void {
    if (this.socket !== socket) return
    this.socket = null
    if (this.disposed || this.aggregate().symbols.size === 0) return
    this.setStatus('fallback')
    this.scheduleReconnect()
  }

  private scheduleReconnect(): void {
    if (this.reconnectTimer || this.disposed || this.aggregate().symbols.size === 0) return
    const delay = reconnectDelayMs(this.reconnectAttempt++, this.random)
    this.reconnectTimer = setTimeout(() => {
      this.reconnectTimer = undefined
      this.connect()
    }, delay)
  }

  private sendSubscription(subscription: ReturnType<RealtimeMarketDataClient['aggregate']>): void {
    if (subscription.symbols.size === 0 || subscription.datasets.size === 0) return
    this.send({
      action: 'subscribe',
      symbols: [...subscription.symbols].sort(),
      datasets: [...subscription.datasets].sort(),
      depthLevels: subscription.depthLevels,
    })
  }

  private send(message: object): void {
    if (this.socket?.readyState === SOCKET_OPEN) {
      this.socket.send(JSON.stringify(message))
    }
  }

  private cancelScheduledStop(): void {
    clearTimeout(this.stopTimer)
    this.stopTimer = undefined
  }

  private scheduleStopConnection(): void {
    this.cancelScheduledStop()
    this.stopTimer = setTimeout(() => {
      this.stopTimer = undefined
      if (this.aggregate().symbols.size === 0) this.stopConnection()
    }, 0)
  }

  private stopConnection(): void {
    this.cancelScheduledStop()
    clearTimeout(this.initialFallbackTimer)
    clearTimeout(this.livenessTimer)
    clearTimeout(this.reconnectTimer)
    clearInterval(this.freshnessTimer)
    this.initialFallbackTimer = undefined
    this.livenessTimer = undefined
    this.reconnectTimer = undefined
    this.freshnessTimer = undefined
    const socket = this.socket
    this.socket = null
    socket?.close()
    this.setStatus('disconnected')
  }

  private setStatus(status: RealtimeStatus): void {
    if (this.status === status) return
    this.status = status
    this.emit()
  }

  private emit(): void {
    this.view = { status: this.status, states: this.states }
    this.listeners.forEach(listener => listener())
  }
}

let sharedClient: RealtimeMarketDataClient | undefined

function getSharedClient(): RealtimeMarketDataClient {
  if (!sharedClient) sharedClient = new RealtimeMarketDataClient()
  return sharedClient
}

export function useRealtimeMarketData(
  symbols: string[],
  datasets: RealtimeDataset[],
  depthLevels: number,
): {
  status: RealtimeStatus
  states: ReadonlyMap<string, RealtimeSymbolState>
} {
  const client = getSharedClient()
  const symbolKey = [...new Set(symbols.map(value => value.trim().toUpperCase()).filter(Boolean))]
    .sort()
    .join(',')
  const datasetKey = [...new Set(datasets)].sort().join(',')

  useEffect(
    () => client.subscribe(
      symbolKey ? symbolKey.split(',') : [],
      datasetKey ? datasetKey.split(',') as RealtimeDataset[] : [],
      depthLevels,
    ),
    [client, symbolKey, datasetKey, depthLevels],
  )
  const snapshot = useSyncExternalStore(
    client.subscribeStore,
    client.getSnapshot,
    () => EMPTY_SNAPSHOT,
  )
  const states = useMemo(() => {
    const selected = new Map<string, RealtimeSymbolState>()
    for (const symbol of symbolKey ? symbolKey.split(',') : []) {
      const state = snapshot.states.get(canonicalRealtimeSymbol(symbol))
      if (state) selected.set(symbol, state)
    }
    return selected
  }, [snapshot.states, symbolKey])
  return { status: snapshot.status, states }
}

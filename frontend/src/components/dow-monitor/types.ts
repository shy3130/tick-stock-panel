export type DowMonitorMarket = 'all' | 'cn' | 'hk' | 'us'
export type DowMonitorSymbolMarket = Exclude<DowMonitorMarket, 'all'>
export type DowTimeframe = '5m' | '15m' | '30m' | '60m' | 'day'
export type DowFreshnessState = 'LIVE' | 'STALE_DATA' | 'ANALYSIS_PAUSED'
export type DowSignalSide = 'BUY' | 'SELL' | 'RISK'

export interface DowMonitorSymbol {
  symbol: string
  market: DowMonitorSymbolMarket
  enabled: boolean
  created_at: string
  updated_at: string
}

export interface DowMonitorBar {
  index: number
  timestamp: string
  open: number
  high: number
  low: number
  close: number
  volume: number
  ma5?: number | null
  ma10?: number | null
  ma20?: number | null
  ma60?: number | null
  macd_dif?: number | null
  macd_dea?: number | null
  macd_hist?: number | null
  rsi_6?: number | null
  rsi_14?: number | null
  rsi_24?: number | null
  kdj_k?: number | null
  kdj_d?: number | null
  kdj_j?: number | null
  boll_upper?: number | null
  boll_lower?: number | null
  vol_ma5?: number | null
  vol_ma10?: number | null
  vol_ratio_5d?: number | null
}

export interface DowMonitorLine {
  id: string
  side: string
  role: string
  generation: number
  anchorIndexes: [number, number]
  anchorTimes: [string, string]
  anchorPrices: [number, number]
  createdIndex: number
  invalidatedIndex: number | null
  controlsSignals: boolean
}

export interface DowMonitorSignalEvidenceDetail {
  name: string
  value: unknown
}

export interface DowMonitorSignalEvidence {
  code: string
  detector: string
  side: string
  barIndex: number
  strength: string
  structureId: string | null
  details: DowMonitorSignalEvidenceDetail[]
}

export interface DowMonitorSignal {
  side: string
  barIndex: number
  barTime: string
  price: number
  reason: string
  confidence: string
  lineId: string | null
  firstCrossIndex: number | null
  firstCrossTime: string | null
  volumeRatio: number | null
  pattern: string | null
  evidence: DowMonitorSignalEvidence[]
}

export interface DowMonitorSnapshot {
  symbol: string
  timeframe: string
  bar_time: string
  bar_completion: string
  provisional: boolean
  phase: string
  phase_code: string
  candle_pattern: string | null
  line_id: string | null
  line_role: string | null
  line_side: string | null
  line_anchor_times: string[]
  line_value: number | null
  price_to_line_pct: number | null
  sequence_count: number
  volume_ratio_20: number | null
  volume_confirmation: string
  action: string
  action_code: string
  reason_codes: string[]
}

export interface DowMonitorLongTermSnapshot {
  symbol: string
  timeframe: string
  bar_time: string
  bar_completion: 'FINAL' | 'FORMING'
  provisional: boolean
  trend_direction: 'UP' | 'DOWN' | 'RANGE' | 'UNKNOWN'
  trend_name: string
  pattern_name: string
  operation: '观察' | '买入触发' | '卖出触发' | '持有' | '无操作'
  signal_stage: 'NONE' | 'WARNING' | 'TRIGGER' | 'CONFIRMED'
  breakout_type: 'NONE' | 'TREND_LINE' | 'KEY_LEVEL' | 'DOUBLE_BREAKOUT' | 'RETEST'
  line_id: string | null
  line_side: string | null
  line_status: string | null
  first_anchor_time: string | null
  first_anchor_price: number | null
  second_anchor_time: string | null
  second_anchor_price: number | null
  line_value: number | null
  key_level_type: string | null
  key_level_time: string | null
  key_level_price: number | null
  first_break_time: string | null
  recent_low_scale: 'PRIMARY' | null
  recent_low_label: string | null
  recent_low_time: string | null
  recent_low_price: number | null
  recent_low_confirmed_time: string | null
  evidence_codes: string[]
  failure_reason: string | null
}

/** Persisted sidecars may predate the current strict engine schema. */
export type DowMonitorPersistedLongTermSnapshot = Partial<DowMonitorLongTermSnapshot> & {
  trendDirection?: string
} & Record<string, unknown>

export interface DowMonitorChart {
  bars?: DowMonitorBar[]
  lines?: DowMonitorLine[]
  signals?: DowMonitorSignal[]
  longTerm?: DowMonitorPersistedLongTermSnapshot
}

export interface DowMonitorTimeframeState {
  symbol: string
  market: DowMonitorSymbolMarket
  timeframe: DowTimeframe
  freshness_state: DowFreshnessState
  source_timestamp: string | null
  snapshot: Partial<DowMonitorSnapshot>
  chart: DowMonitorChart
  updated_at: string
}

export interface DowMonitorEnginePayload {
  symbol: string
  timeframe: DowTimeframe
  snapshot: DowMonitorSnapshot
  bars: DowMonitorBar[]
  lines: DowMonitorLine[]
  signals: DowMonitorSignal[]
  longTerm: DowMonitorLongTermSnapshot
  evaluatedAt: string
}

export type DowMonitorPersistedEnginePayload = Partial<
  Omit<DowMonitorEnginePayload, 'snapshot' | 'longTerm'>
> & {
  snapshot?: Partial<DowMonitorSnapshot> & Record<string, unknown>
  longTerm?: DowMonitorPersistedLongTermSnapshot
} & Record<string, unknown>

export interface DowMonitorActivationSnapshot {
  active: boolean
  family: string
  structure_id: string
  activation_sequence: number
}

export interface DowMonitorNotificationSnapshot {
  engine?: DowMonitorPersistedEnginePayload
  current_ohlc?: Omit<DowMonitorBar, 'index'>
  source_timestamp?: string | null
  activation?: DowMonitorActivationSnapshot
}

export interface DowMonitorNotification {
  notification_id: string
  event_key: string
  symbol: string
  market: DowMonitorSymbolMarket
  timeframe: DowTimeframe
  side: DowSignalSide
  action_name: string
  shape_name: string
  triggered_at: string
  trigger_price: number
  snapshot_payload: DowMonitorNotificationSnapshot
  read_at: string | null
}

export interface DowMonitorOverviewSymbol extends DowMonitorSymbol {
  name: string | null
  last_price: number | null
  change_pct: number | null
  quote_timestamp: number | string | null
  states: Partial<Record<DowTimeframe, DowMonitorTimeframeState>>
  latest_notification: DowMonitorNotification | null
  last_success_at: string | null
  last_error: string | null
}

export interface DowMonitorOverviewResponse {
  symbols: DowMonitorOverviewSymbol[]
  source: string
  source_timestamp: string | null
}

export interface DowMonitorNotificationsResponse {
  notifications: DowMonitorNotification[]
}

export interface DowMonitorDetailResponse extends DowMonitorTimeframeState {
  last_success_at: string | null
  last_error: string | null
}

export interface DowMonitorStatusResponse {
  running: boolean
  poll_seconds: number
  source: string
  last_started_at: string | null
  last_completed_at: string | null
  last_success_at: string | null
  last_error: string | null
  errors: Record<string, string>
}

export interface DowMonitorSymbolsResponse {
  symbols: DowMonitorSymbol[]
}

export interface DowMonitorRemoveSymbolResponse {
  symbol: string
  removed: true
}

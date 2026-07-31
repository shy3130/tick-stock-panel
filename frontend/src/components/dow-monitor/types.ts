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
  stage?: string | null
  triggerPath?: string | null
  lineValue?: number | null
  lineRole?: string | null
  lineAnchorTimes?: string[] | null
  lineAnchorPrices?: number[] | null
  structurePivotId?: string | null
  structurePivotPrice?: number | null
  structurePivotTime?: string | null
  reasonCodes?: string[]
}

export interface DowMonitorTurningSignal {
  side: string
  stage: string
  detectedIndex: number
  detectedTime: string | null
  actionableIndex: number
  actionableTime: string | null
  price: number
  trendStateBefore: string
  trendStateAfter: string
  lineId: string | null
  lineRole: string | null
  lineGeneration: number | null
  parentLineId: string | null
  lineValue: number | null
  lineAnchorTimes?: string[] | null
  lineAnchorPrices?: number[] | null
  breakDistanceNormalized: number | null
  structurePivotId: string | null
  structurePivotPrice: number | null
  structurePivotTime?: string | null
  triggerPath: string | null
  reasonCodes: string[]
  signalQuality?: {
    side?: string
    entryQuality?: 'ACTIONABLE' | 'WAIT_CONFIRMATION' | 'WEAK' | 'NEUTRAL'
    replayOutcome?: 'PENDING' | 'HELD' | 'FAILED' | 'UNKNOWN'
    score?: number
    summary?: string
    reasonCodes?: string[]
    confirmationIndex?: number | null
  } | null
}

export interface DowMonitorTurningPayload {
  signals?: DowMonitorTurningSignal[]
  lineBreaks?: unknown[]
  lines?: unknown[]
  pivots?: unknown[]
}

export type DowHeadShouldersType = 'BOTTOM' | 'TOP'
export type DowHeadShouldersStage =
  | 'FORMING'
  | 'BREAK_WATCH'
  | 'WICK_CROSS'
  | 'NECKLINE_BREAK_WEAK'
  | 'CONFIRMED'
  | 'RETEST_CONFIRMED'
  | 'FAILED'
  | 'FALSE_BREAKOUT'

export interface DowHeadShouldersPoint {
  role: string
  barIndex: number
  barTime: string
  confirmedIndex?: number
  confirmedTime?: string
  price: number
}

export interface DowHeadShouldersSignal {
  family: 'HEAD_SHOULDERS'
  patternId: string
  side: 'BUY' | 'SELL'
  stage: 'CONFIRMED' | 'RETEST_CONFIRMED'
  barIndex: number
  barTime: string
  price: number
}

export interface DowHeadShouldersPattern {
  id: string
  type: DowHeadShouldersType
  stage: DowHeadShouldersStage
  side: 'BUY' | 'SELL' | null
  signal: DowHeadShouldersSignal | null
  points: {
    leftShoulder: DowHeadShouldersPoint | null
    neckline1: DowHeadShouldersPoint | null
    head: DowHeadShouldersPoint | null
    neckline2: DowHeadShouldersPoint | null
    rightShoulder: DowHeadShouldersPoint | null
    breakout: DowHeadShouldersPoint | null
  }
  neckline: {
    anchorIndexes: [number, number]
    anchorTimes: [string, string]
    anchorPrices: [number, number]
    triggerIndex: number | null
    triggerTime: string | null
    triggerValue: number | null
  } | null
  volume: {
    ratio: number
    requiredRatio: number
    baseline: number
    triggerIndex: number
    triggerTime: string | null
  } | null
  invalidation: {
    price: number | null
  }
  geometryScore: number
  volumeScore: number
  contextScore: number
  qualityScore: number
  evidence: string[]
}

export interface DowHeadShouldersPayload {
  patterns: DowHeadShouldersPattern[]
  signals: DowHeadShouldersSignal[]
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
  turning?: DowMonitorTurningPayload
  headShoulders?: DowHeadShouldersPayload
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
  headShoulders?: DowHeadShouldersPayload
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
  category?: 'BUY_POINT' | 'SELL_POINT' | 'EARLY_RISK'
  available_at?: string
  evidence_text?: string
  prompt_text?: string
}

export interface DowDecisionDriver {
  driver_code: string
  direction: 'BULLISH' | 'BEARISH' | 'NEUTRAL'
  contribution: number
  current_value: number | null
  previous_value: number | null
  change_value: number | null
  unit: string | null
  horizons: string[]
  confirmation: 'CONFIRMED' | 'UNCONFIRMED' | 'CONFLICT' | 'NOT_APPLICABLE'
  text: string
}

export interface DowMinuteRiskWarning {
  family:
    | 'OPENING_SURGE_REVERSAL'
    | 'BUYING_INEFFECTIVE'
    | 'REBOUND_FAILURE'
    | 'KEY_LEVEL_BREAKDOWN'
  stage: 'WATCH' | 'WARNING' | 'CONFIRMED'
  title: string
  message: string
}

export interface DowDailyDecisionPhase {
  code:
    | 'RAPID_RISE_CONFIRMED'
    | 'PRICE_CAPITAL_DIVERGENCE'
    | 'SURGE_REVERSAL_RISK'
    | 'DOWNSIDE_CONFIRMED'
  label: string
  first_observed_at: string
}

export interface DowDailyDecisionEvidence {
  code: string
  text: string
  observed_at: string
}

export interface DowDailyDecisionSummary {
  as_of_minute: string
  direction: 'BULLISH' | 'BEARISH' | 'RANGE'
  direction_label: '偏涨' | '偏跌' | '震荡'
  action: 'WATCH_BUY' | 'HOLD' | 'REDUCE_SELL' | 'OBSERVE'
  action_label: '买入观察' | '持有' | '减仓/卖出' | '继续观察'
  confidence: number
  phase_path: DowDailyDecisionPhase[]
  summary_text: string
  key_evidence: DowDailyDecisionEvidence[]
  reversal_condition: string
  data_status: string
  status_label: string
  current_price?: number | null
  vwap_price?: number | null
  vwap_distance_pct?: number | null
  input_event_ids: string[]
}

export interface DowMinuteDecision {
  symbol: string
  market: DowMonitorSymbolMarket
  decision_minute: string
  direction: 'BULLISH' | 'BEARISH' | 'RANGE'
  direction_label: '偏涨' | '偏跌' | '震荡'
  action: 'WATCH_BUY' | 'HOLD' | 'REDUCE_SELL' | 'OBSERVE'
  action_label: '买入观察' | '持有' | '减仓/卖出' | '继续观察'
  confidence: number
  dominant_timeframe: DowTimeframe | null
  confirmation_timeframes: DowTimeframe[]
  supporting_reasons: string[]
  contrary_risks: string[]
  invalidation_conditions: string[]
  data_status:
    | 'COMPLETE'
      | 'WAITING_NEW_MINUTE'
      | 'DELAYED'
      | 'CAPITAL_UNCONFIRMED'
      | 'CAPITAL_UNAVAILABLE'
      | 'CAPITAL_DELAYED'
      | 'CAPITAL_INSUFFICIENT'
      | 'MARKET_CLOSED'
      | 'INSUFFICIENT_STRUCTURE'
  status_label: string
  source_timestamp: string | null
  summary_text?: string | null
  key_drivers?: DowDecisionDriver[]
  turn_stronger_condition?: string | null
  turn_weaker_condition?: string | null
  risk_warning?: DowMinuteRiskWarning | null
  daily_summary?: DowDailyDecisionSummary | null
}

export interface DowMonitorOverviewSymbol extends DowMonitorSymbol {
  name: string | null
  last_price: number | null
  change_pct: number | null
  quote_timestamp: number | string | null
  completed_minute_timestamp?: string | null
  analysis_timestamp?: string | null
  analysis_status?:
    | 'READY'
    | 'WAITING'
    | 'HISTORY_PENDING'
    | 'QUOTE_DELAYED'
    | 'ANALYSIS_TIMEOUT'
    | 'ANALYSIS_PAUSED'
  analysis_status_label?: string
  next_day_direction?: DowMonitorNextDayDirection | null
  intraday_capital?: DowMonitorIntradayCapital | null
  minute_decision?: DowMinuteDecision | null
  states: Partial<Record<DowTimeframe, DowMonitorTimeframeState>>
  latest_notification: DowMonitorNotification | null
  last_success_at: string | null
  last_error: string | null
  history_backfill?: DowMonitorHistoryBackfillStatus
  half_hour_ai_analysis?: DowMonitorHalfHourAiSummary
}

export interface DowMonitorHistoryBackfillStatus {
  status:
    | 'pending'
    | 'queued'
    | 'running'
    | 'rebuilding'
    | 'completed'
    | 'partial'
    | 'failed'
    | 'unknown'
  progress: number
  missing_timeframes: string[]
  last_error: string | null
  updated_at: string | null
}

export type DowMonitorHalfHourAiStatus =
  | 'pending'
  | 'running'
  | 'completed'
  | 'failed'
  | 'insufficient_data'
  | 'unavailable'

export interface DowMonitorHalfHourAiSummary {
  analysis_id: string | null
  status: DowMonitorHalfHourAiStatus
  window_end: string | null
  title: string | null
  summary: string | null
}

export interface DowMonitorHalfHourAiHistoryItem extends DowMonitorHalfHourAiSummary {
  market: DowMonitorSymbolMarket
  symbol: string
  trade_date: string
  updated_at: string
}

export interface DowMonitorHalfHourAiAnalysis extends DowMonitorHalfHourAiHistoryItem {
  data_cutoff: string
  conclusion: string | null
  evidence: Array<{
    metric_key: string
    label: string
    value: string
    meaning: string
  }>
  risks: string[]
  scenarios: Array<{
    condition: string
    implication: string
    invalidates_when: string
  }>
  data_quality: string[]
}

export interface DowMonitorHalfHourAiHistoryResponse {
  analyses: DowMonitorHalfHourAiHistoryItem[]
}

export interface DowMonitorIntradayCapital {
  capital_minute?: string | null
  total_net?: number | null
  large_net?: number | null
  total_in?: number | null
  total_out?: number | null
  large_net_ratio?: number | null
  flow_15m?: number | null
  flow_30m?: number | null
  flow_today?: number | null
  last_flow_time?: string | null
    flow_points?: number | null
    quality?: 'COMPLETE' | 'UNAVAILABLE' | 'DELAYED' | 'INSUFFICIENT' | string
    windows?: DowMonitorIntradayCapitalWindow[]
  source?: 'trading_day' | string
}

export interface DowMonitorIntradayCapitalWindow {
  label?: string | null
  minutes?: number | null
  start_time?: string | null
  end_time?: string | null
  start_price?: number | null
  end_price?: number | null
  price_change_pct?: number | null
  start_total_net?: number | null
  end_total_net?: number | null
  total_net_delta?: number | null
  start_large_net?: number | null
  end_large_net?: number | null
  large_net_delta?: number | null
}

export interface DowMonitorNextDayDirection {
  symbol: string
  as_of: string | null
  score: number
  probability: number
  direction_label: string
  realtime_signal?: string
  realtime_label?: string
  realtime_reason?: string
  last_price?: number | null
  key_levels: {
    support?: number | null
    resistance?: number | null
    stop?: number | null
    recent_low?: number | null
  }
  metrics?: Record<string, number | null>
  evidence: string[]
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

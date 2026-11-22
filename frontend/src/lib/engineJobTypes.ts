/** Workbench research API. Decimal financial values remain strings. */
export interface EngineStudyRequest {
  study_version?: '1.0'
  mode: 'grid' | 'walk_forward'
  search: {
    target: 'strategy_parameters' | 'controlled_program'
    axes: { name: string; values: (string | number | boolean | null)[] }[]
  }
  objective?: { metric?: 'total_return' | 'maximum_drawdown'; min_filled_orders?: number }
  walk_forward?: {
    train_sessions: number
    test_sessions: number
    step_sessions: number
    gap_sessions?: number
    warmup_sessions?: number
    training?: 'rolling' | 'expanding'
  } | null
  limits?: {
    max_candidates?: number
    max_executions?: number
    max_prepared_bytes?: number
    max_archive_bytes?: number
  }
  account_policy?: 'reset_each_window'
  failure_policy?: 'reject_study'
}

export interface EngineExecutionBudget {
  rss_limit_bytes?: number
  timeout_seconds?: number
  max_attempts?: number
}

export interface EngineCoverage {
  status: 'complete' | 'partial' | 'empty' | 'error'
  requested_units: number
  completed_units: number
  row_count: number
  issues?: {
    instrument_id?: string | null
    reason: 'missing' | 'suspended' | 'no_trades' | 'not_listed' | 'delisted'
      | 'not_published' | 'source_error' | 'cancelled' | 'unsupported' | 'no_events'
    detail: string
  }[]
}

export interface EngineLocalDataInput {
  kind?: 'local_jsonl'
  path: string
  request: {
    dataset_id: string
    record_type: 'bar' | 'instrument' | 'symbol_mapping' | 'opening_observation'
      | 'corporate_action' | 'adjustment_factor' | 'extension_value' | 'order_book_observation'
      | 'trade_print'
    extension_field?: {
      table_id: string
      table_version: string
      field_id: string
      field_version: string
      value_type: 'int' | 'float' | 'bool' | 'string'
      unit: string
      granularity: 'instrument' | 'market'
      time_basis?: string
    }
    market_id: string
    asset_class: 'equity' | 'fund' | 'index' | 'future'
    instrument_ids: string[]
    start: string
    end: string
    timeframe?: string | null
    price_basis?: 'raw' | 'qfq' | 'hfq' | null
  }
  source_id: string
  knowledge_basis: 'historical_versions' | 'latest_reconstruction' | 'unknown'
  bar_convention_id?: string | null
  coverage?: EngineCoverage | null
}

export interface EngineRoutedDailyInput {
  kind: 'tsp_daily'
  request: EngineLocalDataInput['request'] & {
    record_type: 'bar'; market_id: 'cn.equity'; asset_class: 'equity'; timeframe: '1d'; price_basis: 'raw'
  }
  expected_provider: string
  calendar_version: string
  symbols: { instrument_id: string; symbol: string; venue_id: 'XSHG' | 'XSHE' | 'XBSE' }[]
}

export interface EngineRepositoryDailyInput extends Omit<EngineRoutedDailyInput, 'kind' | 'request' | 'calendar_version'> {
  kind: 'tsp_repository_daily'
  layout: 'daily_raw_v1' | 'enriched_v1'
  calendar_version?: string | null
  request: Omit<EngineRoutedDailyInput['request'], 'price_basis'> & { price_basis: 'raw' | 'qfq' }
}

type EngineFactorInput = Pick<EngineRoutedDailyInput, 'expected_provider' | 'symbols'> & {
  request: EngineLocalDataInput['request'] & {
    record_type: 'adjustment_factor'; market_id: 'cn.equity'; asset_class: 'equity'
    timeframe?: null; price_basis?: null
  }
} & (
  | { representation: 'event_ratio'; baseline_date?: null }
  | { representation: 'cumulative_forward'; baseline_date: string }
)

export type EngineRepositoryFactorInput = EngineFactorInput & {
  kind: 'tsp_repository_factors'
  layout: 'event_factors_v1'
}

export type EngineRoutedFactorInput = EngineFactorInput & { kind: 'tsp_factors' }

export interface EngineRoutedMinuteInput extends Omit<EngineRoutedDailyInput, 'kind' | 'request'> {
  kind: 'tsp_minute'
  request: Omit<EngineRoutedDailyInput['request'], 'timeframe'> & { timeframe: '1m' }
}

export interface EngineRepositoryMinuteInput extends Omit<EngineRoutedMinuteInput, 'kind'> {
  kind: 'tsp_repository_minute'
  layout: 'minute_v1'
  minute_label: 'start' | 'end'
  price_evidence?: 'marked_raw' | 'legacy_close_anchor'
}

export interface EngineNativeDataInput {
  kind: 'engine_data_provider'
  provider_id: string
  profile?: string
  request: EngineLocalDataInput['request']
}

type EngineDataInput = EngineLocalDataInput | EngineNativeDataInput | EngineRoutedDailyInput | EngineRoutedFactorInput | EngineRoutedMinuteInput | EngineRepositoryDailyInput | EngineRepositoryFactorInput | EngineRepositoryMinuteInput

export interface DailySnapshotRef {
  job_id: string
  node_id: string
  snapshot_id: string
}

export interface DailySnapshotScope {
  symbols: string[]
  start: string
  end: string
}

export interface EngineSnapshotSummary {
  snapshot_id: string
  bundle_id: string
  path: string
  backtest?: DailySnapshotScope | null
  datasets: {
    version_id: string
    dataset_id: string
    record_type: EngineLocalDataInput['request']['record_type']
    knowledge_basis: EngineLocalDataInput['knowledge_basis']
    source_ids: string[]
    price_basis: 'raw' | 'qfq' | 'hfq' | null
    scope: Record<string, unknown>
    coverage: EngineCoverage
  }[]
}

export interface EngineCaptureRequest {
  datasets: EngineDataInput[]
  versions?: { dataset_id: string, version_id: string }[]
}

export interface EngineRunRequest {
  extension_fields?: { dataset_id: string; field_sha256: string }[]
  request_version?: '1.0'
  universe: string[]
  start: string
  end: string
  warmup_start?: string | null
  strategy_parameters_json?: string
  market: { adapter_id: string; distribution: string; configuration_json?: string }
  account: {
    currency: string
    order_expiry?: 'next_session' | 'good_till_cancelled' | 'confirmed_end_of_day'
    dividend_treatment?: 'reject' | 'gross_before_personal_tax'
    cost_allocation?: 'fifo_decimal12_half_even'
    sale_cash?: 'immediately_reinvestable'
    bound_execution?: 'block_buy_at_upper_sell_at_lower'
    corporate_action_orders?: 'cancel'
  }
  initial_capital: string
  price_view: {
    provider_id: string
    mode: 'raw' | 'qfq' | 'hfq' | 'extension'
    anchor_at?: string | null
    factor_definition_id?: string | null
    factor_baseline_id?: string | null
  }
  execution?: {
    timeframe: string
    signal_at?: 'bar_close' | 'scheduled'
    fill_at?: 'next_open' | 'legacy_same_close' | 'witnessed_trade'
    slippage_bps?: string
    intrabar_priority?: 'stop_first' | 'take_first'
    end_policy?: 'mark_to_market' | 'attempt_liquidation'
    missing_data?: 'reject' | 'hold_last_valuation'
    position_policy_failure?: 'reject' | 'equal_weight_with_diagnostic'
    order_expiry?: 'next_session' | 'good_till_cancelled' | 'confirmed_end_of_day'
    decision_delay_seconds?: number
    continuous?: {
      book_dataset_id: string
      trade_dataset_id: string
      sessions: {
        instrument_id: string
        trading_date: string
        sequences: string[]
        confirmed_through_at: string
      }[]
      observe_at?: string[]
      decide_at: string[]
    }
  }
}

export interface EnginePreparationRequest extends EngineCaptureRequest {
  definition: string
  dependency_root?: string | null
  run: EngineRunRequest
}

interface EngineResearchNodeBase {
  node_id: string
  study?: EngineStudyRequest | null
  depends_on?: string[]
}

/** Input paths are relative to research/engine/inputs; choose one preparation mode. */
export type EngineResearchNode = EngineResearchNodeBase & (
  | { run: string; snapshot: string; prepare?: null; capture?: null }
  | { run?: null; snapshot?: null; prepare: EnginePreparationRequest; capture?: null }
  | { run?: null; snapshot?: null; prepare?: null; study?: null; capture: EngineCaptureRequest }
)

export interface EngineJobRequest {
  request_version?: '1.0'
  idempotency_key: string
  nodes: EngineResearchNode[]
  budget?: EngineExecutionBudget
}

export interface EngineLockedInput {
  request: EngineResearchNode
  run_archive_id: string | null
  lock_id: string | null
  snapshot_id: string | null
  bundle_id: string | null
  preparation?: {
    sources: { source: EngineDataInput; plan: Record<string, unknown> }[]
    definition?: Record<string, unknown>
    dependencies?: Record<string, unknown>[]
    requirements: string[]
    packages: Record<string, unknown>[]
    runtime: Record<string, unknown>
  } | null
}

export interface EngineJobPlan {
  plan_version: '1.0' | '1.1' | '1.2' | '1.3' | '1.4' | '1.5'
  request: EngineJobRequest
  inputs: EngineLockedInput[]
  effective_budget?: Required<EngineExecutionBudget> | null
}

export interface EngineJobPreview {
  plan_id: string
  plan: EngineJobPlan
  trading_day_capture_nodes?: string[]
  publication_datasets?: string[]
}

type TimeScheduleTrigger = {
  schedule_version?: '1.0'
  trigger: {
    kind?: 'utc_interval'
    starts_at: string
    interval_seconds: number
    ends_at?: string | null
  }
} | {
  schedule_version: '1.1'
  trigger: {
    kind: 'trading_day'
    market_id?: 'cn.equity'
    venue_id?: 'XSHG' | 'XSHE'
    calendar_version: string
    preset_sha256?: string | null
    start_date: string
    end_date: string
    anchor?: 'open' | 'close'
    offset_minutes?: number
    daily_capture_nodes?: string[]
  }
}

type ScheduleTrigger = (TimeScheduleTrigger & {
  misfire_policy?: 'latest_only'
  overlap_policy?: 'skip'
}) | {
  schedule_version: '1.2'
  trigger: { kind: 'data_publication', dataset_id: string }
  misfire_policy?: 'all_publications'
  overlap_policy?: 'queue'
}

export type EngineScheduleRequest = ScheduleTrigger & {
  schedule_key: string
  template: Pick<EngineJobRequest, 'nodes' | 'budget'>
}

export interface EngineScheduleCalendar {
  calendar_id: string
  preset_sha256: string
  calendar_sha256: string
  occurrences: { trading_date: string, due_at: string }[]
}

export interface EngineCalendarPreset {
  version: string
  preset_sha256: string
  venues: Partial<Record<'XSHG' | 'XSHE', number[]>>
}

export interface EngineScheduleRecord {
  schedule_id: string
  request: EngineScheduleRequest
  enabled: boolean
  created_at: string
  updated_at: string
  next_due_at: string | null
  last_delivery_id: string | null
  last_error: { code: string } | null
  publication_cursor?: { format: 'tivelo-publication-cursor-v1', stream_id: string, sequence: number, event_id: string | null, dataset_id: string }
  calendar?: Omit<EngineScheduleCalendar, 'occurrences'> & { trading_days: number, last_due_at: string }
}

export interface EngineScheduleDelivery {
  delivery_id: string
  schedule_id: string
  due_at: string
  trading_date?: string
  publication?: { sequence: number, event_id: string, dataset_id: string, version_id: string, recorded_at: string }
  missed_count: number
  status: 'pending' | 'prepared' | 'submitted' | 'skipped_overlap' | 'failed'
  request_key: string
  plan_id: string | null
  job_id: string | null
  error: { code: string } | null
  created_at: string
  updated_at: string
}

export interface EngineInputCatalog {
  catalog_version: '1.0'
  runs: {
    path: string
    archive_id: string
    lock_id: string
    snapshot_id: string
    strategy_id: string
    start: string
    end: string
    universe_count: number
    price_view: EngineRunRequest['price_view']
    controlled_semantics?: 'anchor_cancelled_float64_v1' | 'projected_prices_float64_v1' | null
    accounting_model: string
    semantics_version: string
    initial_capital: string
    currency: string
  }[]
  snapshots: { path: string; snapshot_id: string; dataset_count: number }[]
  issues: { path: string; code: string }[]
  truncated: boolean
}

export const PRO_DYNAMIC_PRICE_PROVIDER = 'tivelo.pro.dynamic'

export interface EnginePriceChoice {
  provider_id: string
  mode: EngineRunRequest['price_view']['mode']
  state: 'available' | 'not_installed' | 'disabled' | 'incompatible' | 'needs_license' | 'license_expired'
  extension_id?: string
  version?: string | null
  ordinary_backtest?: boolean
}

export interface EnginePriceVariant {
  run: string
  snapshot: string
  source_lock_id: string
  price_view: EngineRunRequest['price_view']
  adapt_controlled: boolean
}

export type EngineJobStatus = 'queued' | 'running' | 'publishing' | 'cancelling'
  | 'succeeded' | 'failed' | 'cancelled' | 'interrupted'

export interface EngineArtifactReference {
  kind: 'validation' | 'result' | 'study' | 'report' | 'pipeline'
    | 'source' | 'dataset' | 'snapshot' | 'preparation'
  identity: string
  path: string
}

export type RepositorySourceIssue = {
  trading_date: string
  symbol: string
  row_number: number
  file_sha256: string
} & (
  | { layout: 'daily_raw_v1' | 'enriched_v1'
      field: 'open' | 'high' | 'low' | 'close' | 'raw_open' | 'raw_high' | 'raw_low' | 'raw_close' | 'volume' | 'amount'
      reason: 'fractional_precision' | 'integral_range' | 'invalid_number' }
  | { layout: 'daily_raw_v1' | 'enriched_v1'
      field: 'quote_ts'; reason: 'quote_date_mismatch' | 'quote_before_finalization' | 'quote_after_observation' }
  | { layout: 'event_factors_v1'; field: 'ex_factor'; reason: 'fractional_precision' | 'integral_range' }
)

export interface EngineJobAttempt {
  number: number
  token: string
  status: EngineJobStatus
  started_at: string
  finished_at: string | null
  progress: { done: number; total: number; phase: string } | null
  error: { code: string; source_issues?: RepositorySourceIssue[] } | null
  artifact: EngineArtifactReference | null
  pending_error?: string
  worker?: Record<string, number | boolean | null>
}

export interface EngineJobRecord {
  job_id: string
  plan_id: string
  status: EngineJobStatus
  created_at: string
  updated_at: string
  cancel_requested: boolean
  nodes: Record<string, {
    status: EngineJobStatus
    attempts: EngineJobAttempt[]
    artifact: EngineArtifactReference | null
  }>
  artifact: EngineArtifactReference | null
  error?: { code: string }
}

export interface EngineResearchReport {
  format: 'tsp-engine-report'
  version: '1.0'
  plan_id: string
  /** Report stage ID (e.g. daily.report); the API path uses the research node ID (daily). */
  node_id: string
  execution: EngineArtifactReference
  /** Versioned result/study summaries; use their declared financial semantics. */
  summary: Record<string, unknown>
}
